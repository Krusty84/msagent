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

"""Deterministic knowledge-candidate extraction from recorded trajectories.

Everything here is computed by code over the typed trajectory model, without
an LLM: detectors turn recurring session patterns (a failed tool call fixed
by changed arguments, a user correcting the agent, a retry loop, a denied
approval, a procedure shared by several sessions, a skill that should have
been consulted) into :class:`Episode` records whose ``evidence_seq`` points
at real events of the source JSONL. :func:`evidence_score` sums their
weights; below ``min_evidence_score`` (skill evolver config) the LLM stage
is not invoked at all.

Detectors follow their specification literally; known imprecise cases are
documented on each detector and left to the score threshold and the LLM
stage. Stdlib only: importing this module must not load langchain.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, get_args

from msagent.skill_evolver.retrieval import BM25Index
from msagent.trajectory_recorder.model import (
    PRELUDE_RUN_ID,
    ToolCall,
    Trajectory,
    Turn,
)

EpisodeKind = Literal[
    "error_recovery",
    "user_correction",
    "retry_loop",
    "approval_denied",
    "repeated_procedure",
    "skill_gap",
]
EPISODE_KINDS: frozenset[str] = frozenset(get_args(EpisodeKind))
# Version of the detector rules and weights, recorded in the provenance of
# every proposal; bump it when a rule or a weight changes.
FEATURES_VERSION = 1

# Evidence weight of one episode of each kind (0.0..1.0).
EPISODE_WEIGHTS: dict[str, float] = {
    "error_recovery": 0.6,
    "user_correction": 0.9,
    "retry_loop": 0.7,
    "approval_denied": 1.0,
    "repeated_procedure": 1.0,
    "skill_gap": 0.4,
}

# Phrases (ru + en) marking a user message as a correction of the previous
# turn; matched case-insensitively as substrings.
CORRECTION_MARKERS: tuple[str, ...] = (
    "не так",
    "нет,",
    "сначала",
    "надо было",
    "instead",
    "actually",
    "you should have",
    "no, ",
)

# Catalog / introspection tools: not domain work, so they never count as
# evidence that a session did something a skill describes.
CATALOG_TOOLS: frozenset[str] = frozenset(
    {"get_skill", "fetch_skills", "get_tool", "fetch_tools", "run_tool"},
)

# A failed tool call counts as recovered when the same tool succeeds within
# this many subsequent tool calls.
RECOVERY_WINDOW = 5
# retry_loop: at least this many calls of one tool in one turn ...
RETRY_MIN_ATTEMPTS = 3
# ... whose argument key sets are more similar than this (Jaccard).
RETRY_KEY_JACCARD = 0.7
# Tool calls kept as context after a denied approval.
DENIAL_CONTEXT_CALLS = 3
# n-gram sizes mined by :func:`mine_cross_session`.
NGRAM_MIN = 2
NGRAM_MAX = 5
# skill_gap: minimal BM25 score of the best library match. One shared rare
# term scores ln(1 + (N - 0.5) / 1.5) in a library of N skills (0.98 at N=3,
# 1.39 at N=5, 3.5 at N=50), so libraries of four or more skills fire on a
# single distinctive shared term and tiny ones need two.
SKILL_GAP_MIN_SCORE = 1.0
# Truncation limits for values copied into ``Episode.facts``.
VALUE_LIMIT = 200
TEXT_LIMIT = 500

# Denial words looked up in the serialized approval decision as whole words,
# so "note", "unknown" or "no_changes" do not match.
_DENIAL_RE = re.compile(r"\b(?:reject\w*|den(?:y|ied|ies)|no)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Episode:
    """One knowledge candidate mined from a trajectory.

    ``evidence_seq`` lists the ``seq`` of the source events (never empty,
    strictly increasing); ``facts`` holds JSON-safe, kind-specific details.
    """

    kind: EpisodeKind
    thread_id: str
    evidence_seq: list[int]
    tool_sequence: list[str]
    facts: dict[str, Any]
    weight: float

    def __post_init__(self) -> None:
        if self.kind not in EPISODE_KINDS:
            raise ValueError(f"unknown episode kind {self.kind!r}")
        if not self.thread_id:
            raise ValueError(f"{self.kind} episode without thread_id")
        if not self.evidence_seq:
            raise ValueError(f"{self.kind} episode without evidence")
        seqs = self.evidence_seq
        if any(isinstance(s, bool) or not isinstance(s, int) for s in seqs):
            raise ValueError(f"non-int evidence in {self.kind}: {self.evidence_seq!r}")
        pairs = zip(self.evidence_seq, self.evidence_seq[1:])
        if any(earlier >= later for earlier, later in pairs):
            raise ValueError(f"unsorted evidence in {self.kind}: {self.evidence_seq!r}")
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError(f"weight out of range in {self.kind}: {self.weight!r}")


# ------------------------------------------------------------------ helpers


def _flat_calls(traj: Trajectory) -> list[ToolCall]:
    """All tool calls in model order (turns in file order, spans in order).

    ``seq`` restarts when the recorder process restarts mid-thread, so it is
    never used to order calls across turns.
    """
    return [call for turn in traj.turns for call in turn.tool_calls]


def _evidence(*seqs: int | None) -> list[int]:
    """Deduplicate and sort event seqs, dropping ``None`` (orphan spans)."""
    return sorted({seq for seq in seqs if seq is not None})


def _canonical(value: Any) -> str:
    """Stable JSON text used to compare and print argument values."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _clip(value: Any) -> Any:
    """JSON-safe, short copy of a value: text longer than VALUE_LIMIT is cut."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else _canonical(value)
    return text[:VALUE_LIMIT]


def _clip_args(args: dict[str, Any]) -> dict[str, Any]:
    """Tool arguments with every value clipped."""
    return {str(key): _clip(value) for key, value in args.items()}


def _args_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Keys added, removed or changed between two argument dicts (clipped)."""
    added = {key: _clip(new[key]) for key in sorted(new.keys() - old.keys())}
    removed = {key: _clip(old[key]) for key in sorted(old.keys() - new.keys())}
    changed = {
        key: {"old": _clip(old[key]), "new": _clip(new[key])}
        for key in sorted(old.keys() & new.keys())
        if _canonical(old[key]) != _canonical(new[key])
    }
    diff: dict[str, Any] = {}
    if added:
        diff["added"] = added
    if removed:
        diff["removed"] = removed
    if changed:
        diff["changed"] = changed
    return diff


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity of two key sets; two empty sets count as identical."""
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _has_marker(text: str) -> bool:
    """True when a correction marker occurs anywhere in the normalized text."""
    normalized = " ".join(text.split()).casefold()
    return any(marker.casefold() in normalized for marker in CORRECTION_MARKERS)


def _is_denial(decision: Any) -> bool:
    """True when the serialized approval decision contains a denial word.

    Literal rule: whole-word ``reject``/``rejected``/``deny``/``denied``/``no``
    anywhere in the JSON text of the decision. This covers the deepagents
    shape ``{"decisions": [{"type": "reject", ...}]}`` as well as the flat
    ``{"action": "reject"}`` and plain strings; an approval whose free text
    contains the word "no" is reported as a denial too.
    """
    return _DENIAL_RE.search(_canonical(decision)) is not None


def _request_tools(request: Any) -> list[str]:
    """Tool names an interrupt asked approval for (deepagents or flat shape)."""
    if not isinstance(request, dict):
        return []
    actions = request.get("action_requests")
    if isinstance(actions, list):
        names = [action.get("name") for action in actions if isinstance(action, dict)]
        return [str(name) for name in names if name is not None]
    tool = request.get("tool")
    return [str(tool)] if tool is not None else []


def _calls_after(traj: Trajectory, turn_index: int, seq: int) -> list[ToolCall]:
    """Tool calls after event ``seq`` of turn ``turn_index``, in model order.

    Inside the turn, calls are compared by ``seq_start``; every call of the
    later turns follows (a denied approval is recorded in the closed turn
    while the agent's reaction lands in the next ``resume`` turn).
    """
    turn = traj.turns[turn_index]
    following = [call for call in turn.tool_calls if call.seq_start > seq]
    for later in traj.turns[turn_index + 1 :]:
        following.extend(later.tool_calls)
    return following


def _episode(
    kind: EpisodeKind,
    thread_id: str,
    evidence: list[int],
    tool_sequence: list[str],
    facts: dict[str, Any],
) -> Episode:
    return Episode(
        kind=kind,
        thread_id=thread_id,
        evidence_seq=evidence,
        tool_sequence=tool_sequence,
        facts=facts,
        weight=EPISODE_WEIGHTS[kind],
    )


# ---------------------------------------------------------------- detectors


def _detect_error_recovery(traj: Trajectory) -> list[Episode]:
    """A failed tool call followed within RECOVERY_WINDOW calls by a success.

    The knowledge is the argument diff between the failure and the success,
    so a recovery with identical arguments (a transient failure, or calls
    recorded without ``tool.start`` and therefore without arguments) is not
    an episode. The window crosses turn boundaries; orphan spans never match.
    Two failures that share one recovery yield two episodes: two diffs.
    """
    calls = _flat_calls(traj)
    episodes: list[Episode] = []
    for index, failed in enumerate(calls):
        if failed.status != "error":
            continue
        window = calls[index + 1 : index + 1 + RECOVERY_WINDOW]
        for offset, candidate in enumerate(window):
            if candidate.name != failed.name or candidate.status != "ok":
                continue
            diff = _args_diff(failed.args, candidate.args)
            if diff:
                episodes.append(
                    _episode(
                        "error_recovery",
                        traj.thread_id,
                        _evidence(
                            failed.seq_start,
                            failed.seq_end,
                            candidate.seq_start,
                            candidate.seq_end,
                        ),
                        [call.name for call in calls[index : index + offset + 2]],
                        {
                            "tool": failed.name,
                            "error_type": failed.error_type,
                            "error": _clip(failed.error or failed.output_text),
                            "args_diff": diff,
                            "calls_between": offset,
                            "subagent": failed.subagent,
                        },
                    ),
                )
            break
    return episodes


def _detect_user_correction(traj: Trajectory) -> list[Episode]:
    """A user message with a correction marker whose turn uses other tools.

    Each turn is compared with its immediate predecessor. A turn without a
    user message (``resume``, ``unknown``) cannot correct anything, and the
    synthetic prelude turn is never the corrected one. The marker is matched
    as a substring anywhere in the message (literal rule); an empty tool
    list after the correction still differs from a non-empty one before.
    """
    episodes: list[Episode] = []
    for prev, turn in zip(traj.turns, traj.turns[1:]):
        message = turn.user_message
        if prev.run_id == PRELUDE_RUN_ID or message is None:
            continue
        if not _has_marker(message):
            continue
        before = [call.name for call in prev.tool_calls]
        after = [call.name for call in turn.tool_calls]
        if before == after:
            continue
        episodes.append(
            _episode(
                "user_correction",
                traj.thread_id,
                _evidence(prev.seq_start, turn.seq_start),
                after,
                {
                    "correction_text": message[:TEXT_LIMIT],
                    "tools_before": before,
                    "tools_after": after,
                    "run_id_before": prev.run_id,
                    "run_id_after": turn.run_id,
                },
            ),
        )
    return episodes


def _retry_episode(
    traj: Trajectory,
    turn: Turn,
    chain: list[ToolCall],
) -> list[Episode]:
    """The retry_loop episode of a closed chain, if the chain qualifies."""
    if len(chain) < RETRY_MIN_ATTEMPTS:
        return []
    variants: dict[str, dict[str, Any]] = {}
    for call in chain:
        variants.setdefault(_canonical(call.args), _clip_args(call.args))
    if len(variants) < 2:
        return []
    return [
        _episode(
            "retry_loop",
            traj.thread_id,
            _evidence(*(call.seq_start for call in chain)),
            [call.name for call in chain],
            {
                "tool_name": chain[0].name,
                "attempts": len(chain),
                "args_variants": list(variants.values()),
                "statuses": [call.status for call in chain],
                "run_id": turn.run_id,
            },
        ),
    ]


def _detect_retry_loop(traj: Trajectory) -> list[Episode]:
    """Three or more calls of one tool in one turn with similar arguments.

    Calls of a tool are chained while consecutive key sets have a Jaccard
    similarity above RETRY_KEY_JACCARD; a chain of RETRY_MIN_ATTEMPTS or
    more calls with at least two distinct argument sets is an episode. Other
    tools between the attempts and the outcome of each attempt do not matter
    (literal rule), so a fan-out such as reading three different files in
    one turn is reported as well.
    """
    episodes: list[Episode] = []
    for turn in traj.turns:
        chains: dict[str, list[ToolCall]] = {}
        for call in turn.tool_calls:
            chain = chains.setdefault(call.name, [])
            if chain:
                similarity = _jaccard(set(chain[-1].args), set(call.args))
                if similarity <= RETRY_KEY_JACCARD:
                    episodes.extend(_retry_episode(traj, turn, chain))
                    chain.clear()
            chain.append(call)
        for chain in chains.values():
            episodes.extend(_retry_episode(traj, turn, chain))
    return episodes


def _detect_approval_denied(traj: Trajectory) -> list[Episode]:
    """A human denied a tool approval; the next tool calls give the context.

    A denial is the strongest evidence of a boundary the agent must respect.
    The context crosses turn boundaries because the decision is recorded in
    the closed turn while the agent's reaction starts the next one.
    """
    episodes: list[Episode] = []
    for turn_index, turn in enumerate(traj.turns):
        for approval in turn.approvals:
            if not _is_denial(approval.decision):
                continue
            following = _calls_after(traj, turn_index, approval.seq)
            context = following[:DENIAL_CONTEXT_CALLS]
            episodes.append(
                _episode(
                    "approval_denied",
                    traj.thread_id,
                    _evidence(approval.seq, *(call.seq_start for call in context)),
                    [call.name for call in context],
                    {
                        "interrupt_id": approval.interrupt_id,
                        "run_id": approval.run_id or turn.run_id,
                        "tools": _request_tools(approval.request),
                        "request": _clip(approval.request),
                        "decision": approval.decision,
                        "next_tools": [
                            {
                                "name": call.name,
                                "args": _clip_args(call.args),
                                "status": call.status,
                            }
                            for call in context
                        ],
                    },
                ),
            )
    return episodes


def _detect_skill_gap(traj: Trajectory, index: BM25Index) -> list[Episode]:
    """Domain work done without consulting a skill the library describes.

    Fires when tools other than the catalog tools were used, no skill was
    consulted, and the best BM25 match of the user messages plus tool names
    against the library scores at least SKILL_GAP_MIN_SCORE. The candidate is
    a description-fix suggestion for that skill, not a new skill.
    """
    if traj.skills_consulted or len(index) == 0:
        return []
    domain_tools: list[str] = []
    first_call: ToolCall | None = None
    for call in _flat_calls(traj):
        if call.name in CATALOG_TOOLS:
            continue
        if first_call is None:
            first_call = call
        if call.name not in domain_tools:
            domain_tools.append(call.name)
    if first_call is None:
        return []
    turns = [turn for turn in traj.turns if turn.user_message]
    query = "\n".join([turn.user_message or "" for turn in turns] + domain_tools)
    hits = index.search(query, top_k=1)
    if not hits or hits[0].score < SKILL_GAP_MIN_SCORE:
        return []
    hit = hits[0]
    return [
        _episode(
            "skill_gap",
            traj.thread_id,
            _evidence(first_call.seq_start, *(turn.seq_start for turn in turns)),
            domain_tools,
            {
                "candidate_skill": hit.doc.name,
                "score": round(hit.score, 4),
                "matched_terms": list(hit.matched),
                "domain_tools": domain_tools,
            },
        ),
    ]


# --------------------------------------------------------------- public API


def extract_episodes(
    traj: Trajectory,
    *,
    skill_index: BM25Index | None = None,
) -> list[Episode]:
    """Run every per-trajectory detector; ``skill_gap`` needs a skill index."""
    episodes = [
        *_detect_error_recovery(traj),
        *_detect_user_correction(traj),
        *_detect_retry_loop(traj),
        *_detect_approval_denied(traj),
    ]
    if skill_index is not None:
        episodes.extend(_detect_skill_gap(traj, skill_index))
    return episodes


def _is_extended(
    gram: tuple[str, ...],
    frequent: dict[tuple[str, ...], set[str]],
) -> bool:
    """True when a longer frequent n-gram contains ``gram`` with equal support."""
    if len(gram) >= NGRAM_MAX:
        return False
    count = len(frequent[gram])
    return any(
        len(threads) == count and (other[1:] == gram or other[:-1] == gram)
        for other, threads in frequent.items()
        if len(other) == len(gram) + 1
    )


def mine_cross_session(
    trajs: list[Trajectory],
    *,
    min_support: int = 2,
) -> list[Episode]:
    """Tool-name n-grams (NGRAM_MIN..NGRAM_MAX) shared by ``min_support`` threads.

    Support counts distinct ``thread_id`` values, so repetition inside one
    trajectory is not evidence. Only closed patterns are reported: an n-gram
    is dropped when a longer frequent n-gram containing it has the same
    support, so a shared five-step procedure yields one episode, not ten.
    The episode cites the first supporting trajectory; every supporting
    thread is listed in ``facts["thread_ids"]``.
    """
    if min_support < 2:
        raise ValueError(f"min_support must be >= 2, got {min_support}")
    steps_by_thread: dict[str, list[ToolCall]] = {}
    for traj in trajs:
        steps_by_thread.setdefault(traj.thread_id, _flat_calls(traj))
    support: dict[tuple[str, ...], set[str]] = {}
    first_seen: dict[tuple[str, ...], tuple[str, int]] = {}
    for thread_id, steps in steps_by_thread.items():
        names = [call.name for call in steps]
        for size in range(NGRAM_MIN, NGRAM_MAX + 1):
            for start in range(len(names) - size + 1):
                gram = tuple(names[start : start + size])
                support.setdefault(gram, set()).add(thread_id)
                first_seen.setdefault(gram, (thread_id, start))
    frequent = {g: t for g, t in support.items() if len(t) >= min_support}
    episodes: list[Episode] = []
    for gram in sorted(frequent, key=lambda g: (-len(frequent[g]), -len(g), g)):
        if _is_extended(gram, frequent):
            continue
        thread_id, start = first_seen[gram]
        steps = steps_by_thread[thread_id][start : start + len(gram)]
        threads = frequent[gram]
        episodes.append(
            _episode(
                "repeated_procedure",
                thread_id,
                _evidence(*(call.seq_start for call in steps)),
                list(gram),
                {
                    "ngram": list(gram),
                    "support": len(threads),
                    "thread_ids": sorted(threads),
                },
            ),
        )
    return episodes


def evidence_score(episodes: list[Episode]) -> float:
    """Sum of episode weights, compared with the ``min_evidence_score`` config."""
    return float(sum(episode.weight for episode in episodes))
