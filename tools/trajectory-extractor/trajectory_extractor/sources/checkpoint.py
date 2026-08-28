"""Read LangGraph checkpoints written by msagent.

This is the only source with untruncated tool arguments and results, and it is
written for every agent configured with ``checkpointer: sqlite`` -- no flags to
remember before the run.

Two caveats shape the implementation:

* Deserializing a checkpoint blob requires LangGraph's own serializer, so this
  module imports it lazily and reports a clear error when it is missing. The
  message-to-event conversion below is deliberately duck-typed and free of
  LangGraph imports, so it stays testable without the dependency.
* When automatic compaction has fired, early messages in the latest checkpoint
  are replaced by a summary. The originals live in ``conversation_history/``;
  see ``sources/history.py``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import MAIN_ORIGIN, RawEvent
from .base import SourceResult

MAIN_NAMESPACE = ""
SUBAGENT_ORIGIN = "subagent"


class CheckpointUnavailable(RuntimeError):
    """Raised when LangGraph is not importable in the current interpreter."""


@dataclass(frozen=True, slots=True)
class ThreadInfo:
    """One conversation thread found in the checkpoint database."""

    thread_id: str
    namespaces: tuple[str, ...]
    latest_checkpoint_id: str


def _field(message: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a message object or its dict form."""
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)


def _content_text(message: Any) -> str:
    """Flatten message content, which may be a string or a list of blocks."""
    content = _field(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return "" if content is None else str(content)


def _message_kind(message: Any) -> str:
    """Normalize a message to ``human`` / ``ai`` / ``tool`` / ``system``."""
    kind = _field(message, "type") or _field(message, "role") or ""
    kind = str(kind).lower()
    aliases = {
        "human": "human",
        "user": "human",
        "ai": "ai",
        "assistant": "ai",
        "tool": "tool",
        "function": "tool",
        "system": "system",
    }
    if kind in aliases:
        return aliases[kind]
    if _field(message, "tool_call_id") is not None:
        return "tool"
    return kind or "unknown"


def _tool_failed(message: Any) -> bool:
    if _field(message, "status") == "error":
        return True
    return bool(_field(message, "is_error", False))


def messages_to_events(
    messages: list[Any],
    *,
    origin: str = MAIN_ORIGIN,
    namespace: str = MAIN_NAMESPACE,
    start_index: int = 0,
) -> tuple[list[RawEvent], list[str]]:
    """Convert LangChain messages into raw events plus the user's messages."""
    events: list[RawEvent] = []
    user_messages: list[str] = []
    index = start_index

    for message in messages:
        kind = _message_kind(message)
        if kind == "system":
            continue

        index += 1
        text = _content_text(message)

        if kind == "human":
            if text.strip():
                user_messages.append(text.strip())
                events.append(RawEvent(kind="user", index=index, origin=origin, namespace=namespace, text=text))
        elif kind == "ai":
            if text.strip():
                events.append(RawEvent(kind="assistant", index=index, origin=origin, namespace=namespace, text=text))
            for call in _field(message, "tool_calls", None) or []:
                index += 1
                arguments = _field(call, "args", None)
                events.append(
                    RawEvent(
                        kind="tool_call",
                        index=index,
                        origin=origin,
                        namespace=namespace,
                        tool=str(_field(call, "name", "") or "unknown"),
                        call_id=str(_field(call, "id", "") or ""),
                        args=arguments if isinstance(arguments, dict) else {},
                    )
                )
        elif kind == "tool":
            events.append(
                RawEvent(
                    kind="tool_result",
                    index=index,
                    origin=origin,
                    namespace=namespace,
                    tool=str(_field(message, "name", "") or ""),
                    call_id=str(_field(message, "tool_call_id", "") or ""),
                    text=text,
                    ok=not _tool_failed(message),
                )
            )

    return events, user_messages


def list_threads(db_path: Path) -> list[ThreadInfo]:
    """List conversation threads in a checkpoint database (no LangGraph needed)."""
    if not db_path.is_file():
        return []

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT thread_id, checkpoint_ns, MAX(checkpoint_id) AS latest "
            "FROM checkpoints GROUP BY thread_id, checkpoint_ns ORDER BY latest DESC"
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        connection.close()

    grouped: dict[str, list[tuple[str, str]]] = {}
    order: list[str] = []
    for thread_id, namespace, latest in rows:
        if thread_id not in grouped:
            grouped[thread_id] = []
            order.append(thread_id)
        grouped[thread_id].append((str(namespace or ""), str(latest or "")))

    threads: list[ThreadInfo] = []
    for thread_id in order:
        entries = grouped[thread_id]
        namespaces = tuple(sorted({namespace for namespace, _latest in entries}))
        latest_checkpoint_id = max(latest for _namespace, latest in entries)
        threads.append(
            ThreadInfo(
                thread_id=str(thread_id),
                namespaces=namespaces,
                latest_checkpoint_id=latest_checkpoint_id,
            )
        )
    return threads


def _load_saver(db_path: Path):
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise CheckpointUnavailable(
            "reading checkpoints requires langgraph-checkpoint-sqlite; "
            "install msagent's dependencies or use --source trace/audit"
        ) from exc

    connection = sqlite3.connect(str(db_path), check_same_thread=False)
    return SqliteSaver(connection), connection


def read_checkpoint(
    db_path: Path,
    thread_id: str,
    *,
    include_subagents: bool = True,
) -> SourceResult:
    """Load the latest checkpoint of a thread and convert it into raw events."""
    result = SourceResult(name=f"checkpoint:{thread_id}", thread_id=thread_id)
    if not db_path.is_file():
        result.warnings.append(f"checkpoint database not found: {db_path}")
        return result

    namespaces = [MAIN_NAMESPACE]
    for info in list_threads(db_path):
        if info.thread_id == thread_id:
            extra = [ns for ns in info.namespaces if ns != MAIN_NAMESPACE]
            if include_subagents:
                namespaces.extend(extra)
            elif extra:
                result.warnings.append(f"skipped {len(extra)} subagent namespace(s)")
            break

    saver, connection = _load_saver(db_path)
    try:
        index = 0
        for namespace in namespaces:
            config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": namespace}}
            checkpoint_tuple = saver.get_tuple(config)
            if checkpoint_tuple is None:
                continue
            channel_values = (checkpoint_tuple.checkpoint or {}).get("channel_values", {}) or {}
            messages = list(channel_values.get("messages", []) or [])
            if not messages:
                continue

            events, user_messages = messages_to_events(
                messages,
                origin=MAIN_ORIGIN if namespace == MAIN_NAMESPACE else SUBAGENT_ORIGIN,
                namespace=namespace,
                start_index=index,
            )
            index = events[-1].index if events else index
            result.events.extend(events)
            if namespace == MAIN_NAMESPACE:
                result.user_messages.extend(user_messages)

            if channel_values.get("_summarization_event"):
                result.warnings.append(
                    f"namespace '{namespace or 'main'}' was compacted; earlier messages live in "
                    "conversation_history/ and are only present if that source is enabled"
                )
    finally:
        connection.close()

    if len(namespaces) > 1:
        result.warnings.append(
            "subagent namespaces are appended after the main thread; checkpoints carry no "
            "cross-namespace ordering, so relative interleaving is not reconstructed"
        )
    return result
