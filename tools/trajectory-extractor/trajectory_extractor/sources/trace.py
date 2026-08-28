"""Read the JSONL trace written by ``msagent --trace-jsonl <FILE>``.

The trace is the most convenient single-file source, with two caveats worth
knowing before trusting it:

* Text is capped at 4000 characters per message and per tool result by the
  recorder, so long arguments and outputs arrive truncated.
* The recorder writes no user-message events, so the task statement has to come
  from the audit log or the checkpoint database.

Both are reported as warnings rather than worked around, because pretending the
data is complete would corrupt everything downstream.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import MAIN_ORIGIN, RawEvent
from .base import SourceResult

SUBAGENT_ORIGIN = "subagent"


def _origin(payload: dict[str, Any]) -> str:
    origin = payload.get("origin")
    if isinstance(origin, str) and origin.strip():
        return origin.strip()
    return MAIN_ORIGIN


def read_trace(path: Path) -> SourceResult:
    """Parse a trace file into raw events."""
    result = SourceResult(name=f"trace:{path.name}")
    if not path.is_file():
        result.warnings.append(f"trace file not found: {path}")
        return result

    truncated_seen = False
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            result.warnings.append(f"{path.name}:{line_number}: malformed JSON line skipped")
            continue
        if not isinstance(payload, dict):
            continue

        event_type = payload.get("type")
        index = int(payload.get("index") or 0)

        if event_type == "session_started":
            result.thread_id = str(payload.get("thread_id") or "")
            result.agent = str(payload.get("agent") or "")
        elif event_type == "assistant_message":
            content = str(payload.get("content") or "")
            truncated_seen = truncated_seen or bool(payload.get("content_truncated"))
            if content.strip():
                result.events.append(
                    RawEvent(
                        kind="assistant",
                        index=index,
                        origin=_origin(payload),
                        text=content,
                        timestamp=payload.get("timestamp"),
                        truncated=bool(payload.get("content_truncated")),
                    )
                )
        elif event_type == "tool_call":
            arguments = payload.get("input")
            result.events.append(
                RawEvent(
                    kind="tool_call",
                    index=index,
                    origin=_origin(payload),
                    tool=str(payload.get("tool") or "unknown"),
                    call_id=str(payload.get("item_id") or ""),
                    args=arguments if isinstance(arguments, dict) else {},
                    timestamp=payload.get("timestamp"),
                )
            )
        elif event_type == "tool_result":
            output = payload.get("output")
            output = output if isinstance(output, dict) else {}
            truncated = bool(output.get("content_truncated"))
            truncated_seen = truncated_seen or truncated
            result.events.append(
                RawEvent(
                    kind="tool_result",
                    index=index,
                    origin=_origin(payload),
                    tool=str(payload.get("tool") or ""),
                    call_id=str(payload.get("item_id") or ""),
                    text=str(output.get("content") or ""),
                    ok=not bool(output.get("is_error")),
                    duration_ms=payload.get("duration_ms"),
                    timestamp=payload.get("timestamp"),
                    truncated=truncated,
                )
            )
        elif event_type == "error":
            result.warnings.append(f"session error recorded: {payload.get('error_type')}: {payload.get('message')}")

    if truncated_seen:
        result.warnings.append(
            "trace contains truncated text (recorder caps messages and tool results at 4000 chars); "
            "read the checkpoint database for full arguments"
        )
    result.warnings.append("trace files carry no user messages; task text comes from audit or checkpoints")
    return result
