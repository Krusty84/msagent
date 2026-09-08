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
at real events of the source JSONL.

Rules of FEATURES_VERSION 2:

- Calls are compared inside one *execution context*: a turn group (a turn
  plus the ``resume`` turns that continue it — the only continuation the
  recorder can express) split by subagent. Nothing is matched across a
  following ``dispatch`` turn or between subagents.
- Arguments are compared in normalized form (volatile keys dropped, paths
  and whitespace normalized) and a *work object* — the path or the command
  a call operates on — tells one operation from another.
- Approval decisions are read by structure (:func:`classify_approval`); a
  shape that cannot be read is ``unknown``, never an assumed denial.
- Episodes about the same events share ``anchors`` and form one *incident*.
  :func:`evidence_score` counts the heaviest episode of each incident and
  adds incidents up, so several detectors describing one chain do not pile
  up; :func:`gate_decision` says why a thread passes or skips the
  ``min_evidence_score`` gate. Passing the gate only admits the thread to
  the LLM stage; whether a rule is worth keeping is decided there.

Stdlib only: importing this module must not load langchain.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from msagent.skill_evolver.retrieval import BM25Index
from msagent.trajectory_recorder.model import (
    PRELUDE_RUN_ID,
    ToolCall,
    Trajectory,
    Turn,
)

logger = logging.getLogger(__name__)

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
FEATURES_VERSION = 2

# Gate threshold when the skill evolver config does not set
# min_evidence_score; direct_skill_generation re-exports it. The strong
# correction rule of gate_decision() applies at this threshold or below.
DEFAULT_MIN_EVIDENCE_SCORE = 1.0

# Evidence weight of one episode of each kind (0.0..1.0).
EPISODE_WEIGHTS: dict[str, float] = {
    "error_recovery": 0.6,
    "user_correction": 0.9,
    "retry_loop": 0.7,
    "approval_denied": 1.0,
    "repeated_procedure": 1.0,
    "skill_gap": 0.4,
}
# Weight of a user_correction carrying only a weak marker: an observation
# for the LLM stage that never admits a thread on its own.
WEAK_CORRECTION_WEIGHT = 0.5

# Explicit corrective instructions (ru + en), matched case-insensitively as
# substrings of the user message; with an observed change of the agent's
# actions they make a strong correction.
STRONG_CORRECTION_MARKERS: tuple[str, ...] = (
    "не так",
    "нет,",
    "надо было",
    "нужно было",
    "неправильно",
    "no, ",
    "should have",
    "instead",
)
# Hedges that may or may not correct anything; alone they never make a
# strong correction.
WEAK_CORRECTION_MARKERS: tuple[str, ...] = (
    "actually",
    "сначала",
    "вообще-то",
    "rather",
)
# Negations correct only when they open the message ("Нет, не так");
# elsewhere ("если нет, создай его") they are ordinary text.
OPENING_MARKERS: frozenset[str] = frozenset({"нет,", "no, "})

# Catalog / introspection tools: not domain work, so they never count as
# evidence that a session did something a skill describes and are not steps
# of a shared procedure.
CATALOG_TOOLS: frozenset[str] = frozenset(
    {"get_skill", "fetch_skills", "get_tool", "fetch_tools", "run_tool"},
)

# Argument keys naming the object a call works on, in lookup order. A
# command's working directory is context, not its object, so "cwd" is absent.
PATH_KEYS: tuple[str, ...] = (
    "path",
    "file_path",
    "file",
    "filename",
    "directory",
    "dir",
)
# Argument keys holding a shell command; "input" is the legacy wrapper the
# reader puts around a non-dict tool input.
COMMAND_KEYS: tuple[str, ...] = ("command", "cmd", "script", "input")
# Keys whose change between attempts means "searching again for a result".
SEARCH_KEYS: frozenset[str] = frozenset({"pattern", "query", "regex"})
# Keys that vary without changing what a call does; dropped before comparing.
VOLATILE_KEYS: frozenset[str] = frozenset({"offset", "limit", "timeout", "timeout_ms"})

# Legacy question/options interrupts answer with the chosen option string;
# only these exact answers are read, anything else is unknown.
LEGACY_DENIAL_ANSWERS: frozenset[str] = frozenset(
    {"reject", "rejected", "deny", "denied", "no", "n"},
)
LEGACY_APPROVAL_ANSWERS: frozenset[str] = frozenset(
    {"approve", "approved", "accept", "accepted", "yes", "y", "ok"},
)
# Decision types of the structured (deepagents / langchain HITL) shape.
DENYING_DECISIONS: frozenset[str] = frozenset({"reject"})
ACCEPTING_DECISIONS: frozenset[str] = frozenset({"approve", "edit", "respond"})

# A failed tool call counts as recovered when the same tool succeeds within
# this many subsequent tool calls of the same execution context.
RECOVERY_WINDOW = 5
# retry_loop: at least this many attempts at one operation.
RETRY_MIN_ATTEMPTS = 3
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

# Reasons of :func:`gate_decision`, printed by the dry run.
GATE_NO_EPISODES = "no episodes"
GATE_SCORE_REACHED = "score >= min_evidence_score"
GATE_SCORE_BELOW = "score < min_evidence_score"
GATE_STRONG_CORRECTION = "strong user correction"


@dataclass(frozen=True, slots=True)
class Episode:
    """One knowledge candidate mined from a trajectory.

    ``evidence_seq`` lists the ``seq`` of the source events (never empty,
    strictly increasing); ``facts`` holds JSON-safe, kind-specific details.
    ``anchors`` are keys (``"<run_id>#<seq>"``) of the events the episode is
    *about* — its tool calls, its correcting turn, its approval — never of
    context-only events; episodes sharing an anchor form one incident. A
    trajectory-level observation (``skill_gap``) has no anchors.
    """

    kind: EpisodeKind
    thread_id: str
    evidence_seq: list[int]
    tool_sequence: list[str]
    facts: dict[str, Any]
    weight: float
    anchors: list[str] = field(default_factory=list)

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
        if any(not isinstance(a, str) or not a for a in self.anchors):
            raise ValueError(f"invalid anchors in {self.kind}: {self.anchors!r}")


@dataclass(frozen=True, slots=True)
class ApprovalVerdict:
    """What one recorded approval decision says, read by structure.

    ``denied`` lists the rejected actions as ``{"name", "args"}`` (name may
    be ``None`` when the request names no tool); ``reason`` says how the
    decision was read, or why it could not be.
    """

    status: Literal["denied", "approved", "unknown"]
    denied: list[dict[str, Any]]
    reason: str


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Whether a thread's episodes reach the LLM stage, and why."""

    incidents: list[list[Episode]]
    score: float
    passes: bool
    reason: str


# ------------------------------------------------------------------ helpers

# A tool call with the turn it belongs to: ``ToolCall`` carries no run_id.
_Step = tuple[Turn, ToolCall]


def _flat_calls(traj: Trajectory) -> list[ToolCall]:
    """All tool calls in model order (turns in file order, spans in order).

    ``seq`` restarts when the recorder process restarts mid-thread, so it is
    never used to order calls across turns.
    """
    return [call for turn in traj.turns for call in turn.tool_calls]


def _groups(traj: Trajectory) -> list[list[Turn]]:
    """Turns grouped with their explicit continuations.

    A turn whose ``source`` is ``resume`` (the ``/threads`` path: no user
    message, a fresh run_id and no link field) continues the previous group;
    every other turn (``dispatch``, ``unknown``, ``prelude``) opens one.
    Adjacency plus ``source`` is the only continuation signal recorded.
    """
    groups: list[list[Turn]] = []
    for turn in traj.turns:
        if groups and turn.source == "resume":
            groups[-1].append(turn)
        else:
            groups.append([turn])
    return groups


def _streams(group: list[Turn]) -> list[list[_Step]]:
    """The group's calls split by subagent (``None`` = root), in model order.

    One stream is one execution context; detectors never compare calls of
    different streams.
    """
    streams: dict[str | None, list[_Step]] = {}
    for turn in group:
        for call in turn.tool_calls:
            streams.setdefault(call.subagent, []).append((turn, call))
    return list(streams.values())


def _anchor(run_id: str, seq: int) -> str:
    """Incident key of one event; run_id-scoped because seq restarts per writer."""
    return f"{run_id}#{seq}"


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


def _normalize_args(args: dict[str, Any]) -> dict[str, Any]:
    """Comparison form of tool arguments.

    VOLATILE_KEYS are dropped, whitespace in strings is collapsed and path
    values are normalized (``./a/b`` == ``a/b``); non-string values pass
    through unchanged.
    """
    normalized: dict[str, Any] = {}
    for key, value in args.items():
        if key in VOLATILE_KEYS:
            continue
        if isinstance(value, str):
            value = " ".join(value.split())
            if key in PATH_KEYS and value:
                value = posixpath.normpath(value)
        normalized[str(key)] = value
    return normalized


def _command_object(command: str) -> str:
    """The program of a shell command plus its first non-option token."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return ""
    operands = [token for token in tokens[1:] if not token.startswith("-")]
    return " ".join([tokens[0], *operands[:1]])


def _work_object(call: ToolCall) -> str | None:
    """What one call operates on, or ``None`` when nothing is known.

    The program and first operand of the first command-like argument
    (``msprof --collect train.py`` → ``msprof train.py``; a command wins over
    the directory it runs in); else the first path-like argument
    (normalized); else ``""`` — the tool itself, e.g. a search without a
    path. A call recorded without arguments has no object.
    """
    if not call.args:
        return None
    args = _normalize_args(call.args)
    for key in COMMAND_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            return _command_object(value)
    for key in PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _markers(text: str) -> list[str]:
    """Correction markers found in the normalized text, strong ones first.

    A marker must start a word (``интернет,`` is not ``нет,``), and an
    OPENING_MARKERS negation must start the message.
    """
    normalized = " ".join(text.split()).casefold()
    found: list[str] = []
    for marker in (*STRONG_CORRECTION_MARKERS, *WEAK_CORRECTION_MARKERS):
        prefix = "^" if marker in OPENING_MARKERS else r"(?<!\w)"
        if re.search(prefix + re.escape(marker.casefold()), normalized):
            found.append(marker)
    return found


def _action(entry: Any) -> dict[str, Any]:
    """``{"name", "args"}`` of one requested action.

    Reads a deepagents ``action_requests`` entry (``name`` / ``args``) or a
    flat request (``tool`` / ``args``); anything else names no tool.
    """
    if not isinstance(entry, dict):
        return {"name": None, "args": {}}
    name = entry.get("name", entry.get("tool"))
    args = entry.get("args")
    return {
        "name": None if name is None else str(name),
        "args": _clip_args(args) if isinstance(args, dict) else {},
    }


def _decision_type(entry: Any) -> str | None:
    """Casefolded ``type`` (or ``action``) of one decision dict."""
    if not isinstance(entry, dict):
        return None
    value = entry.get("type", entry.get("action"))
    return value.strip().casefold() if isinstance(value, str) else None


def classify_approval(request: Any, decision: Any) -> ApprovalVerdict:
    """Read one recorded approval decision by its structure.

    - ``{"decisions": [...]}`` is matched by index against
      ``request["action_requests"]`` (the langchain HITL contract); one
      decision without such a list applies to the flat request itself.
      A length mismatch is ``unknown``.
    - A flat ``{"action": ...}`` / ``{"type": ...}`` dict applies to the
      request. ``reject`` denies; ``approve`` / ``edit`` / ``respond`` do not;
      any other type is ``unknown``.
    - A bare string (legacy question/options interrupt) must equal an
      answer of LEGACY_DENIAL_ANSWERS or LEGACY_APPROVAL_ANSWERS; free text
      is ``unknown``.
    - Anything else (``None``, numbers, lists) is ``unknown``.
    """
    if isinstance(decision, str):
        answer = decision.strip().casefold()
        if answer in LEGACY_DENIAL_ANSWERS:
            return ApprovalVerdict("denied", [_action(request)], "legacy answer")
        if answer in LEGACY_APPROVAL_ANSWERS:
            return ApprovalVerdict("approved", [], "legacy answer")
        return ApprovalVerdict("unknown", [], f"unrecognised answer {decision!r}")
    if not isinstance(decision, dict):
        return ApprovalVerdict("unknown", [], f"decision is {type(decision).__name__}")
    if "decisions" in decision:
        entries = decision["decisions"]
        if not isinstance(entries, list):
            return ApprovalVerdict("unknown", [], "decisions is not a list")
        if not entries:
            return ApprovalVerdict("unknown", [], "no decision recorded")
        requested = request.get("action_requests") if isinstance(request, dict) else None
        if isinstance(requested, list):
            actions = requested
        elif len(entries) == 1:
            actions = [request]
        else:
            count = len(entries)
            return ApprovalVerdict("unknown", [], f"{count} decisions without action_requests")
        if len(entries) != len(actions):
            detail = f"{len(entries)} decisions for {len(actions)} action_requests"
            return ApprovalVerdict("unknown", [], detail)
        reason = "structured decisions"
    else:
        entries, actions, reason = [decision], [request], "flat decision"
    denied: list[dict[str, Any]] = []
    for index, (entry, action) in enumerate(zip(entries, actions)):
        kind = _decision_type(entry)
        if kind in DENYING_DECISIONS:
            denied.append(_action(action))
        elif kind not in ACCEPTING_DECISIONS:
            return ApprovalVerdict("unknown", [], f"decision {index} has type {kind!r}")
    return ApprovalVerdict("denied" if denied else "approved", denied, reason)


def _episode(
    kind: EpisodeKind,
    thread_id: str,
    evidence: list[int],
    tool_sequence: list[str],
    facts: dict[str, Any],
    anchors: list[str],
    *,
    weight: float | None = None,
) -> Episode:
    return Episode(
        kind=kind,
        thread_id=thread_id,
        evidence_seq=evidence,
        tool_sequence=tool_sequence,
        facts=facts,
        weight=EPISODE_WEIGHTS[kind] if weight is None else weight,
        anchors=anchors,
    )


# ---------------------------------------------------------------- detectors


def _detect_error_recovery(traj: Trajectory) -> list[Episode]:
    """A failed call fixed by a later ``ok`` call of the same tool.

    The success must come within RECOVERY_WINDOW calls of the same stream
    (one turn group, one subagent): a following ``dispatch`` turn, another
    subagent and an orphan span never recover anything. The knowledge is the
    raw argument diff, so a recovery with identical arguments (a transient
    failure, or calls recorded without ``tool.start``) is not an episode.
    Two failures sharing one recovery yield two episodes: two diffs. Known
    imprecision: the tool name is the only link, so a success of the same
    tool on another work object is reported as a recovery too.
    """
    episodes: list[Episode] = []
    for group in _groups(traj):
        for stream in _streams(group):
            for index, (turn, failed) in enumerate(stream):
                if failed.status != "error":
                    continue
                window = stream[index + 1 : index + 1 + RECOVERY_WINDOW]
                for offset, (later, candidate) in enumerate(window):
                    if candidate.name != failed.name or candidate.status != "ok":
                        continue
                    diff = _args_diff(failed.args, candidate.args)
                    if diff:
                        between = stream[index : index + offset + 2]
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
                                [call.name for _, call in between],
                                {
                                    "tool": failed.name,
                                    "error_type": failed.error_type,
                                    "error": _clip(failed.error or failed.output_text),
                                    "args_diff": diff,
                                    "calls_between": offset,
                                    "subagent": failed.subagent,
                                },
                                [
                                    _anchor(turn.run_id, failed.seq_start),
                                    _anchor(later.run_id, candidate.seq_start),
                                ],
                            ),
                        )
                    break
    return episodes


def _action_changes(before: list[ToolCall], after: list[ToolCall]) -> dict[str, Any] | None:
    """How the agent's actions changed between two turn groups.

    Tools added or removed by name, and for a tool used in both groups the
    diff of its last call before against its first call after, compared in
    normalized form. ``None`` when nothing changed.
    """
    names_before = {call.name for call in before}
    names_after = {call.name for call in after}
    last_before = {call.name: call for call in before}
    first_after: dict[str, ToolCall] = {}
    for call in after:
        first_after.setdefault(call.name, call)
    args_changed: dict[str, Any] = {}
    for name in sorted(names_before & names_after):
        diff = _args_diff(
            _normalize_args(last_before[name].args),
            _normalize_args(first_after[name].args),
        )
        if diff:
            args_changed[name] = diff
    changes = {
        "tools_added": sorted(names_after - names_before),
        "tools_removed": sorted(names_before - names_after),
        "args_changed": args_changed,
    }
    return changes if any(changes.values()) else None


def _detect_user_correction(traj: Trajectory) -> list[Episode]:
    """A user message correcting the previous turn group, with a changed action.

    The message opening a turn group must contain a correction marker, and
    the group's actions must differ from the previous group's: a tool added
    or removed, or a tool used in both groups called with different
    normalized arguments (the diff is recorded). A STRONG_CORRECTION_MARKERS
    phrase makes a strong correction; only WEAK_CORRECTION_MARKERS make a
    weak one with WEAK_CORRECTION_WEIGHT. A marker without an observed change
    is not an episode; no intent recognition is attempted, so the diff shows
    what changed, not that the message caused it. A group without a user
    message cannot correct anything, and the synthetic prelude turn is never
    the corrected one.
    """
    episodes: list[Episode] = []
    groups = _groups(traj)
    for prev, group in zip(groups, groups[1:]):
        head = group[0]
        message = head.user_message
        if prev[0].run_id == PRELUDE_RUN_ID or message is None:
            continue
        markers = _markers(message)
        if not markers:
            continue
        before = [call for turn in prev for call in turn.tool_calls]
        after = [call for turn in group for call in turn.tool_calls]
        changes = _action_changes(before, after)
        if changes is None:
            continue
        strong = any(marker in STRONG_CORRECTION_MARKERS for marker in markers)
        episodes.append(
            _episode(
                "user_correction",
                traj.thread_id,
                _evidence(prev[0].seq_start, head.seq_start),
                [call.name for call in after],
                {
                    "correction_text": message[:TEXT_LIMIT],
                    "strength": "strong" if strong else "weak",
                    "markers": markers,
                    "tools_before": [call.name for call in before],
                    "tools_after": [call.name for call in after],
                    "changes": changes,
                    "run_id_before": prev[0].run_id,
                    "run_id_after": head.run_id,
                },
                [_anchor(head.run_id, head.seq_start)],
                weight=None if strong else WEAK_CORRECTION_WEIGHT,
            ),
        )
    return episodes


def _search_signature(call: ToolCall) -> str:
    """The search keys of a call, canonicalized."""
    args = _normalize_args(call.args)
    return _canonical({key: value for key, value in args.items() if key in SEARCH_KEYS})


def _retry_episode(traj: Trajectory, chain: list[_Step], subject: str) -> list[Episode]:
    """The retry_loop episode of one (tool, work object) chain, if it qualifies."""
    if len(chain) < RETRY_MIN_ATTEMPTS:
        return []
    calls = [call for _, call in chain]
    variants: dict[str, dict[str, Any]] = {}
    for call in calls:
        normalized = _normalize_args(call.args)
        variants.setdefault(_canonical(normalized), _clip_args(normalized))
    if len(variants) < 2:
        return []
    if any(call.status == "error" for call in calls):
        reason = "failed attempt"
    elif len({_search_signature(call) for call in calls}) > 1:
        reason = "search key varies"
    else:
        return []
    return [
        _episode(
            "retry_loop",
            traj.thread_id,
            _evidence(*(call.seq_start for call in calls)),
            [call.name for call in calls],
            {
                "tool_name": calls[0].name,
                "work_object": subject,
                "attempts": len(calls),
                "reason": reason,
                "args_variants": list(variants.values()),
                "statuses": [call.status for call in calls],
                "run_id": chain[0][0].run_id,
            },
            [_anchor(turn.run_id, call.seq_start) for turn, call in chain],
        ),
    ]


def _detect_retry_loop(traj: Trajectory) -> list[Episode]:
    """Three or more attempts at one operation, forced by failure or search.

    Attempts are calls of one tool on one work object inside one stream;
    calls recorded without arguments have no object and never chain.
    A chain of RETRY_MIN_ATTEMPTS or more calls (orphans count as attempts)
    with at least two distinct normalized argument sets is an episode when
    an attempt failed, or when the attempts differ in a search key. Equal
    argument keys alone are not a retry: reading three files is a fan-out,
    and a parameter sweep that never failed is not a loop.
    """
    episodes: list[Episode] = []
    for group in _groups(traj):
        for stream in _streams(group):
            chains: dict[tuple[str, str], list[_Step]] = {}
            for turn, call in stream:
                subject = _work_object(call)
                if subject is None:
                    continue
                chains.setdefault((call.name, subject), []).append((turn, call))
            for (_, subject), chain in chains.items():
                episodes.extend(_retry_episode(traj, chain, subject))
    return episodes


def _detect_approval_denied(traj: Trajectory) -> list[Episode]:
    """A human rejected at least one requested action; the next calls show the reaction.

    The decision is read by :func:`classify_approval`; only ``denied`` makes
    an episode, naming the rejected actions only, and an ``unknown`` shape is
    logged at debug level and ignored. The context is the next
    DENIAL_CONTEXT_CALLS calls inside the approval's turn group — the
    ``resume`` turn that continues an interrupted turn, never a following
    ``dispatch`` turn. Approvals carry no subagent, so the context takes
    calls of any subagent.
    """
    episodes: list[Episode] = []
    for group in _groups(traj):
        for position, turn in enumerate(group):
            for approval in turn.approvals:
                verdict = classify_approval(approval.request, approval.decision)
                if verdict.status == "unknown":
                    logger.debug(
                        "approval %s (seq %s) of thread %s ignored: %s",
                        approval.interrupt_id,
                        approval.seq,
                        traj.thread_id,
                        verdict.reason,
                    )
                if verdict.status != "denied":
                    continue
                following = [call for call in turn.tool_calls if call.seq_start > approval.seq]
                for later in group[position + 1 :]:
                    following.extend(later.tool_calls)
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
                            "tools": [action["name"] for action in verdict.denied if action["name"] is not None],
                            "denied_actions": verdict.denied,
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
                        [_anchor(turn.run_id, approval.seq)],
                    ),
                )
    return episodes


def _detect_skill_gap(traj: Trajectory, index: BM25Index) -> list[Episode]:
    """Domain work done without consulting a skill the library describes.

    Fires when tools other than the catalog tools were used, no skill was
    consulted, and the best BM25 match of the user messages plus tool names
    against the library scores at least SKILL_GAP_MIN_SCORE. The candidate is
    a description-fix suggestion for that skill, not a new skill. It is a
    trajectory-level observation and has no anchors.
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
            [],
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


def _procedure_segments(traj: Trajectory) -> list[list[_Step]]:
    """Runs of ``ok`` domain calls inside one stream: the steps of a procedure.

    Catalog calls are not steps and are skipped; a call that failed or never
    finished ends the segment, so a repeated failure is never mined as a
    procedure. Segments shorter than NGRAM_MIN are dropped.
    """
    segments: list[list[_Step]] = []
    for group in _groups(traj):
        for stream in _streams(group):
            current: list[_Step] = []
            for turn, call in stream:
                if call.name in CATALOG_TOOLS:
                    continue
                if call.status != "ok":
                    if len(current) >= NGRAM_MIN:
                        segments.append(current)
                    current = []
                    continue
                current.append((turn, call))
            if len(current) >= NGRAM_MIN:
                segments.append(current)
    return segments


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

    Steps come from :func:`_procedure_segments`: calls that returned ``ok``,
    in one execution context, catalog calls removed. Support counts distinct
    ``thread_id`` values, so repetition inside one trajectory is not
    evidence. Only closed patterns are reported: an n-gram is dropped when a
    longer frequent n-gram containing it has the same support, so a shared
    five-step procedure yields one episode, not ten. The episode cites the
    first supporting trajectory; every supporting thread is listed in
    ``facts["thread_ids"]``. A shared n-gram says that several sessions
    issued these calls in this order and each returned ``ok`` — not that
    the task succeeded.
    """
    if min_support < 2:
        raise ValueError(f"min_support must be >= 2, got {min_support}")
    segments_by_thread: dict[str, list[list[_Step]]] = {}
    for traj in trajs:
        segments_by_thread.setdefault(traj.thread_id, _procedure_segments(traj))
    support: dict[tuple[str, ...], set[str]] = {}
    first_seen: dict[tuple[str, ...], tuple[str, int, int]] = {}
    for thread_id, segments in segments_by_thread.items():
        for segment_index, segment in enumerate(segments):
            names = [call.name for _, call in segment]
            for size in range(NGRAM_MIN, NGRAM_MAX + 1):
                for start in range(len(names) - size + 1):
                    gram = tuple(names[start : start + size])
                    support.setdefault(gram, set()).add(thread_id)
                    first_seen.setdefault(gram, (thread_id, segment_index, start))
    frequent = {g: t for g, t in support.items() if len(t) >= min_support}
    episodes: list[Episode] = []
    for gram in sorted(frequent, key=lambda g: (-len(frequent[g]), -len(g), g)):
        if _is_extended(gram, frequent):
            continue
        thread_id, segment_index, start = first_seen[gram]
        steps = segments_by_thread[thread_id][segment_index][start : start + len(gram)]
        threads = frequent[gram]
        episodes.append(
            _episode(
                "repeated_procedure",
                thread_id,
                _evidence(*(call.seq_start for _, call in steps)),
                list(gram),
                {
                    "ngram": list(gram),
                    "support": len(threads),
                    "thread_ids": sorted(threads),
                },
                [_anchor(turn.run_id, call.seq_start) for turn, call in steps],
            ),
        )
    return episodes


# ------------------------------------------------------------- incidents


def group_incidents(episodes: list[Episode]) -> list[list[Episode]]:
    """Episodes about the same events, grouped: connected components over anchors.

    An episode without anchors is keyed on its own identity, so an exact
    duplicate joins its twin and nothing else. Incidents are ordered by
    their first episode; episodes keep list order inside an incident.
    """
    parent = list(range(len(episodes)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    owner: dict[str, int] = {}
    for index, episode in enumerate(episodes):
        keys = episode.anchors or [
            f"{episode.kind}@{episode.thread_id}:{episode.evidence_seq}",
        ]
        for key in keys:
            root = find(owner.setdefault(key, index))
            if root != find(index):
                parent[root] = find(index)
    incidents: dict[int, list[Episode]] = {}
    for index, episode in enumerate(episodes):
        incidents.setdefault(find(index), []).append(episode)
    return list(incidents.values())


def _incident_score(incidents: list[list[Episode]]) -> float:
    """Sum of the heaviest weight per incident; rounded so 0.6 + 0.7 + 0.7 reaches 2.0."""
    return round(float(sum(max(episode.weight for episode in incident) for incident in incidents)), 6)


def evidence_score(episodes: list[Episode]) -> float:
    """Sum over incidents of the heaviest episode in each.

    Several detectors describing one chain count once; independent incidents
    add up. Compared with the ``min_evidence_score`` config by
    :func:`gate_decision`.
    """
    return _incident_score(group_incidents(episodes))


def gate_decision(episodes: list[Episode], *, min_score: float) -> GateDecision:
    """Whether the episodes admit their thread to the LLM stage, and why.

    A thread with no episodes never passes. It passes when the incident
    score reaches ``min_score``, or — the strong correction rule — when it
    holds a strong ``user_correction`` and ``min_score`` is at most
    DEFAULT_MIN_EVIDENCE_SCORE: one explicit correction with an observed
    change of action is worth analysing at the default settings, while a
    stricter threshold opts out of the rule.
    """
    incidents = group_incidents(episodes)
    score = _incident_score(incidents)
    if not episodes:
        return GateDecision(incidents, score, False, GATE_NO_EPISODES)
    if score >= min_score:
        return GateDecision(incidents, score, True, GATE_SCORE_REACHED)
    strong = any(
        episode.kind == "user_correction" and episode.facts.get("strength") == "strong" for episode in episodes
    )
    if strong and min_score <= DEFAULT_MIN_EVIDENCE_SCORE:
        return GateDecision(incidents, score, True, GATE_STRONG_CORRECTION)
    return GateDecision(incidents, score, False, GATE_SCORE_BELOW)
