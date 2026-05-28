from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from time import perf_counter
from typing import Any

from .codex_cli import (
    AGENT_OUTPUT_SCHEMA,
    JUDGE_OUTPUT_SCHEMA,
    build_agent_prompt,
    build_judge_prompt,
    copy_input_data,
    display_path,
    safe_command_for_trace,
    safe_path_component,
    trim_event_text,
    trim_text,
)
from .schema import BenchmarkCase
from .trace import TraceBuilder


DEFAULT_MSAGENT_AGENT = "Hermes"
DEFAULT_MSAGENT_APPROVAL_MODE = "aggressive"


class MsagentCliUnavailableError(RuntimeError):
    pass


class MsagentCliAgent:
    agent_info = {
        "name": "msagent-cli-agent",
        "runtime": "msagent one-shot",
        "token_usage_mode": "unavailable",
    }

    def __init__(
        self,
        *,
        workspace: Path,
        artifact_dir: Path,
        model: str | None = None,
        timeout_seconds: int = 900,
        msagent_agent: str | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.artifact_dir = artifact_dir.resolve()
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.msagent_agent = msagent_agent or os.environ.get("MSAGENT_AGENT") or DEFAULT_MSAGENT_AGENT
        self.msagent_command = resolve_msagent_cli_command()
        self.approval_mode = os.environ.get("MSAGENT_APPROVAL_MODE") or DEFAULT_MSAGENT_APPROVAL_MODE

    def run(self, case: BenchmarkCase, input_path: Path) -> dict[str, Any]:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        schema_text = json.dumps(AGENT_OUTPUT_SCHEMA, indent=2)
        schema_artifact_path = self.artifact_dir / "agent_output.schema.json"
        schema_artifact_path.write_text(schema_text, encoding="utf-8")
        final_artifact_path = self.artifact_dir / f"{case.id}.agent.final.json"
        stdout_path = self.artifact_dir / f"{case.id}.agent.stdout.txt"
        stderr_path = self.artifact_dir / f"{case.id}.agent.stderr.txt"

        prefix = f"benchmark-builder-msagent-agent-{safe_path_component(case.id)}-"
        with tempfile.TemporaryDirectory(prefix=prefix) as temp_dir:
            run_workspace = Path(temp_dir).resolve()
            isolated_input_path = run_workspace / "input_data"
            copy_input_data(input_path, isolated_input_path)
            copy_msagent_config(self.workspace, run_workspace)

            prompt = build_msagent_agent_prompt(
                case,
                isolated_input_path,
                visible_input_path=Path("input_data"),
                schema_text=schema_text,
            )
            cmd = build_msagent_command(
                self.msagent_command,
                working_dir=run_workspace,
                msagent_agent=self.msagent_agent,
                model=self.model,
                approval_mode=self.approval_mode,
                prompt=prompt,
            )

            started = perf_counter()
            completed = subprocess.run(
                cmd,
                cwd=run_workspace,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                env=build_msagent_env(),
            )
            duration_ms = round((perf_counter() - started) * 1000)
            stdout_path.write_text(completed.stdout, encoding="utf-8")
            stderr_path.write_text(completed.stderr, encoding="utf-8")

            if completed.returncode != 0:
                raise RuntimeError(
                    "msagent failed for agent run "
                    f"{case.id} with exit code {completed.returncode}: {completed.stderr[-4000:]}"
                )
            final_answer = read_msagent_structured_final(
                completed.stdout,
                required_keys={"answer"},
            )
            final_artifact_path.write_text(
                json.dumps(final_answer, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            trace = TraceBuilder(
                case_id=case.id,
                prompt=case.prompt,
                agent={
                    **self.agent_info,
                    "model": self.model,
                    "cli_command": self.msagent_command,
                    "msagent_agent": self.msagent_agent,
                    "approval_mode": self.approval_mode,
                    "isolation": "temp-workspace-copied-input",
                },
            )
            trace.add(
                "agent_run",
                command=safe_command_for_trace(cmd),
                cwd=str(run_workspace),
                input_data_path="input_data",
                input_data_isolation="copied",
                stdout_path=display_path(stdout_path, self.workspace),
                stderr_path=display_path(stderr_path, self.workspace),
                stderr_tail=completed.stderr[-2000:],
                duration_ms=duration_ms,
            )
            for event in normalize_msagent_stdout(completed.stdout):
                trace.add(**event)
            trace.final_answer(final_answer)
            trace.finish(msagent_token_usage())
            trace.duration_ms = duration_ms
            return trace.to_dict()


class MsagentCliJudge:
    judge_info = {
        "name": "msagent-cli-judge",
        "runtime": "msagent one-shot",
        "token_usage_mode": "unavailable",
    }

    def __init__(
        self,
        *,
        workspace: Path,
        artifact_dir: Path,
        model: str | None = None,
        timeout_seconds: int = 900,
        msagent_agent: str | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.artifact_dir = artifact_dir.resolve()
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.msagent_agent = msagent_agent or os.environ.get("MSAGENT_AGENT") or DEFAULT_MSAGENT_AGENT
        self.msagent_command = resolve_msagent_cli_command()
        self.approval_mode = os.environ.get("MSAGENT_APPROVAL_MODE") or DEFAULT_MSAGENT_APPROVAL_MODE

    def judge(
        self,
        case: BenchmarkCase,
        trace: dict[str, Any],
    ) -> dict[str, Any]:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        schema_text = json.dumps(JUDGE_OUTPUT_SCHEMA, indent=2)
        schema_artifact_path = self.artifact_dir / "judge_output.schema.json"
        schema_artifact_path.write_text(schema_text, encoding="utf-8")
        final_artifact_path = self.artifact_dir / f"{case.id}.judge.final.json"
        stdout_path = self.artifact_dir / f"{case.id}.judge.stdout.txt"
        stderr_path = self.artifact_dir / f"{case.id}.judge.stderr.txt"

        prompt = build_msagent_judge_prompt(
            case,
            trace,
            schema_text=schema_text,
        )
        prefix = f"benchmark-builder-msagent-judge-{safe_path_component(case.id)}-"
        with tempfile.TemporaryDirectory(prefix=prefix) as temp_dir:
            run_workspace = Path(temp_dir).resolve()
            copy_msagent_config(self.workspace, run_workspace)
            cmd = build_msagent_command(
                self.msagent_command,
                working_dir=run_workspace,
                msagent_agent=self.msagent_agent,
                model=self.model,
                approval_mode=self.approval_mode,
                prompt=prompt,
            )

            started = perf_counter()
            completed = subprocess.run(
                cmd,
                cwd=run_workspace,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                env=build_msagent_env(),
            )
            duration_ms = round((perf_counter() - started) * 1000)
            stdout_path.write_text(completed.stdout, encoding="utf-8")
            stderr_path.write_text(completed.stderr, encoding="utf-8")

            if completed.returncode != 0:
                raise RuntimeError(
                    "msagent failed for judge run "
                    f"{case.id} with exit code {completed.returncode}: {completed.stderr[-4000:]}"
                )
            judge_result = read_msagent_structured_final(
                completed.stdout,
                required_keys={"rubric_score"},
            )
            final_artifact_path.write_text(
                json.dumps(judge_result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            return {
                "case_id": case.id,
                "judge": {
                    **self.judge_info,
                    "model": self.model,
                    "cli_command": self.msagent_command,
                    "msagent_agent": self.msagent_agent,
                    "approval_mode": self.approval_mode,
                    "isolation": "temp-workspace-no-repo",
                },
                **judge_result,
                "duration_ms": duration_ms,
                "token_usage": msagent_token_usage(),
                "stdout_path": display_path(stdout_path, self.workspace),
                "stderr_path": display_path(stderr_path, self.workspace),
            }


def resolve_msagent_cli_command() -> list[str]:
    configured = os.environ.get("MSAGENT_CLI")
    if configured:
        configured_parts = shlex.split(configured)
        if not configured_parts:
            raise MsagentCliUnavailableError("MSAGENT_CLI is set but empty.")
        executable = configured_parts[0]
        executable_path = Path(executable).expanduser()
        if executable_path.exists():
            return [str(executable_path.resolve()), *configured_parts[1:]]
        resolved = shutil.which(executable)
        if resolved:
            return [resolved, *configured_parts[1:]]
        raise MsagentCliUnavailableError(
            f"MSAGENT_CLI executable was not found: {executable}"
        )

    msagent_path = shutil.which("msagent")
    if msagent_path:
        return [msagent_path]
    raise MsagentCliUnavailableError(
        "msAgent CLI was not found. Install mindstudio-agent or set MSAGENT_CLI, "
        'for example MSAGENT_CLI="uv --project /path/to/msagent run msagent".'
    )


def build_msagent_command(
    msagent_command: list[str],
    *,
    working_dir: Path,
    msagent_agent: str,
    model: str | None,
    approval_mode: str,
    prompt: str,
) -> list[str]:
    cmd = [
        *msagent_command,
        "--no-stream",
        "--working-dir",
        str(working_dir),
        "--agent",
        msagent_agent,
        "--approval-mode",
        approval_mode,
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)
    return cmd


def build_msagent_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    env.setdefault("CLICOLOR", "0")
    env.setdefault("TERM", "dumb")
    env.setdefault("COLUMNS", "20000")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def copy_msagent_config(source_workspace: Path, run_workspace: Path) -> None:
    source_config = source_workspace / ".msagent"
    if not source_config.is_dir() or source_config.is_symlink():
        return

    destination_config = run_workspace / ".msagent"
    ignored_dirs = {".git", "logs", "__pycache__"}
    ignored_files = {".history"}
    ignored_prefixes = ("config.checkpoints.db",)
    for root, dirs, files in os.walk(source_config, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(source_config)
        target_root = destination_config / relative_root
        target_root.mkdir(parents=True, exist_ok=True)

        dirs[:] = [
            dirname
            for dirname in dirs
            if dirname not in ignored_dirs and not (root_path / dirname).is_symlink()
        ]
        for filename in files:
            if filename in ignored_files or filename.startswith(ignored_prefixes):
                continue
            source_file = root_path / filename
            if source_file.is_symlink():
                continue
            shutil.copy2(source_file, target_root / filename)


def build_msagent_agent_prompt(
    case: BenchmarkCase,
    input_path: Path,
    *,
    visible_input_path: Path | str | None,
    schema_text: str,
) -> str:
    base_prompt = build_agent_prompt(
        case,
        input_path,
        visible_input_path=visible_input_path,
    )
    return f"""{base_prompt}

Output contract for msagent one-shot mode:
- Your final response must be a single JSON object.
- Do not wrap the JSON in Markdown fences.
- Do not include any text before or after the JSON object.
- The JSON object must match this schema:
{schema_text}
"""


def build_msagent_judge_prompt(
    case: BenchmarkCase,
    trace: dict[str, Any],
    *,
    schema_text: str,
) -> str:
    base_prompt = build_judge_prompt(case, trace)
    return f"""{base_prompt}

Output contract for msagent one-shot mode:
- Your final response must be a single JSON object.
- Do not wrap the JSON in Markdown fences.
- Do not include any text before or after the JSON object.
- The JSON object must match this schema:
{schema_text}
"""


def read_msagent_structured_final(
    stdout: str,
    *,
    required_keys: set[str],
) -> dict[str, Any]:
    candidates = extract_json_objects(strip_ansi(stdout))
    for candidate in reversed(candidates):
        if required_keys <= set(candidate):
            return candidate
    if candidates:
        return candidates[-1]
    raise RuntimeError(f"Could not parse structured msagent final output: {stdout[-1000:]}")


def extract_json_objects(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)


def normalize_msagent_stdout(stdout: str) -> list[dict[str, Any]]:
    text = strip_ansi(stdout).strip()
    if not text:
        return []

    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        tool_call = parse_msagent_tool_call_line(line)
        if tool_call is not None:
            events.append(tool_call)

    events.append({
        "event_type": "agent_event",
        "raw_type": "msagent.stdout",
        "summary": trim_text(text, 4000),
    })
    return events


def parse_msagent_tool_call_line(line: str) -> dict[str, Any] | None:
    cleaned = line.strip()
    match = re.search(r"Use tool\s+([A-Za-z0-9_.:-]+)", cleaned)
    if not match:
        return None
    return {
        "event_type": "tool_call",
        "raw_type": "msagent.rendered_tool_call",
        "tool": match.group(1),
        "input": {},
        "summary": trim_event_text({"line": cleaned}),
    }


def msagent_token_usage() -> dict[str, Any]:
    return {
        "available": False,
        "source": "msagent-cli-no-usage-event",
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
