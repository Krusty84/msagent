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

"""Tests for the code-only episode detectors (hand-built trajectories + fixtures)."""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, get_args

import pytest

from msagent.skill_evolver.features import (
    EPISODE_WEIGHTS,
    Episode,
    EpisodeKind,
    _is_denial,
    evidence_score,
    extract_episodes,
    mine_cross_session,
)
from msagent.skill_evolver.retrieval import BM25Index, SkillDoc
from msagent.trajectory_recorder.model import (
    PRELUDE_RUN_ID,
    Approval,
    ToolCall,
    ToolStatus,
    Trajectory,
    Turn,
)
from msagent.trajectory_recorder.reader import iter_events, load_trajectory

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "trajectories"
FIXTURE_FILES = sorted(FIXTURES.glob("*.jsonl"))
FIXTURE_IDS = [path.name for path in FIXTURE_FILES]

DOCS = [
    SkillDoc("cluster-analysis", "Run the clustering workflow over profiler data"),
    SkillDoc("dit-quant", "Quantize DiT diffusion models with int8 calibration"),
    SkillDoc("ep-parallel", "Adapt expert parallel models for msmodelslim"),
    SkillDoc(
        "profiling-bottleneck",
        "Profile a training run with msprof and locate the kernel bottleneck",
    ),
]


def _index() -> BM25Index:
    return BM25Index(DOCS)


# ------------------------------------------------------------------ builders


def _call(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    seq: int,
    status: ToolStatus = "ok",
    error_type: str | None = None,
    error: str | None = None,
) -> ToolCall:
    """A tool span starting at ``seq``; its result (if any) sits at ``seq + 1``."""
    return ToolCall(
        span_id=f"s{seq}",
        parent_span_id=None,
        name=name,
        args={} if args is None else args,
        status=status,
        output_text="",
        error_type=error_type,
        error=error,
        duration_ms=1,
        seq_start=seq,
        seq_end=None if status == "orphan" else seq + 1,
        subagent=None,
    )


def _turn(
    run_id: str,
    seq: int,
    message: str | None,
    calls: Sequence[ToolCall] = (),
    *,
    approvals: Sequence[Approval] = (),
    source: str = "dispatch",
) -> Turn:
    return Turn(
        run_id=run_id,
        seq_start=seq,
        user_message=message,
        source=source,
        tool_calls=list(calls),
        approvals=list(approvals),
        status="completed",
    )


def _traj(
    *turns: Turn,
    thread_id: str = "thread-t",
    skills: Sequence[str] = (),
) -> Trajectory:
    return Trajectory(
        path=Path(f"{thread_id}.jsonl"),
        thread_id=thread_id,
        agent="Tester",
        model=None,
        working_dir="/w",
        started_at="2026-09-01T10:00:00.000+00:00",
        turns=list(turns),
        skills_consulted=list(skills),
    )


def _approval(seq: int, decision: Any, request: Any = None) -> Approval:
    return Approval(
        seq=seq,
        run_id=None,
        interrupt_id=f"int-{seq}",
        request=request,
        decision=decision,
    )


def _kinds(episodes: list[Episode]) -> list[str]:
    return [episode.kind for episode in episodes]


# ------------------------------------------------------------------- Episode


def test_episode_rejects_invalid_fields() -> None:
    def make(**overrides: Any) -> Episode:
        fields: dict[str, Any] = {
            "kind": "retry_loop",
            "thread_id": "t",
            "evidence_seq": [1, 2],
            "tool_sequence": ["bash"],
            "facts": {},
            "weight": 0.7,
        }
        fields.update(overrides)
        return Episode(**fields)

    assert make().weight == 0.7
    for overrides, message in (
        ({"evidence_seq": []}, "without evidence"),
        ({"evidence_seq": [3, 3]}, "unsorted"),
        ({"evidence_seq": [5, 2]}, "unsorted"),
        ({"evidence_seq": [1, True]}, "non-int"),
        ({"evidence_seq": [1, "2"]}, "non-int"),
        ({"weight": -0.1}, "out of range"),
        ({"weight": 1.1}, "out of range"),
        ({"kind": "bogus"}, "unknown episode kind"),
        ({"thread_id": ""}, "without thread_id"),
    ):
        with pytest.raises(ValueError, match=message):
            make(**overrides)


def test_episode_weights_cover_all_kinds() -> None:
    assert set(EPISODE_WEIGHTS) == set(get_args(EpisodeKind))
    assert all(0.0 < weight <= 1.0 for weight in EPISODE_WEIGHTS.values())


# ----------------------------------------------------------- error_recovery


def test_error_recovery_positive_reports_args_diff() -> None:
    failed = _call(
        "bash",
        {"cmd": "a", "shell": True},
        seq=4,
        status="error",
        error_type="ToolException",
        error="boom",
    )
    traj = _traj(
        _turn(
            "run-1",
            2,
            "go",
            [
                failed,
                _call("read_file", {"path": "x"}, seq=6),
                _call("bash", {"cmd": "a --fix", "timeout": 30}, seq=8),
            ],
        ),
    )

    (episode,) = extract_episodes(traj)

    assert episode.kind == "error_recovery"
    assert episode.weight == 0.6
    assert episode.thread_id == "thread-t"
    assert episode.evidence_seq == [4, 5, 8, 9]
    assert episode.tool_sequence == ["bash", "read_file", "bash"]
    assert episode.facts["tool"] == "bash"
    assert episode.facts["error_type"] == "ToolException"
    assert episode.facts["error"] == "boom"
    assert episode.facts["calls_between"] == 1
    assert episode.facts["args_diff"] == {
        "added": {"timeout": 30},
        "removed": {"shell": True},
        "changed": {"cmd": {"old": "a", "new": "a --fix"}},
    }


def test_error_recovery_negative_cases() -> None:
    err = _call("bash", {"cmd": "a"}, seq=4, status="error")
    # No successful call of the same tool at all.
    assert extract_episodes(_traj(_turn("run-1", 2, "go", [err]))) == []
    # Identical arguments: a transient failure carries no knowledge.
    same = _traj(_turn("run-1", 2, "go", [err, _call("bash", {"cmd": "a"}, seq=6)]))
    assert extract_episodes(same) == []
    # A different tool succeeding is not a recovery.
    other = _traj(_turn("run-1", 2, "go", [err, _call("grep", {"cmd": "b"}, seq=6)]))
    assert extract_episodes(other) == []
    # An orphan span never recovers anything.
    orphan_call = _call("bash", {"cmd": "b"}, seq=6, status="orphan")
    assert extract_episodes(_traj(_turn("run-1", 2, "go", [err, orphan_call]))) == []


def test_error_recovery_window_is_five_calls() -> None:
    err = _call("bash", {"cmd": "a"}, seq=4, status="error")
    fillers = [_call(f"step{i}", {"n": i}, seq=10 + 2 * i) for i in range(5)]
    fixed = _call("bash", {"cmd": "b"}, seq=30)

    near = _traj(_turn("run-1", 2, "go", [err, *fillers[:4], fixed]))
    within = extract_episodes(near)
    assert _kinds(within) == ["error_recovery"]
    assert within[0].evidence_seq == [4, 5, 30, 31]
    assert within[0].facts["calls_between"] == 4

    far = _traj(_turn("run-1", 2, "go", [err, *fillers, fixed]))
    assert extract_episodes(far) == []


def test_error_recovery_crosses_turns_and_pairs_every_failure() -> None:
    first = _call("bash", {"cmd": "a"}, seq=4, status="error")
    second = _call("bash", {"cmd": "b"}, seq=6, status="error")
    fixed = _call("bash", {"cmd": "c"}, seq=12)
    traj = _traj(
        _turn("run-1", 2, "go", [first, second]),
        _turn("run-2", 10, None, [fixed], source="resume"),
    )

    episodes = extract_episodes(traj)

    assert _kinds(episodes) == ["error_recovery", "error_recovery"]
    evidence = [episode.evidence_seq for episode in episodes]
    assert evidence == [[4, 5, 12, 13], [6, 7, 12, 13]]
    olds = [episode.facts["args_diff"]["changed"]["cmd"]["old"] for episode in episodes]
    assert olds == ["a", "b"]


def test_error_recovery_clips_long_values() -> None:
    err = _call("bash", {"cmd": "x" * 500}, seq=4, status="error")
    fixed = _call("bash", {"cmd": "y" * 500}, seq=6)

    (episode,) = extract_episodes(_traj(_turn("run-1", 2, "go", [err, fixed])))

    changed = episode.facts["args_diff"]["changed"]["cmd"]
    assert (len(changed["old"]), len(changed["new"])) == (200, 200)


# ---------------------------------------------------------- user_correction


@pytest.mark.parametrize(
    "message",
    [
        "Нет, не так — сначала посмотри summary",
        "No, you should have used grep",
        "Actually, run pytest instead",
        "Надо было проверить логи",
    ],
)
def test_user_correction_positive(message: str) -> None:
    traj = _traj(
        _turn("run-1", 2, "do it", [_call("bash", {"cmd": "a"}, seq=4)]),
        _turn("run-2", 10, message, [_call("grep", {"pattern": "b"}, seq=12)]),
    )

    (episode,) = extract_episodes(traj)

    assert episode.kind == "user_correction"
    assert episode.weight == 0.9
    assert episode.evidence_seq == [2, 10]
    assert episode.tool_sequence == ["grep"]
    assert episode.facts == {
        "correction_text": message,
        "tools_before": ["bash"],
        "tools_after": ["grep"],
        "run_id_before": "run-1",
        "run_id_after": "run-2",
    }


def test_user_correction_negative_cases() -> None:
    before = _turn("run-1", 2, "do it", [_call("bash", {"cmd": "a"}, seq=4)])
    # Gratitude is not a correction even when the tools differ.
    thanks = _turn("run-2", 10, "спасибо, всё работает", [_call("grep", {}, seq=12)])
    assert extract_episodes(_traj(before, thanks)) == []
    # A marker without a change of tools is just grumbling.
    grumble = _turn("run-2", 10, "нет, не так", [_call("bash", {"cmd": "a"}, seq=12)])
    assert extract_episodes(_traj(before, grumble)) == []
    # A resumed turn carries no user message and cannot correct anything.
    resume = _turn("run-2", 10, None, [_call("grep", {}, seq=12)], source="resume")
    assert extract_episodes(_traj(before, resume)) == []
    # The very first user turn has no predecessor; the prelude does not count.
    prelude = _turn(PRELUDE_RUN_ID, 1, None, [], source="prelude")
    first = _turn("run-1", 2, "нет, не так", [_call("bash", {}, seq=4)])
    assert extract_episodes(_traj(first)) == []
    assert extract_episodes(_traj(prelude, first)) == []


def test_user_correction_clips_text_and_accepts_marker_anywhere() -> None:
    message = "x" * 600 + " no, do it instead"
    traj = _traj(
        _turn("run-1", 2, "do it", [_call("bash", {}, seq=4)]),
        _turn("run-2", 10, message, []),
    )

    (episode,) = extract_episodes(traj)

    assert len(episode.facts["correction_text"]) == 500
    assert episode.facts["tools_after"] == []
    assert episode.tool_sequence == []


# ---------------------------------------------------------------- retry_loop


def test_retry_loop_positive() -> None:
    calls = [
        _call("grep", {"pattern": "hotspot", "path": "a.csv"}, seq=4),
        _call("read_file", {"path": "a.csv"}, seq=6),
        _call("grep", {"pattern": "hot_spot", "path": "a.csv"}, seq=8),
        _call("grep", {"pattern": "HotSpot", "path": "a.csv"}, seq=10),
    ]

    (episode,) = extract_episodes(_traj(_turn("run-1", 2, "find it", calls)))

    assert episode.kind == "retry_loop"
    assert episode.weight == 0.7
    assert episode.evidence_seq == [4, 8, 10]
    assert episode.tool_sequence == ["grep", "grep", "grep"]
    assert episode.facts["tool_name"] == "grep"
    assert episode.facts["attempts"] == 3
    assert episode.facts["statuses"] == ["ok", "ok", "ok"]
    assert episode.facts["run_id"] == "run-1"
    variants = [variant["pattern"] for variant in episode.facts["args_variants"]]
    assert variants == ["hotspot", "hot_spot", "HotSpot"]


def test_retry_loop_negative_cases() -> None:
    # Two calls are not a loop.
    two = [_call("bash", {"cmd": "a"}, seq=4), _call("bash", {"cmd": "b"}, seq=6)]
    assert extract_episodes(_traj(_turn("run-1", 2, "go", two))) == []
    # Three identical calls differ in no value; so do calls without arguments.
    same = [_call("bash", {"cmd": "a"}, seq=seq) for seq in (4, 6, 8)]
    assert extract_episodes(_traj(_turn("run-1", 2, "go", same))) == []
    empty = [_call("bash", {}, seq=seq) for seq in (4, 6, 8)]
    assert extract_episodes(_traj(_turn("run-1", 2, "go", empty))) == []
    # Dissimilar key sets break the chain (Jaccard 0 between {cmd} and {path}).
    mixed = [
        _call("bash", {"cmd": "a"}, seq=4),
        _call("bash", {"path": "b"}, seq=6),
        _call("bash", {"cmd": "c"}, seq=8),
    ]
    assert extract_episodes(_traj(_turn("run-1", 2, "go", mixed))) == []
    # Attempts spread over two turns do not add up.
    split = _traj(
        _turn("run-1", 2, "go", two),
        _turn("run-2", 10, "more", [_call("bash", {"cmd": "c"}, seq=12)]),
    )
    assert extract_episodes(split) == []


def test_retry_loop_literal_rule_reports_ok_fanout_and_orphans() -> None:
    # Reading three files with one key set is a retry loop by the literal rule
    # (documented false positive); an interrupted attempt still counts.
    calls = [
        _call("read_file", {"path": "c.py"}, seq=4),
        _call("bash", {"cmd": "ls"}, seq=6),
        _call("read_file", {"path": "d.py"}, seq=8),
        _call("read_file", {"path": "e.py"}, seq=10, status="orphan"),
    ]

    (episode,) = extract_episodes(_traj(_turn("run-1", 2, "read", calls)))

    assert episode.evidence_seq == [4, 8, 10]
    assert episode.facts["statuses"] == ["ok", "ok", "orphan"]


# ----------------------------------------------------------- approval_denied


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (
            {
                "decisions": [
                    {"type": "approve"},
                    {"type": "reject", "message": "Rejected by policy."},
                ],
            },
            True,
        ),
        ({"decisions": [{"type": "approve"}]}, False),
        ({"decisions": []}, False),
        ({"action": "reject"}, True),
        ({"action": "approve"}, False),
        ({"type": "edit", "edited_action": {"args": {"cmd": "echo note"}}}, False),
        ("no", True),
        ("No, don't", True),
        ("note", False),
        ("unknown", False),
        ("no_changes", False),
        ("Denied", True),
        ("deny", True),
        ("rejected", True),
        (None, False),
        (42, False),
    ],
)
def test_is_denial(decision: Any, expected: bool) -> None:
    assert _is_denial(decision) is expected


def test_approval_denied_positive_with_real_hitl_shape() -> None:
    request = {
        "action_requests": [
            {"name": "bash", "args": {"cmd": "ls"}},
            {"name": "write_file", "args": {"content": "x" * 300}},
        ],
        "review_configs": [],
    }
    decision = {"decisions": [{"type": "approve"}, {"type": "reject"}]}
    following = [
        _call("bash", {"cmd": "cat x"}, seq=12),
        _call("ls", {"path": "."}, seq=14),
    ]
    traj = _traj(
        _turn(
            "run-1",
            2,
            "write it",
            [_call("grep", {"pattern": "a"}, seq=4)],
            approvals=[_approval(9, decision, request)],
        ),
        _turn("run-2", 10, None, following, source="resume"),
    )

    (episode,) = extract_episodes(traj)

    assert episode.kind == "approval_denied"
    assert episode.weight == 1.0
    assert episode.evidence_seq == [9, 12, 14]
    assert episode.tool_sequence == ["bash", "ls"]
    assert episode.facts["interrupt_id"] == "int-9"
    assert episode.facts["run_id"] == "run-1"
    assert episode.facts["tools"] == ["bash", "write_file"]
    assert episode.facts["decision"] == decision
    assert len(episode.facts["request"]) == 200
    assert episode.facts["next_tools"] == [
        {"name": "bash", "args": {"cmd": "cat x"}, "status": "ok"},
        {"name": "ls", "args": {"path": "."}, "status": "ok"},
    ]


def test_approval_denied_negative_and_context_rules() -> None:
    approve = _approval(9, {"decisions": [{"type": "approve"}]}, {"tool": "bash"})
    before = _call("grep", {}, seq=4)
    approved = _traj(_turn("run-1", 2, "go", [before], approvals=[approve]))
    assert extract_episodes(approved) == []

    denied = _approval(9, {"action": "reject"}, {"tool": "bash", "args": {"cmd": "rm"}})
    # Calls before the approval are excluded; later ones are capped at three.
    later = [_call(f"step{i}", {}, seq=20 + 2 * i) for i in range(4)]
    same_turn = _call("ls", {}, seq=11)
    traj = _traj(
        _turn("run-1", 2, "go", [before, same_turn], approvals=[denied]),
        _turn("run-2", 18, "next", later),
    )

    (episode,) = extract_episodes(traj)

    assert episode.evidence_seq == [9, 11, 20, 22]
    assert episode.tool_sequence == ["ls", "step0", "step1"]
    assert episode.facts["tools"] == ["bash"]

    # Nothing after the denial and an unknown request shape: the approval
    # alone is the evidence and no tool can be named.
    for request in (None, {"question": "Proceed?"}):
        alone = _approval(9, "no", request)
        turn = _turn("run-1", 2, "go", [], approvals=[alone])
        (episode,) = extract_episodes(_traj(turn))
        assert (episode.evidence_seq, episode.tool_sequence) == ([9], [])
        assert episode.facts["tools"] == []


# ----------------------------------------------------------------- skill_gap


def test_skill_gap_positive() -> None:
    calls = [_call("fetch_skills", {}, seq=4), _call("bash", {"cmd": "q"}, seq=6)]
    message = "Quantize the DiT model with int8 calibration"
    traj = _traj(_turn("run-1", 2, message, calls))

    (episode,) = extract_episodes(traj, skill_index=_index())

    assert episode.kind == "skill_gap"
    assert episode.weight == 0.4
    assert episode.evidence_seq == [2, 6]
    assert episode.tool_sequence == ["bash"]
    assert episode.facts["candidate_skill"] == "dit-quant"
    assert episode.facts["score"] >= 1.0
    assert episode.facts["matched_terms"] == ["calibration", "dit", "int8", "quantize"]
    assert episode.facts["domain_tools"] == ["bash"]


def test_skill_gap_negative_cases() -> None:
    message = "Quantize the DiT model with int8 calibration"
    work = [_call("bash", {"cmd": "q"}, seq=4)]
    traj = _traj(_turn("run-1", 2, message, work))
    # No index or an empty index: the detector is off.
    assert extract_episodes(traj) == []
    assert extract_episodes(traj, skill_index=BM25Index([])) == []
    # A consulted skill means no gap.
    consulted = _traj(_turn("run-1", 2, message, work), skills=["dit-quant"])
    assert extract_episodes(consulted, skill_index=_index()) == []
    # Only catalog tools were used: no domain work happened.
    catalog = _traj(_turn("run-1", 2, message, [_call("fetch_skills", {}, seq=4)]))
    assert extract_episodes(catalog, skill_index=_index()) == []
    # Nothing in the library resembles the session, or only weakly.
    unrelated = _traj(_turn("run-1", 2, "Fix the flaky login test", work))
    assert extract_episodes(unrelated, skill_index=_index()) == []
    weak = _traj(_turn("run-1", 2, "Compare the models", work))
    assert extract_episodes(weak, skill_index=_index()) == []


# ------------------------------------------------------- mine_cross_session


def _procedure(thread_id: str, names: Sequence[str], *, seq: int = 4) -> Trajectory:
    calls = [_call(name, {"i": i}, seq=seq + 2 * i) for i, name in enumerate(names)]
    return _traj(_turn("run-1", 2, "go", calls), thread_id=thread_id)


def test_mine_cross_session_reports_closed_patterns() -> None:
    trajs = [
        _procedure("A", ["bash", "read_file", "grep"]),
        _procedure("B", ["bash", "read_file", "grep"], seq=20),
        _procedure("C", ["ls"]),
    ]

    (episode,) = mine_cross_session(trajs)

    assert episode.kind == "repeated_procedure"
    assert episode.weight == 1.0
    assert episode.thread_id == "A"
    assert episode.evidence_seq == [4, 6, 8]
    assert episode.tool_sequence == ["bash", "read_file", "grep"]
    assert episode.facts == {
        "ngram": ["bash", "read_file", "grep"],
        "support": 2,
        "thread_ids": ["A", "B"],
    }


def test_mine_cross_session_negative_cases() -> None:
    # Repetition inside one trajectory is not evidence.
    solo = _procedure("A", ["bash", "grep"] * 3)
    assert mine_cross_session([solo]) == []
    # The same thread recorded twice counts once.
    twin = _procedure("A", ["bash", "grep"], seq=30)
    assert mine_cross_session([solo, twin]) == []
    # Support below min_support, and min_support itself must be at least 2.
    trajs = [_procedure("A", ["bash", "grep"]), _procedure("B", ["bash", "grep"])]
    assert mine_cross_session(trajs, min_support=3) == []
    for bad in (1, 0):
        with pytest.raises(ValueError, match="min_support"):
            mine_cross_session(trajs, min_support=bad)


def test_mine_cross_session_subgram_with_larger_support_survives() -> None:
    trajs = [
        _procedure("A", ["bash", "read_file", "grep"]),
        _procedure("B", ["bash", "read_file", "grep"]),
        _procedure("C", ["bash", "read_file", "ls"]),
    ]

    episodes = mine_cross_session(trajs)

    assert [(e.tool_sequence, e.facts["support"]) for e in episodes] == [
        (["bash", "read_file"], 3),
        (["bash", "read_file", "grep"], 2),
    ]


def test_mine_cross_session_caps_ngrams_at_five() -> None:
    names = ["t1", "t2", "t3", "t4", "t5", "t6", "t7"]
    trajs = [_procedure("A", names), _procedure("B", names)]

    episodes = mine_cross_session(trajs)

    assert [e.tool_sequence for e in episodes] == [names[0:5], names[1:6], names[2:7]]
    assert all(e.facts["support"] == 2 for e in episodes)


# ------------------------------------------------------------------- scoring


def test_evidence_score_sums_weights() -> None:
    assert evidence_score([]) == 0.0
    signals = load_trajectory(FIXTURES / "skill_evolver_signals.jsonl")
    assert evidence_score(extract_episodes(signals)) == pytest.approx(3.8)


# ------------------------------------------------------------------ fixtures


def test_signals_fixture_end_to_end() -> None:
    traj = load_trajectory(FIXTURES / "skill_evolver_signals.jsonl")

    episodes = extract_episodes(traj, skill_index=_index())

    assert [(e.kind, e.evidence_seq) for e in episodes] == [
        ("error_recovery", [4, 5, 10, 11]),
        ("error_recovery", [7, 8, 10, 11]),
        ("user_correction", [2, 17]),
        ("retry_loop", [4, 7, 10]),
        ("approval_denied", [26, 29, 32]),
        ("skill_gap", [2, 4, 17]),
    ]
    recovery, _, correction, retry, denial, gap = episodes
    changed = recovery.facts["args_diff"]["changed"]["cmd"]
    assert changed["old"] == "msprof --collect train.py"
    assert changed["new"] == "msprof --application train.py --output ./prof"
    assert correction.facts["tools_before"] == ["bash", "bash", "bash", "read_file"]
    assert correction.facts["tools_after"] == ["bash", "grep"]
    assert retry.facts["attempts"] == 3
    assert denial.facts["tools"] == ["write_file"]
    assert denial.tool_sequence == ["bash", "ls"]
    assert gap.facts["candidate_skill"] == "profiling-bottleneck"
    assert evidence_score(episodes) == pytest.approx(4.2)


EXPECTED_KINDS = {
    "malformed_lines.jsonl": {"approval_denied": 1},
    "missing_turn_end.jsonl": {"retry_loop": 1, "skill_gap": 1},
    "normal_subagent.jsonl": {},
    "orphan_tool_start.jsonl": {"retry_loop": 1, "skill_gap": 1},
    "recorder_limit.jsonl": {"retry_loop": 1, "skill_gap": 1},
    "result_without_start.jsonl": {},
    "skill_evolver_signals.jsonl": {
        "error_recovery": 2,
        "user_correction": 1,
        "retry_loop": 1,
        "approval_denied": 1,
        "skill_gap": 1,
    },
}


def test_every_fixture_has_expected_kinds() -> None:
    assert sorted(EXPECTED_KINDS) == FIXTURE_IDS


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=FIXTURE_IDS)
def test_fixture_episode_kinds(path: Path) -> None:
    episodes = extract_episodes(load_trajectory(path), skill_index=_index())
    assert Counter(_kinds(episodes)) == Counter(EXPECTED_KINDS[path.name])


def _recorded_seqs(path: Path) -> set[int]:
    """Seqs of the events the reader accepts (its own schema-v1 filter)."""
    seqs: set[int] = set()
    for event in iter_events(path):
        valid = event.get("v") == 1 and isinstance(event.get("event"), str)
        if valid and isinstance(event.get("seq"), int):
            seqs.add(event["seq"])
    return seqs


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=FIXTURE_IDS)
def test_evidence_seqs_exist_in_source(path: Path) -> None:
    traj = load_trajectory(path)
    recorded = _recorded_seqs(path)

    for episode in extract_episodes(traj, skill_index=_index()):
        assert episode.kind in EPISODE_WEIGHTS
        assert episode.thread_id == traj.thread_id
        assert episode.evidence_seq == sorted(set(episode.evidence_seq))
        assert set(episode.evidence_seq) <= recorded
        assert 0.0 <= episode.weight <= 1.0


def test_cross_session_evidence_exists_in_source() -> None:
    trajs = [load_trajectory(path) for path in FIXTURE_FILES]
    recorded = {traj.thread_id: _recorded_seqs(traj.path) for traj in trajs}

    episodes = mine_cross_session(trajs)

    assert episodes
    for episode in episodes:
        assert episode.kind == "repeated_procedure"
        assert episode.facts["support"] == len(episode.facts["thread_ids"]) >= 2
        assert episode.thread_id in episode.facts["thread_ids"]
        assert episode.tool_sequence == episode.facts["ngram"]
        assert 2 <= len(episode.tool_sequence) <= 5
        assert set(episode.evidence_seq) <= recorded[episode.thread_id]
    procedures = {tuple(e.tool_sequence): e for e in episodes}
    shared = procedures[("bash", "read_file", "grep", "bash", "bash")]
    assert shared.facts["thread_ids"] == ["thread-ctrlc", "thread-limit"]
    assert shared.thread_id == "thread-ctrlc"
    assert shared.evidence_seq == [4, 7, 10, 13, 18]
    assert ("bash", "read_file", "grep", "bash") not in procedures


# ----------------------------------------------------------------- isolation

_ISOLATION_PROBE = """
import sys
import msagent.skill_evolver.features as features
import msagent.skill_evolver.retrieval as retrieval
assert features.__file__.startswith(sys.argv[1]), features.__file__
assert retrieval.__file__.startswith(sys.argv[1]), retrieval.__file__
banned = ("langchain", "langgraph", "httpx", "requests", "urllib3", "aiohttp",
          "urllib.request", "http.client")
leaked = sorted(m for m in sys.modules if m.startswith(banned))
assert not leaked, leaked
"""


def test_features_import_no_langchain_or_network() -> None:
    # The pytest process already has langchain loaded (conftest imports the CLI
    # initializer), so the check runs in a fresh interpreter; PYTHONPATH must
    # win over the wheel that may be installed in the virtualenv.
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_PROBE, str(SRC_DIR)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
