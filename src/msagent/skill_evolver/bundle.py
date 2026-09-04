#!/usr/bin/python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This file is part of the MindStudio project.
#
# MindStudio is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#
#    http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
# -------------------------------------------------------------------------

"""Evidence bundle: the only view of a session the classify stage's LLM gets.

:func:`build_evidence_bundle` renders :class:`Episode` records (the output
of the code-only detectors in :mod:`msagent.skill_evolver.features`) into
one markdown block per episode, heaviest first, with the structured facts and
short excerpts of the events the episode cites. There is no transcript and
no chronological narrative: the model sees evidence, not the session. The
seqs listed in the ``Evidence:`` lines are returned as a set, so the classify
stage can reject any candidate citing an event the model never saw.

Stdlib only: importing this module must not load langchain.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from msagent.skill_evolver.features import Episode
from msagent.trajectory_recorder.model import (
    AiMessage,
    Approval,
    ToolCall,
    Trajectory,
    Turn,
)

logger = logging.getLogger(__name__)

# Header of one episode block; ``idx`` is the 1-based rank after sorting.
EPISODE_HEADER = "### Episode E{idx} — {kind} (weight {weight:.2f}, thread {thread})"
# Characters of the thread id shown in the header.
THREAD_ID_CHARS = 8
# Length limits of one rendered fact line and of one excerpt line.
FACT_LIMIT = 800
EXCERPT_LIMIT = 300
# An episode citing more events than EXCERPT_HEAD + EXCERPT_TAIL gets excerpts
# of its first EXCERPT_HEAD and last EXCERPT_TAIL seqs; the rest is counted.
EXCERPT_HEAD = 6
EXCERPT_TAIL = 2
# Marks text that was cut.
ELLIPSIS = "…"

_SEPARATOR = "\n\n"
# ``Turn.source`` of the turns the reader opens itself (``reader._route``) at
# the seq of a real event that had no turn to belong to.
_SYNTHETIC_SOURCES = frozenset({"unknown", "prelude"})


@dataclass(slots=True)
class _Record:
    """What the model sees for one cited ``seq``: a label and its text."""

    label: str
    text: str


_ThreadIndex = dict[int, list[_Record]]


# ------------------------------------------------------------------ helpers


def _json(value: Any) -> str:
    """Stable JSON text of a fact, argument or message value."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _clip(text: str, limit: int) -> str:
    """Whitespace-collapsed copy of ``text``, cut to ``limit`` characters."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - len(ELLIPSIS)] + ELLIPSIS


# ------------------------------------------------------------------ records


def _turn_record(turn: Turn) -> _Record:
    if turn.user_message is None:
        return _Record("turn.start", f"(source={turn.source})")
    return _Record("user", _json(turn.user_message))


def _start_record(call: ToolCall) -> _Record:
    return _Record(f"tool.start {call.name}", _json(call.args))


def _end_record(call: ToolCall) -> _Record:
    if call.status == "error":
        detail = f"{call.error_type or 'error'}: {call.error or ''}"
        return _Record(f"tool.error {call.name} (error)", detail.rstrip(": "))
    label = f"tool.result {call.name} ({call.status})"
    return _Record(label, call.output_text or "(no output)")


def _ai_record(message: AiMessage) -> _Record:
    text = _json(message.text)
    if message.tool_call_names:
        text += f" [tool calls: {', '.join(message.tool_call_names)}]"
    return _Record("ai", text)


def _approval_record(approval: Approval) -> _Record:
    detail = f"request={_json(approval.request)} decision={_json(approval.decision)}"
    return _Record("approval.decision", detail)


def _index_thread(traj: Trajectory) -> _ThreadIndex:
    """Records of one trajectory keyed by ``seq``.

    Tool, AI and approval records go first; a synthetic turn (opened by the
    reader at the seq of a real event) is added only when its seq is still
    free, so it never shadows that event. Several records under one seq mean
    the recorder process restarted mid-thread (``seq`` restarts per writer);
    they are all kept and rendered as ambiguous.
    """
    by_seq: _ThreadIndex = {}
    for turn in traj.turns:
        for call in turn.tool_calls:
            if call.seq_end != call.seq_start:
                by_seq.setdefault(call.seq_start, []).append(_start_record(call))
            if call.seq_end is not None:
                by_seq.setdefault(call.seq_end, []).append(_end_record(call))
        for message in turn.ai_messages:
            by_seq.setdefault(message.seq, []).append(_ai_record(message))
        for approval in turn.approvals:
            by_seq.setdefault(approval.seq, []).append(_approval_record(approval))
    for turn in traj.turns:
        record = _turn_record(turn)
        if turn.source in _SYNTHETIC_SOURCES:
            by_seq.setdefault(turn.seq_start, [record])
        else:
            by_seq.setdefault(turn.seq_start, []).append(record)
    return by_seq


def _index_records(trajectories: list[Trajectory]) -> dict[str, _ThreadIndex]:
    """Per-thread seq index; a thread recorded twice keeps its first copy."""
    index: dict[str, _ThreadIndex] = {}
    for traj in trajectories:
        if traj.thread_id not in index:
            index[traj.thread_id] = _index_thread(traj)
    return index


# ---------------------------------------------------------------- rendering


def _thread_index(index: dict[str, _ThreadIndex], episode: Episode) -> _ThreadIndex:
    """The seq index of the episode's thread; every cited seq must be in it."""
    by_seq = index.get(episode.thread_id)
    if by_seq is None:
        where = f"{episode.kind} episode cites thread {episode.thread_id!r}"
        raise ValueError(f"{where}, which is not among the trajectories")
    missing = [seq for seq in episode.evidence_seq if seq not in by_seq]
    if missing:
        where = f"{episode.kind} episode of thread {episode.thread_id!r}"
        raise ValueError(f"{where} cites seq {missing}, which is not in its trajectory")
    return by_seq


def _excerpt(seq: int, by_seq: _ThreadIndex, thread_id: str) -> str:
    records = by_seq[seq]
    if len(records) > 1:
        logger.warning(
            "bundle: %d records share seq %d in thread %r (recorder restart?)",
            len(records),
            seq,
            thread_id,
        )
        return f"- seq {seq} (ambiguous: {len(records)} records share this seq)"
    record = records[0]
    return f"- seq {seq} {record.label}: {_clip(record.text, EXCERPT_LIMIT)}"


def _render_block(idx: int, episode: Episode, by_seq: _ThreadIndex) -> str:
    """One markdown block: header, evidence seqs, tools, facts, excerpts."""
    seqs = episode.evidence_seq
    lines = [
        EPISODE_HEADER.format(
            idx=idx,
            kind=episode.kind,
            weight=episode.weight,
            thread=episode.thread_id[:THREAD_ID_CHARS],
        ),
        "Evidence: seq " + ", ".join(str(seq) for seq in seqs),
    ]
    if episode.tool_sequence:
        lines.append("Tools: " + _clip(", ".join(episode.tool_sequence), FACT_LIMIT))
    if episode.facts:
        lines.append("Facts:")
        for key, value in episode.facts.items():
            lines.append(f"- {key}: {_clip(_json(value), FACT_LIMIT)}")
    lines.append("Excerpts:")
    shown: list[int | None] = list(seqs)
    omitted = 0
    if len(seqs) > EXCERPT_HEAD + EXCERPT_TAIL:
        omitted = len(seqs) - EXCERPT_HEAD - EXCERPT_TAIL
        shown = [*seqs[:EXCERPT_HEAD], None, *seqs[-EXCERPT_TAIL:]]
    for seq in shown:
        if seq is None:
            lines.append(f"- {ELLIPSIS} {omitted} more events omitted")
        else:
            lines.append(_excerpt(seq, by_seq, episode.thread_id))
    return "\n".join(lines)


# --------------------------------------------------------------- public API


def build_evidence_bundle(
    episodes: list[Episode],
    trajectories: list[Trajectory],
    *,
    max_chars: int = 30000,
) -> tuple[str, set[int]]:
    """Render episodes for the classify stage; return the text and its seqs.

    One block per episode, heaviest first (stable for equal weights). When
    the text would exceed ``max_chars`` the lightest episodes are dropped
    whole, never cut. The returned set holds every seq listed in a kept
    block's ``Evidence:`` line: a classification citing any other seq is
    fabricated. Seqs are unique per thread only, so a bundle mixing threads
    cannot tell the same number apart across them (known limitation).

    Raises ``ValueError`` on a non-positive budget, on an episode whose
    thread is not among ``trajectories`` or whose evidence seq is not in it
    (episodes and trajectories must come from the same data), and when even
    the heaviest episode does not fit the budget.
    """
    if max_chars <= 0:
        raise ValueError(f"max_chars must be positive, got {max_chars}")
    index = _index_records(trajectories)
    ranked = sorted(episodes, key=lambda episode: -episode.weight)
    rendered: list[str] = []
    for idx, episode in enumerate(ranked, start=1):
        rendered.append(_render_block(idx, episode, _thread_index(index, episode)))

    blocks: list[str] = []
    seqs: set[int] = set()
    total = 0
    for episode, block in zip(ranked, rendered):
        cost = len(block) + (len(_SEPARATOR) if blocks else 0)
        if total + cost > max_chars:
            if not blocks:
                need = f"episode E1 ({episode.kind}) needs {len(block)} chars"
                raise ValueError(f"{need}, more than max_chars={max_chars}")
            break
        blocks.append(block)
        seqs.update(episode.evidence_seq)
        total += cost
    return _SEPARATOR.join(blocks), seqs
