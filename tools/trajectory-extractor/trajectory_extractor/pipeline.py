"""Assemble a normalized trajectory from msagent's recorded state.

Order matters here. Redaction runs before parameterization so placeholders are
derived from already-clean values; retries collapse before parameterization so
the same work is not scanned five times; recoveries are detected on collapsed
steps so a run of identical failures reads as one problem, not five.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import RawEvent, TrajectoryDocument
from .normalize import build_steps, collapse_retries, detect_recoveries
from .parameterize import parameterize
from .redact import redact_text, redact_value
from .scripts import find_script_candidates
from .sources import resolve_project
from .sources.audit import find_audit_file, list_audit_threads, read_audit
from .sources.base import SourceResult
from .sources.checkpoint import CheckpointUnavailable, list_threads, read_checkpoint
from .sources.history import history_path, read_history
from .sources.trace import read_trace

ALL_SOURCES = ("checkpoint", "audit", "history", "trace")


@dataclass(slots=True)
class ExtractionRequest:
    """Everything the pipeline needs to produce one trajectory document."""

    working_dir: Path
    thread_id: str | None = None
    sources: tuple[str, ...] = ALL_SOURCES
    trace_file: Path | None = None
    home: Path | None = None
    redact: bool = True
    include_subagents: bool = True
    min_script_occurrences: int = 2
    parameterize_results: bool = True
    result_preview_chars: int = 2000
    extras: dict[str, Any] = field(default_factory=dict)

    def wants(self, source: str) -> bool:
        return source in self.sources


def discover_threads(request: ExtractionRequest) -> list[dict[str, Any]]:
    """List the threads recorded for a working directory."""
    location = resolve_project(request.working_dir, home=request.home)
    audit_threads = {thread_id: agent for thread_id, agent, _path in list_audit_threads(location.audit_dir)}

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for info in list_threads(location.checkpoints_db):
        seen.add(info.thread_id)
        rows.append(
            {
                "thread_id": info.thread_id,
                "agent": audit_threads.get(info.thread_id, ""),
                "namespaces": list(info.namespaces),
                "latest_checkpoint_id": info.latest_checkpoint_id,
                "has_audit": info.thread_id in audit_threads,
            }
        )

    for thread_id, agent in audit_threads.items():
        if thread_id in seen:
            continue
        rows.append(
            {
                "thread_id": thread_id,
                "agent": agent,
                "namespaces": [],
                "latest_checkpoint_id": "",
                "has_audit": True,
            }
        )
    return rows


def _resolve_thread_id(request: ExtractionRequest, location) -> tuple[str, list[str]]:
    warnings: list[str] = []
    if request.thread_id:
        return request.thread_id, warnings

    threads = list_threads(location.checkpoints_db)
    if threads:
        chosen = threads[0].thread_id
        warnings.append(f"no --thread-id given; using most recent checkpoint thread '{chosen}'")
        return chosen, warnings

    audit_threads = list_audit_threads(location.audit_dir)
    if audit_threads:
        chosen = audit_threads[-1][0]
        warnings.append(f"no --thread-id given; using audit thread '{chosen}'")
        return chosen, warnings

    return "", warnings


def _redact_events(events: list[RawEvent]) -> None:
    for event in events:
        if event.text:
            event.text = redact_text(event.text)
        if event.args:
            event.args = redact_value(event.args)


def _collect(request: ExtractionRequest) -> tuple[list[SourceResult], str, list[str]]:
    location = resolve_project(request.working_dir, home=request.home)
    thread_id, warnings = _resolve_thread_id(request, location)
    if not location.used_msagent:
        warnings.append("msagent is not importable; project path resolved with the local fallback")

    results: list[SourceResult] = []

    if request.wants("checkpoint") and thread_id:
        try:
            results.append(
                read_checkpoint(
                    location.checkpoints_db,
                    thread_id,
                    include_subagents=request.include_subagents,
                )
            )
        except CheckpointUnavailable as exc:
            warnings.append(str(exc))

    if request.wants("audit") and thread_id:
        audit_file = find_audit_file(location.audit_dir, thread_id)
        if audit_file is not None:
            results.append(read_audit(audit_file))
        else:
            warnings.append(f"no audit file for thread '{thread_id}' (audit_log disabled for this agent?)")

    if request.wants("history") and thread_id:
        results.append(read_history(history_path(location.conversation_history_dir, thread_id)))

    if request.wants("trace") and request.trace_file is not None:
        results.append(read_trace(request.trace_file))

    return results, thread_id, warnings


def extract(request: ExtractionRequest) -> TrajectoryDocument:
    """Run the full extraction pipeline and return the normalized document."""
    results, thread_id, warnings = _collect(request)

    document = TrajectoryDocument(thread_id=thread_id)
    document.warnings.extend(warnings)

    checkpoint_events: list[RawEvent] = []
    trace_events: list[RawEvent] = []

    for result in results:
        document.sources.append(result.name)
        document.warnings.extend(result.warnings)
        if result.agent and not document.agent:
            document.agent = result.agent
        if result.thread_id and not document.thread_id:
            document.thread_id = result.thread_id
        for message in result.user_messages:
            if message not in document.user_messages:
                document.user_messages.append(message)
        document.phases.extend(result.phases)
        document.offloaded_context.extend(result.offloaded_context)

        if result.name.startswith("checkpoint:"):
            checkpoint_events.extend(result.events)
        elif result.name.startswith("trace:"):
            trace_events.extend(result.events)

    if checkpoint_events and trace_events:
        document.warnings.append(
            "both checkpoint and trace produced events; using checkpoints (untruncated) and "
            "ignoring the trace event stream"
        )
    events = checkpoint_events or trace_events
    if not events:
        document.warnings.append("no tool events found; steps will be empty")

    if request.redact:
        _redact_events(events)
        document.user_messages = [redact_text(message) for message in document.user_messages]
        document.offloaded_context = [redact_text(block) for block in document.offloaded_context]
        for phase in document.phases:
            phase.task = redact_text(phase.task)
            phase.input = redact_value(phase.input) if phase.input else phase.input
            phase.output = redact_value(phase.output) if phase.output else phase.output
            phase.error = redact_value(phase.error) if phase.error else phase.error

    steps = collapse_retries(build_steps(events))
    for step in steps:
        if len(step.result_preview) > request.result_preview_chars:
            step.result_preview = step.result_preview[: request.result_preview_chars] + "..."
            step.result_truncated = True

    document.parameters = parameterize(steps, include_results=request.parameterize_results)
    document.steps = steps
    document.script_candidates = find_script_candidates(steps, min_occurrences=request.min_script_occurrences)
    document.recoveries = detect_recoveries(steps)
    return document
