"""Read conversation history that automatic compaction offloaded to disk.

When a session crosses its compaction threshold, msagent summarizes the early
messages and appends the originals to
``conversation_history/<thread_id>.md`` as ``get_buffer_string`` output under an
``## Offloaded at <timestamp>`` heading.

Without this source, the opening of any long session -- which is exactly where
the task is stated and the approach chosen -- is missing from the trajectory.
The buffer format is lossy (no tool-call arguments survive it), so sections are
kept as context text rather than being reconstructed into steps.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import SourceResult

_SECTION_RE = re.compile(r"^## Offloaded at (?P<timestamp>.+)$", re.MULTILINE)

# `get_buffer_string` prefixes each message with its role. Only the known role
# labels end a block, so a line such as "Note: ..." inside a message body does
# not split it.
_ROLE_LABELS = "Human|AI|Assistant|Tool|Function|System"
_HUMAN_BLOCK_RE = re.compile(
    rf"^Human:[ \t]*(?P<text>.*?)(?=^(?:{_ROLE_LABELS}):|\Z)",
    re.MULTILINE | re.DOTALL,
)


def history_path(conversation_history_dir: Path, thread_id: str) -> Path:
    return conversation_history_dir / f"{thread_id}.md"


def read_history(path: Path) -> SourceResult:
    """Parse offloaded conversation history into context blocks."""
    result = SourceResult(name=f"history:{path.name}")
    if not path.is_file():
        return result

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return result

    boundaries = [match.start() for match in _SECTION_RE.finditer(text)]
    if not boundaries:
        sections = [text]
    else:
        edges = boundaries + [len(text)]
        sections = [text[edges[i] : edges[i + 1]] for i in range(len(boundaries))]

    for section in sections:
        stripped = section.strip()
        if not stripped:
            continue
        result.offloaded_context.append(stripped)
        for match in _HUMAN_BLOCK_RE.finditer(stripped):
            message = match.group("text").strip()
            if message:
                result.user_messages.append(message)

    if result.offloaded_context:
        result.warnings.append(
            f"{len(result.offloaded_context)} compacted section(s) recovered as plain text; "
            "tool-call arguments do not survive offloading and are not reconstructed as steps"
        )
    return result
