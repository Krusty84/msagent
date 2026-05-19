from __future__ import annotations

import json
import os
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
    extract_token_usage,
    parse_jsonl,
    safe_command_for_trace,
    safe_path_component,
    trim_event_text,
    trim_text,
)
from .schema import BenchmarkCase
from .trace import TraceBuilder


class ClaudeCliUnavailableError(RuntimeError):
    pass


class ClaudeCliAgent:
    agent_info = {
        "name": "claude-cli-agent",
        "runtime": "claude -p",
        "token_usage_mode": "claude-stream-json",
    }

    def __init__(
        self,
        *,
        workspace: Path,
        artifact_dir: Path,
        model: str | None = None,
        timeout_seconds: int = 900,
    ) -> None:
        self.workspace = workspace.resolve()
        self.artifact_dir = artifact_dir.resolve()
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.claude_path = resolve_claude_cli()

    def run(self, case: BenchmarkCase, input_path: Path) -> dict[str, Any]:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        schema_text = json.dumps(SLOW_CARD_OUTPUT_SCHEMA, indent=2)
        schema_artifact_path = self.artifact_dir / "slow_card_output.schema.json"
        schema_artifact_path.write_text(schema_text, encoding="utf-8")
        final_artifact_path = self.artifact_dir / f"{case.id}.agent.final.json"
        jsonl_path = self.artifact_dir / f"{case.id}.agent.events.jsonl"

        prefix = f"benchmark-builder-claude-agent-{safe_path_component(case.id)}-"
        with tempfile.TemporaryDirectory(prefix=prefix) as temp_dir:
            run_workspace = Path(temp_dir).resolve()
            isolated_input_path = run_workspace / "input_data"
            copy_input_data(input_path, isolated_input_path)

            prompt = build_agent_prompt(
                case,
                isolated_input_path,
                visible_input_path=Path("input_data"),
            )
            cmd = build_claude_command(
                self.claude_path,
                schema_text=schema_text,
                model=self.model,
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
            )
            duration_ms = round((perf_counter() - started) * 1000)
            jsonl_path.write_text(completed.stdout, encoding="utf-8")

            raw_events = parse_jsonl(completed.stdout)
            token_usage = extract_claude_token_usage(raw_events)
            if completed.returncode != 0:
                raise RuntimeError(
                    "claude -p failed for agent run "
                    f"{case.id} with exit code {completed.returncode}: {completed.stderr[-4000:]}"
                )
            final_answer = read_claude_structured_final(raw_events, completed.stdout)
            final_artifact_path.write_text(
                json.dumps(final_answer, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            trace = TraceBuilder(case_id=case.id, prompt=case.prompt, agent={
                **self.agent_info,
                "model": self.model,
                "cli_path": str(self.claude_path),
                "isolation": "temp-workspace-copied-input",
            })
            trace.add(
                "agent_run",
                command=safe_command_for_trace(cmd),
                cwd=str(run_workspace),
                input_data_path="input_data",
                input_data_isolation="copied",
                jsonl_path=display_path(jsonl_path, self.workspace),
                jsonl_event_count=len(raw_events),
                stderr_tail=completed.stderr[-2000:],
                duration_ms=duration_ms,
            )
            for event in normalize_claude_events(raw_events):
                trace.add(**event)
            trace.final_answer(final_answer)
            trace.finish(token_usage)
            trace.duration_ms = duration_ms
            return trace.to_dict()


class ClaudeCliJudge:
    judge_info = {
        "name": "claude-cli-judge",
        "runtime": "claude -p",
        "token_usage_mode": "claude-stream-json",
    }

    def __init__(
        self,
        *,
        workspace: Path,
        artifact_dir: Path,
        model: str | None = None,
        timeout_seconds: int = 900,
    ) -> None:
        self.workspace = workspace.resolve()
        self.artifact_dir = artifact_dir.resolve()
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.claude_path = resolve_claude_cli()

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
        jsonl_path = self.artifact_dir / f"{case.id}.judge.events.jsonl"

        prompt = build_judge_prompt(case, trace, correctness)
        prefix = f"benchmark-builder-claude-judge-{safe_path_component(case.id)}-"
        with tempfile.TemporaryDirectory(prefix=prefix) as temp_dir:
            run_workspace = Path(temp_dir).resolve()
            cmd = build_claude_command(
                self.claude_path,
                schema_text=schema_text,
                model=self.model,
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
            )
            duration_ms = round((perf_counter() - started) * 1000)
            jsonl_path.write_text(completed.stdout, encoding="utf-8")
            raw_events = parse_jsonl(completed.stdout)
            token_usage = extract_claude_token_usage(raw_events)
            if completed.returncode != 0:
                raise RuntimeError(
                    "claude -p failed for judge run "
                    f"{case.id} with exit code {completed.returncode}: {completed.stderr[-4000:]}"
                )
            judge_result = read_claude_structured_final(raw_events, completed.stdout)
            final_artifact_path.write_text(
                json.dumps(judge_result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            return {
                "case_id": case.id,
                "judge": {
                    **self.judge_info,
                    "model": self.model,
                    "cli_path": str(self.claude_path),
                    "isolation": "temp-workspace-no-repo",
                },
                **judge_result,
                "duration_ms": duration_ms,
                "token_usage": token_usage,
                "jsonl_path": display_path(jsonl_path, self.workspace),
            }


def resolve_claude_cli() -> Path:
    configured = os.environ.get("CLAUDE_CLI")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(shutil.which("claude") or ""),
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    raise ClaudeCliUnavailableError(
        "Claude CLI was not found. Install Claude Code or set CLAUDE_CLI to its path."
    )


def build_claude_command(
    claude_path: Path,
    *,
    schema_text: str,
    model: str | None,
    prompt: str,
) -> list[str]:
    cmd = [
        str(claude_path),
        "-p",
        "--bare",
        "--no-session-persistence",
        "--permission-mode",
        "bypassPermissions",
        "--tools",
        "default",
        "--output-format",
        "stream-json",
        "--verbose",
        "--json-schema",
        compact_json(schema_text),
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)
    return cmd


def compact_json(text: str) -> str:
    return json.dumps(json.loads(text), separators=(",", ":"))


def read_claude_structured_final(
    events: list[dict[str, Any]],
    stdout: str,
) -> dict[str, Any]:
    for event in reversed(events):
        structured_output = event.get("structured_output")
        if isinstance(structured_output, dict):
            return structured_output

    for event in reversed(events):
        if event.get("type") != "result":
            continue
        result = event.get("result")
        parsed = parse_json_object(result)
        if parsed is not None:
            return parsed

    parsed = parse_json_object(stdout.strip().splitlines()[-1] if stdout.strip() else "")
    if parsed is not None:
        return parsed
    raise RuntimeError(f"Could not parse structured Claude final output: {stdout[-1000:]}")


def parse_json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = parse_fenced_json(text)
    if isinstance(parsed, dict):
        return parsed
    return None


def parse_fenced_json(text: str) -> Any:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def extract_claude_token_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("type") != "result":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            break
        normalized = extract_token_usage([{"usage": usage}])
        if normalized.get("available"):
            return {
                **normalized,
                "source": "claude-stream-json-result",
            }
    return {
        "available": False,
        "source": "claude-stream-json-no-usage-event",
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


def normalize_claude_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    tool_names_by_id: dict[str, str] = {}
    for raw in events:
        event_type = str(raw.get("type") or "")
        if event_type == "assistant":
            message_events = normalize_claude_message(raw)
            for event in message_events:
                if event.get("event_type") == "tool_call" and event.get("item_id") is not None:
                    tool_names_by_id[str(event["item_id"])] = str(event.get("tool") or "unknown")
            normalized.extend(message_events)
        elif event_type == "user":
            normalized.extend(normalize_claude_tool_results(raw, tool_names_by_id))
        elif event_type == "result":
            normalized.append({
                "event_type": "agent_event",
                "raw_type": event_type,
                "summary": trim_event_text(raw),
            })
        elif event_type == "system":
            normalized.append({
                "event_type": "agent_event",
                "raw_type": f"system.{raw.get('subtype') or 'event'}",
                "summary": trim_event_text(raw),
            })
    return normalized


def normalize_claude_message(raw: dict[str, Any]) -> list[dict[str, Any]]:
    message = raw.get("message")
    if not isinstance(message, dict):
        return []
    normalized = []
    for content in message.get("content", []):
        if not isinstance(content, dict):
            continue
        content_type = content.get("type")
        if content_type == "tool_use":
            normalized.append({
                "event_type": "tool_call",
                "raw_type": "assistant.tool_use",
                "tool": str(content.get("name") or "unknown"),
                "item_id": content.get("id"),
                "input": content.get("input") if isinstance(content.get("input"), dict) else {},
            })
        elif content_type == "text":
            text = str(content.get("text") or "")
            if text:
                normalized.append({
                    "event_type": "agent_event",
                    "raw_type": "assistant.text",
                    "summary": trim_text(text, 2000),
                })
        elif content_type == "thinking":
            thinking = str(content.get("thinking") or "")
            if thinking:
                normalized.append({
                    "event_type": "thought",
                    "raw_type": "assistant.thinking",
                    "content": trim_text(thinking, 2000),
                })
    return normalized


def normalize_claude_tool_results(
    raw: dict[str, Any],
    tool_names_by_id: dict[str, str],
) -> list[dict[str, Any]]:
    message = raw.get("message")
    if not isinstance(message, dict):
        return []
    normalized = []
    for content in message.get("content", []):
        if not isinstance(content, dict) or content.get("type") != "tool_result":
            continue
        result = raw.get("tool_use_result")
        item_id = content.get("tool_use_id")
        normalized.append({
            "event_type": "tool_result",
            "raw_type": "user.tool_result",
            "tool": tool_names_by_id.get(str(item_id), "tool_result"),
            "item_id": item_id,
            "output": normalize_claude_tool_result_output(content, result),
        })
    return normalized


def normalize_claude_tool_result_output(
    content: dict[str, Any],
    result: Any,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "is_error": bool(content.get("is_error")),
    }
    if isinstance(result, dict):
        stdout = str(result.get("stdout") or "")
        stderr = str(result.get("stderr") or "")
        output.update({
            "stdout": trim_text(stdout),
            "stdout_chars": len(stdout),
            "stdout_truncated": len(stdout) > 4000,
            "stderr": trim_text(stderr, 2000),
            "stderr_chars": len(stderr),
            "stderr_truncated": len(stderr) > 2000,
            "interrupted": bool(result.get("interrupted")),
        })
    else:
        text = str(content.get("content") or result or "")
        output.update({
            "content": trim_text(text),
            "content_chars": len(text),
            "content_truncated": len(text) > 4000,
        })
    return output
