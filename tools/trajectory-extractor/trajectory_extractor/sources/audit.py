"""Read msagent audit logs.

The audit log is the semantic layer of a trajectory: the user's task statement,
every human-in-the-loop decision, and one record per subagent delegation with
its parsed ``msagent-io v1`` input and output. It carries no ordinary tool
calls, so it pairs with the checkpoint or trace source rather than replacing it.

Audit files are appended as pretty-printed JSON blocks rather than one object
per line, so they are parsed with ``raw_decode`` -- mirroring
``msagent/audit/read.py``, which is reused when msagent is importable.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from ..models import Phase
from .base import SourceResult

_AUDIT_EVENTS = frozenset({"user.turn", "user.response", "subagent.delegation"})


def _local_iter_json_values(text: str) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from compact or pretty-printed audit log text."""
    decoder = json.JSONDecoder()
    index = 0
    length = len(text)

    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        try:
            payload, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            break
        index = end
        if isinstance(payload, dict):
            yield payload


def _resolve_parser() -> Callable[[str], Iterator[dict[str, Any]]]:
    """Prefer msagent's own audit reader, so format quirks stay in one place.

    Resolved before parsing starts: wrapping the iteration itself in the
    fallback would replay records already yielded by the upstream reader.
    """
    try:
        from msagent.audit.read import iter_json_values as upstream
    except ImportError:
        return _local_iter_json_values
    return upstream


def iter_json_values(text: str) -> Iterator[dict[str, Any]]:
    """Parse compact or pretty-printed audit text into JSON objects."""
    return _resolve_parser()(text)


def _decision_output(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a human-decision record, omitting options when none were offered."""
    output: dict[str, Any] = {"response": payload.get("response")}
    options = payload.get("options")
    if options:
        output["options"] = options
    return output


def find_audit_file(audit_dir: Path, thread_id: str) -> Path | None:
    """Resolve ``<agent-prefix>_<thread_id>.jsonl`` inside an audit directory."""
    if not audit_dir.is_dir():
        return None
    prefixed = sorted(audit_dir.glob(f"*_{thread_id}.jsonl"))
    if prefixed:
        return prefixed[0]
    legacy = audit_dir / f"{thread_id}.jsonl"
    return legacy if legacy.is_file() else None


def list_audit_threads(audit_dir: Path) -> list[tuple[str, str, Path]]:
    """Return ``(thread_id, agent_name, path)`` for every audit file present."""
    if not audit_dir.is_dir():
        return []
    entries: list[tuple[str, str, Path]] = []
    for path in sorted(audit_dir.glob("*.jsonl")):
        stem = path.stem
        agent, separator, thread_id = stem.partition("_")
        if not separator:
            agent, thread_id = "", stem
        entries.append((thread_id, agent, path))
    return entries


def read_audit(path: Path) -> SourceResult:
    """Parse one audit file into user messages and subagent phases."""
    result = SourceResult(name=f"audit:{path.name}")
    if not path.is_file():
        result.warnings.append(f"audit file not found: {path}")
        return result

    agent_prefix, separator, thread_id = path.stem.partition("_")
    result.thread_id = thread_id if separator else path.stem
    result.agent = agent_prefix if separator else ""

    text = path.read_text(encoding="utf-8")
    for payload in iter_json_values(text):
        event = payload.get("event")
        if event not in _AUDIT_EVENTS:
            continue

        if event == "user.turn":
            message = str(payload.get("message") or "").strip()
            if message:
                result.user_messages.append(message)
        elif event == "user.response":
            result.phases.append(
                Phase(
                    name="human-decision",
                    task=str(payload.get("prompt") or ""),
                    status=str(payload.get("kind") or ""),
                    start_time=str(payload.get("start_time") or ""),
                    output=_decision_output(payload),
                )
            )
        else:
            result.phases.append(
                Phase(
                    name=str(payload.get("subagent_type") or "subagent"),
                    task=str(payload.get("task_description_raw") or ""),
                    status=str(payload.get("status") or ""),
                    start_time=str(payload.get("start_time") or ""),
                    end_time=str(payload.get("end_time") or ""),
                    duration_ms=payload.get("duration_ms"),
                    input=payload.get("input") if isinstance(payload.get("input"), dict) else None,
                    output=payload.get("output") if isinstance(payload.get("output"), dict) else None,
                    error=payload.get("error") if isinstance(payload.get("error"), dict) else None,
                )
            )

    if not result.phases:
        result.warnings.append(
            "audit file has no delegation records; enable `audit_log.enabled` in the agent YAML "
            "(only Quantizer ships with it on)"
        )
    return result
