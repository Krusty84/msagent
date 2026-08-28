"""Spot the parts of a trajectory that belong in a skill's ``scripts/`` directory.

The repository's own convention is that deterministic, repeatable logic lives in
``scripts/`` rather than in SKILL.md prose. Three signals mark a command as a
candidate: it ran more than once, it is long or multi-line enough that prose
would mangle it, or it sits inside a run of consecutive shell steps that is
really one procedure.
"""

from __future__ import annotations

import re

from .models import ScriptCandidate, Step

_SHELL_TOOL_RE = re.compile(r"(?i)(^|[_.:\-])(execute|bash|sh|shell|terminal|run_command|command)($|[_.:\-])")
_COMMAND_KEYS = ("command", "cmd", "script", "shell_command")

_LONG_COMMAND_CHARS = 200
_SEQUENCE_LENGTH = 3


def is_shell_step(step: Step) -> bool:
    """Whether a step invokes a shell-like tool."""
    if _SHELL_TOOL_RE.search(step.tool or ""):
        return True
    return any(isinstance(step.args.get(key), str) for key in _COMMAND_KEYS)


def command_text(step: Step) -> str:
    """Extract the command string from a shell step, if it carries one."""
    for key in _COMMAND_KEYS:
        value = step.args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def find_script_candidates(
    steps: list[Step],
    *,
    min_occurrences: int = 2,
) -> list[ScriptCandidate]:
    """Return commands worth extracting into standalone scripts."""
    grouped: dict[str, list[Step]] = {}
    for step in steps:
        if not is_shell_step(step):
            continue
        command = command_text(step)
        if command:
            grouped.setdefault(command, []).append(step)

    candidates: list[ScriptCandidate] = []
    for command, owners in grouped.items():
        occurrences = sum(step.repeat_count for step in owners)
        multiline = "\n" in command
        if occurrences >= min_occurrences:
            reason = "repeated"
        elif multiline:
            reason = "multiline"
        elif len(command) >= _LONG_COMMAND_CHARS:
            reason = "long"
        else:
            continue
        candidates.append(
            ScriptCandidate(
                template=command,
                reason=reason,
                occurrences=occurrences,
                step_indices=[step.index for step in owners],
                multiline=multiline,
            )
        )

    candidates.extend(_find_sequences(steps))
    candidates.sort(key=lambda item: (-item.occurrences, item.step_indices[:1]))
    return candidates


def _find_sequences(steps: list[Step]) -> list[ScriptCandidate]:
    """Report runs of consecutive shell steps as one procedural candidate."""
    sequences: list[ScriptCandidate] = []
    run: list[Step] = []

    def flush() -> None:
        if len(run) < _SEQUENCE_LENGTH:
            return
        commands = [command_text(step) for step in run]
        if not all(commands):
            return
        sequences.append(
            ScriptCandidate(
                template="\n".join(commands),
                reason="sequence",
                occurrences=1,
                step_indices=[step.index for step in run],
                multiline=True,
            )
        )

    for step in steps:
        if is_shell_step(step) and command_text(step):
            run.append(step)
            continue
        flush()
        run = []
    flush()
    return sequences
