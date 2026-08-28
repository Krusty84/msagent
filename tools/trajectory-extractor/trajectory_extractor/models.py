"""Data model for extracted msagent trajectories.

The pipeline moves through three representations:

``RawEvent``
    Flat, source-shaped records emitted by readers in ``sources/``. One per
    user message, assistant message, tool call or tool result.
``Step``
    A paired tool call + tool result, after retry collapsing. This is the unit
    a SKILL.md workflow step is written from.
``TrajectoryDocument``
    The normalized export: steps, phases, parameters, script candidates and
    recovery pairs, ready to feed to a synthesis prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

MAIN_ORIGIN = "main"


def _clean(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` and empty-collection fields to keep exports readable."""
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, (list, dict, str)) and len(value) == 0:
            continue
        result[key] = value
    return result


@dataclass(slots=True)
class RawEvent:
    """One trajectory event as read from a source, before normalization."""

    kind: str  # user | assistant | tool_call | tool_result
    index: int = 0
    origin: str = MAIN_ORIGIN
    namespace: str = ""
    text: str = ""
    tool: str = ""
    call_id: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    duration_ms: int | None = None
    timestamp: str | None = None
    truncated: bool = False

    @property
    def is_subagent(self) -> bool:
        return self.origin != MAIN_ORIGIN


@dataclass(slots=True)
class Step:
    """A tool call together with its result, after retry collapsing."""

    index: int
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    args_raw: dict[str, Any] = field(default_factory=dict)
    origin: str = MAIN_ORIGIN
    namespace: str = ""
    ok: bool = True
    error: str | None = None
    result_preview: str = ""
    result_chars: int = 0
    result_truncated: bool = False
    duration_ms: int | None = None
    repeat_count: int = 1
    source_indices: list[int] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return _clean(
            {
                "index": self.index,
                "tool": self.tool,
                "origin": self.origin,
                "namespace": self.namespace,
                "args": self.args,
                "ok": self.ok,
                "error": self.error,
                "result_preview": self.result_preview,
                "result_chars": self.result_chars or None,
                "result_truncated": self.result_truncated or None,
                "duration_ms": self.duration_ms,
                "repeat_count": self.repeat_count if self.repeat_count > 1 else None,
                "source_indices": self.source_indices,
            }
        )


@dataclass(slots=True)
class Parameter:
    """A concrete value lifted out of the trajectory into a placeholder."""

    placeholder: str
    kind: str
    values: list[str] = field(default_factory=list)
    occurrences: int = 0
    arg_keys: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return _clean(
            {
                "placeholder": self.placeholder,
                "kind": self.kind,
                "values": self.values,
                "occurrences": self.occurrences,
                "arg_keys": self.arg_keys,
            }
        )


@dataclass(slots=True)
class ScriptCandidate:
    """A command worth extracting into the skill's ``scripts/`` directory."""

    template: str
    reason: str
    occurrences: int = 1
    step_indices: list[int] = field(default_factory=list)
    multiline: bool = False

    def to_json(self) -> dict[str, Any]:
        return _clean(
            {
                "template": self.template,
                "reason": self.reason,
                "occurrences": self.occurrences,
                "step_indices": self.step_indices,
                "multiline": self.multiline or None,
            }
        )


@dataclass(slots=True)
class Recovery:
    """A failed step followed by a corrected retry of the same tool.

    These pairs are the raw material for a skill's troubleshooting and
    "do not do this" sections, which successful runs alone never yield.
    """

    tool: str
    failed_step: int
    fixed_step: int
    error: str = ""
    changed_args: list[str] = field(default_factory=list)
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return _clean(
            {
                "tool": self.tool,
                "failed_step": self.failed_step,
                "fixed_step": self.fixed_step,
                "error": self.error,
                "changed_args": self.changed_args,
                "before": self.before,
                "after": self.after,
            }
        )


@dataclass(slots=True)
class Phase:
    """One subagent delegation, read from the audit log."""

    name: str
    task: str = ""
    status: str = ""
    start_time: str = ""
    end_time: str = ""
    duration_ms: int | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return _clean(
            {
                "name": self.name,
                "task": self.task,
                "status": self.status,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "duration_ms": self.duration_ms,
                "input": self.input,
                "output": self.output,
                "error": self.error,
            }
        )


@dataclass(slots=True)
class TrajectoryDocument:
    """Normalized trajectory, ready to hand to a synthesis prompt."""

    thread_id: str = ""
    agent: str = ""
    sources: list[str] = field(default_factory=list)
    user_messages: list[str] = field(default_factory=list)
    offloaded_context: list[str] = field(default_factory=list)
    phases: list[Phase] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    parameters: list[Parameter] = field(default_factory=list)
    script_candidates: list[ScriptCandidate] = field(default_factory=list)
    recoveries: list[Recovery] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def stats(self) -> dict[str, Any]:
        tools: dict[str, int] = {}
        for step in self.steps:
            tools[step.tool] = tools.get(step.tool, 0) + 1
        failed = sum(1 for step in self.steps if not step.ok)
        subagent = sum(1 for step in self.steps if step.origin != MAIN_ORIGIN)
        return {
            "steps": len(self.steps),
            "failed_steps": failed,
            "subagent_steps": subagent,
            "distinct_tools": len(tools),
            "tool_histogram": dict(sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))),
            "phases": len(self.phases),
            "parameters": len(self.parameters),
            "script_candidates": len(self.script_candidates),
            "recoveries": len(self.recoveries),
        }

    def to_json(self) -> dict[str, Any]:
        return _clean(
            {
                "schema_version": SCHEMA_VERSION,
                "thread_id": self.thread_id,
                "agent": self.agent,
                "sources": self.sources,
                "stats": self.stats,
                "user_messages": self.user_messages,
                "offloaded_context": self.offloaded_context,
                "phases": [phase.to_json() for phase in self.phases],
                "steps": [step.to_json() for step in self.steps],
                "parameters": [parameter.to_json() for parameter in self.parameters],
                "script_candidates": [candidate.to_json() for candidate in self.script_candidates],
                "recoveries": [recovery.to_json() for recovery in self.recoveries],
                "warnings": self.warnings,
            }
        )
