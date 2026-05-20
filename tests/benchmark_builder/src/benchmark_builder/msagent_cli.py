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
    JUDGE_OUTPUT_SCHEMA,
    SLOW_CARD_OUTPUT_SCHEMA,
    build_agent_prompt,
    build_judge_prompt,
    copy_input_data,
    display_path,
    parse_jsonl,
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
        "token_usage_mode": "msagent-jsonl",
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
        schema_text = json.dumps(SLOW_CARD_OUTPUT_SCHEMA, indent=2)
        schema_artifact_path = self.artifact_dir / "slow_card_output.schema.json"
        schema_artifact_path.write_text(schema_text, encoding="utf-8")
        final_artifact_path = self.artifact_dir / f"{case.id}.agent.final.json"
        stdout_path = self.artifact_dir / f"{case.id}.agent.stdout.txt"
        stderr_path = self.artifact_dir / f"{case.id}.agent.stderr.txt"
        jsonl_path = self.artifact_dir / f"{case.id}.agent.events.jsonl"

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
                trace_jsonl_path=jsonl_path,
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
            raw_events = read_msagent_jsonl_events(jsonl_path)
            token_usage = extract_msagent_token_usage(raw_events)

            if completed.returncode != 0:
                raise RuntimeError(
                    "msagent failed for agent run "
                    f"{case.id} with exit code {completed.returncode}: {completed.stderr[-4000:]}"
                )
            final_answer = read_msagent_structured_final(
                completed.stdout,
                required_keys={"slow_cards"},
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
                jsonl_path=display_path(jsonl_path, self.workspace),
                jsonl_event_count=len(raw_events),
                stderr_tail=completed.stderr[-2000:],
                duration_ms=duration_ms,
            )
            for event in normalize_msagent_events(raw_events) or normalize_msagent_stdout(completed.stdout):
                trace.add(**event)
            trace.final_answer(final_answer)
            trace.finish(token_usage)
            trace.duration_ms = duration_ms
            return trace.to_dict()


class MsagentCliJudge:
    judge_info = {
        "name": "msagent-cli-judge",
        "runtime": "msagent one-shot",
        "token_usage_mode": "msagent-jsonl",
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
        correctness: dict[str, Any],
    ) -> dict[str, Any]:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        schema_text = json.dumps(JUDGE_OUTPUT_SCHEMA, indent=2)
        schema_artifact_path = self.artifact_dir / "judge_output.schema.json"
        schema_artifact_path.write_text(schema_text, encoding="utf-8")
        final_artifact_path = self.artifact_dir / f"{case.id}.judge.final.json"
        stdout_path = self.artifact_dir / f"{case.id}.judge.stdout.txt"
        stderr_path = self.artifact_dir / f"{case.id}.judge.stderr.txt"
        jsonl_path = self.artifact_dir / f"{case.id}.judge.events.jsonl"

        prompt = build_msagent_judge_prompt(
            case,
            trace,
            correctness,
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
                trace_jsonl_path=jsonl_path,
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
            raw_events = read_msagent_jsonl_events(jsonl_path)
            token_usage = extract_msagent_token_usage(raw_events)

            if completed.returncode != 0:
                raise RuntimeError(
                    "msagent failed for judge run "
                    f"{case.id} with exit code {completed.returncode}: {completed.stderr[-4000:]}"
                )
            judge_result = read_msagent_structured_final(
                completed.stdout,
                required_keys={"overall_score"},
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
                "token_usage": token_usage,
                "jsonl_path": display_path(jsonl_path, self.workspace),
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
    trace_jsonl_path: Path | None,
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
    if trace_jsonl_path is not None:
        cmd.extend(["--trace-jsonl", str(trace_jsonl_path)])
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
    correctness: dict[str, Any],
    *,
    schema_text: str,
) -> str:
    base_prompt = build_judge_prompt(case, trace, correctness)
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


def read_msagent_jsonl_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return parse_jsonl(path.read_text(encoding="utf-8"))


def extract_msagent_token_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("type") != "session_finished":
            continue
        usage = event.get("token_usage")
        if isinstance(usage, dict):
            return normalize_msagent_usage(usage)

    for event in reversed(events):
        if event.get("type") != "token_usage":
            continue
        cumulative = event.get("cumulative")
        if isinstance(cumulative, dict):
            return normalize_msagent_usage(cumulative)

    return msagent_token_usage()


def normalize_msagent_usage(usage: dict[str, Any]) -> dict[str, Any]:
    try:
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)
    except (TypeError, ValueError):
        return msagent_token_usage()

    available = bool(usage.get("available", total_tokens > 0))
    return {
        "available": available,
        "source": str(usage.get("source") or "msagent-cli-jsonl"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def normalize_msagent_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in events:
        event_type = str(raw.get("type") or "")
        if event_type == "tool_call":
            normalized.append({
                "event_type": "tool_call",
                "raw_type": raw.get("raw_type") or "assistant.tool_call",
                "tool": str(raw.get("tool") or "unknown"),
                "item_id": raw.get("item_id"),
                "input": raw.get("input") if isinstance(raw.get("input"), dict) else {},
                "summary": trim_event_text(raw),
            })
            continue

        if event_type == "tool_result":
            output = raw.get("output")
            payload: dict[str, Any] = {
                "event_type": "tool_result",
                "raw_type": raw.get("raw_type") or "tool.result",
                "tool": str(raw.get("tool") or "tool"),
                "item_id": raw.get("item_id"),
                "output": output if isinstance(output, dict) else {"content": str(output or "")},
                "summary": trim_event_text(raw),
            }
            if raw.get("duration_ms") is not None:
                payload["duration_ms"] = raw.get("duration_ms")
            normalized.append(payload)
            continue

        if event_type == "assistant_message":
            content = str(raw.get("content") or "")
            if content:
                normalized.append({
                    "event_type": "agent_event",
                    "raw_type": "assistant.message",
                    "summary": trim_text(content, 2000),
                })
            continue

        if event_type in {"session_started", "session_finished", "token_usage", "error"}:
            normalized.append({
                "event_type": "agent_event",
                "raw_type": f"msagent.{event_type}",
                "summary": trim_event_text(raw),
            })

    return normalized


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
        "source": "msagent-cli-jsonl-no-usage-event",
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
