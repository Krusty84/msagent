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

"""Tests for the evidence bundle rendered for the classify stage (no LLM)."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from msagent.skill_evolver.bundle import (
    ELLIPSIS,
    EXCERPT_HEAD,
    EXCERPT_LIMIT,
    EXCERPT_TAIL,
    FACT_LIMIT,
    build_evidence_bundle,
)
from msagent.skill_evolver.features import (
    EPISODE_WEIGHTS,
    Episode,
    extract_episodes,
    mine_cross_session,
)
from msagent.skill_evolver.retrieval import BM25Index, SkillDoc
from msagent.trajectory_recorder.model import (
    PRELUDE_RUN_ID,
    AiMessage,
    Approval,
    ToolCall,
    ToolStatus,
    Trajectory,
    Turn,
)
from msagent.trajectory_recorder.reader import load_trajectory

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "trajectories"
FIXTURE_FILES = sorted(FIXTURES.glob("*.jsonl"))
FIXTURE_IDS = [path.name for path in FIXTURE_FILES]
LOGGER = "msagent.skill_evolver.bundle"
HEADER_RE = re.compile(r"^### Episode E(\d+) — (\w+) \(weight (\d\.\d\d), thread ([^)]{1,8})\)$")

DOCS = [
    SkillDoc("cluster-analysis", "Run the clustering workflow over profiler data"),
    SkillDoc("dit-quant", "Quantize DiT diffusion models with int8 calibration"),
    SkillDoc("ep-parallel", "Adapt expert parallel models for msmodelslim"),
    SkillDoc(
        "profiling-bottleneck",
        "Profile a training run with msprof and locate the kernel bottleneck",
    ),
]


# ------------------------------------------------------------------ builders


def _call(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    seq: int,
    status: ToolStatus = "ok",
    output: str = "",
    error_type: str | None = None,
    error: str | None = None,
    seq_end: int | None = -1,
) -> ToolCall:
    """A tool span starting at ``seq``; by default its result sits at ``seq + 1``.

    ``seq_end=-1`` (the default) derives the end from the status; pass an
    explicit value to model a start-less call (``seq_end == seq``).
    """
    if seq_end == -1:
        seq_end = None if status == "orphan" else seq + 1
    return ToolCall(
        span_id=f"s{seq}",
        parent_span_id=None,
        name=name,
        args={} if args is None else args,
        status=status,
        output_text=output,
        error_type=error_type,
        error=error,
        duration_ms=1,
        seq_start=seq,
        seq_end=seq_end,
        subagent=None,
    )


def _turn(
    run_id: str,
    seq: int,
    message: str | None,
    calls: Sequence[ToolCall] = (),
    *,
    ai: Sequence[AiMessage] = (),
    approvals: Sequence[Approval] = (),
    source: str = "dispatch",
) -> Turn:
    return Turn(
        run_id=run_id,
        seq_start=seq,
        user_message=message,
        source=source,
        ai_messages=list(ai),
        tool_calls=list(calls),
        approvals=list(approvals),
        status="completed",
    )


def _traj(*turns: Turn, thread_id: str = "thread-t") -> Trajectory:
    return Trajectory(
        path=Path(f"{thread_id}.jsonl"),
        thread_id=thread_id,
        agent="Tester",
        model=None,
        working_dir="/w",
        started_at="2026-09-01T10:00:00.000+00:00",
        turns=list(turns),
    )


def _ai(seq: int, text: str, tools: Sequence[str] = ()) -> AiMessage:
    return AiMessage(
        seq=seq,
        span_id=f"a{seq}",
        text=text,
        tool_call_names=list(tools),
        usage=None,
        duration_ms=None,
        subagent=None,
    )


def _approval(seq: int, decision: Any, request: Any = None) -> Approval:
    return Approval(
        seq=seq,
        run_id=None,
        interrupt_id=f"int-{seq}",
        request=request,
        decision=decision,
    )


def _episode(
    kind: str,
    seqs: Sequence[int],
    *,
    thread_id: str = "thread-t",
    tools: Sequence[str] = ("bash",),
    facts: dict[str, Any] | None = None,
) -> Episode:
    return Episode(
        kind=kind,  # type: ignore[arg-type]
        thread_id=thread_id,
        evidence_seq=list(seqs),
        tool_sequence=list(tools),
        facts={"tool": "bash"} if facts is None else facts,
        weight=EPISODE_WEIGHTS[kind],
    )


def _sample() -> Trajectory:
    """seq 2 user turn; 4/5 failed bash; 6 ai; 8/9 ok bash; 12 approval."""
    calls = [
        _call(
            "bash",
            {"cmd": "make"},
            seq=4,
            status="error",
            error_type="CalledProcessError",
            error="exit 2\n  missing dep",
        ),
        _call("bash", {"cmd": "make deps && make"}, seq=8, output="ok  built\n\n42 targets"),
    ]
    turn = _turn(
        "run-1",
        2,
        "please   build it",
        calls,
        ai=[_ai(6, "I will build", ["bash"])],
        approvals=[_approval(12, {"decisions": [{"type": "reject"}]}, {"tool": "rm"})],
    )
    return _traj(turn)


def _blocks(text: str) -> list[str]:
    return text.split("\n\n")


def _headers(text: str) -> list[re.Match[str]]:
    matches = [HEADER_RE.match(block.splitlines()[0]) for block in _blocks(text)]
    assert all(matches), text
    return matches  # type: ignore[return-value]


# ------------------------------------------------------------------ ordering


def test_blocks_ordered_by_weight_and_numbered() -> None:
    episodes = [
        _episode("skill_gap", [2, 4]),  # 0.4
        _episode("approval_denied", [8, 12]),  # 1.0
        _episode("error_recovery", [4, 5, 8, 9]),  # 0.6
    ]

    text, seqs = build_evidence_bundle(episodes, [_sample()])

    headers = _headers(text)
    assert [m.group(1, 2, 3) for m in headers] == [
        ("1", "approval_denied", "1.00"),
        ("2", "error_recovery", "0.60"),
        ("3", "skill_gap", "0.40"),
    ]
    assert {m.group(4) for m in headers} == {"thread-t"}
    assert seqs == {2, 4, 5, 8, 9, 12}


def test_equal_weights_keep_input_order() -> None:
    episodes = [_episode("retry_loop", [4, 8]), _episode("retry_loop", [2, 4])]

    text, _ = build_evidence_bundle(episodes, [_sample()])

    first, second = _blocks(text)
    assert first.splitlines()[1] == "Evidence: seq 4, 8"
    assert second.splitlines()[1] == "Evidence: seq 2, 4"


def test_thread_id_is_shortened_in_header() -> None:
    traj = _traj(_turn("run-1", 2, "hi"), thread_id="thread-0123456789")
    episode = _episode("skill_gap", [2], thread_id="thread-0123456789")

    text, _ = build_evidence_bundle([episode], [traj])

    assert text.splitlines()[0] == "### Episode E1 — skill_gap (weight 0.40, thread thread-0)"


# -------------------------------------------------------------------- budget


def test_budget_drops_whole_episodes_from_the_end() -> None:
    traj = _sample()
    light = _episode("skill_gap", [2, 4])
    heavy = _episode("approval_denied", [8, 12])
    full, full_seqs = build_evidence_bundle([light, heavy], [traj])
    first_block, second_block = _blocks(full)
    assert full_seqs == {2, 4, 8, 12}

    exact, seqs = build_evidence_bundle([light, heavy], [traj], max_chars=len(full))
    assert exact == full
    assert seqs == full_seqs

    trimmed, seqs = build_evidence_bundle([light, heavy], [traj], max_chars=len(full) - 1)
    assert trimmed == first_block
    assert seqs == {8, 12}  # only the kept episode's seqs are citable
    assert second_block not in trimmed


def test_first_block_too_large_raises() -> None:
    with pytest.raises(ValueError, match=r"episode E1 \(approval_denied\) needs \d+ chars"):
        build_evidence_bundle([_episode("approval_denied", [12])], [_sample()], max_chars=10)


@pytest.mark.parametrize("max_chars", [0, -1])
def test_non_positive_budget_raises(max_chars: int) -> None:
    with pytest.raises(ValueError, match="max_chars must be positive"):
        build_evidence_bundle([], [_sample()], max_chars=max_chars)


def test_empty_episodes_give_empty_bundle() -> None:
    assert build_evidence_bundle([], [_sample()]) == ("", set())
    assert build_evidence_bundle([], []) == ("", set())


# ------------------------------------------------------------------ excerpts


def test_excerpts_come_from_the_cited_records() -> None:
    episode = _episode("error_recovery", [2, 4, 5, 6, 8, 9, 12], facts={"tool": "bash", "calls_between": 2})

    text, seqs = build_evidence_bundle([episode], [_sample()])

    lines = text.splitlines()
    assert seqs == {2, 4, 5, 6, 8, 9, 12}
    assert lines[1] == "Evidence: seq 2, 4, 5, 6, 8, 9, 12"
    assert lines[2] == "Tools: bash"
    assert lines[3:6] == ["Facts:", '- tool: "bash"', "- calls_between: 2"]
    assert lines[6] == "Excerpts:"
    assert lines[7:] == [
        '- seq 2 user: "please build it"',
        '- seq 4 tool.start bash: {"cmd": "make"}',
        "- seq 5 tool.error bash (error): CalledProcessError: exit 2 missing dep",
        '- seq 6 ai: "I will build" [tool calls: bash]',
        '- seq 8 tool.start bash: {"cmd": "make deps && make"}',
        "- seq 9 tool.result bash (ok): ok built 42 targets",
        '- seq 12 approval.decision: request={"tool": "rm"} decision={"decisions": [{"type": "reject"}]}',
    ]


def test_turn_without_message_orphan_and_empty_output() -> None:
    turn = _turn(
        "run-2",
        20,
        None,
        [_call("grep", {"q": "x"}, seq=22, status="orphan"), _call("ls", seq=24, output="")],
        source="resume",
    )
    episode = _episode("retry_loop", [20, 22, 24, 25], tools=[])

    text, _ = build_evidence_bundle([episode], [_traj(turn)])

    lines = text.splitlines()
    assert "Tools:" not in text
    assert lines[-4:] == [
        "- seq 20 turn.start: (source=resume)",
        '- seq 22 tool.start grep: {"q": "x"}',
        "- seq 24 tool.start ls: {}",
        "- seq 25 tool.result ls (ok): (no output)",
    ]


def test_start_less_call_has_only_a_result_record() -> None:
    turn = _turn("run-1", 2, "go", [_call("bash", seq=4, seq_end=4, output="done")])

    text, _ = build_evidence_bundle([_episode("retry_loop", [4])], [_traj(turn)])

    assert text.splitlines()[-1] == "- seq 4 tool.result bash (ok): done"


def test_excerpt_and_fact_clipping() -> None:
    turn = _turn("run-1", 2, "word " * 400)
    long_decision = {"comment": "x" * 5000}
    episode = _episode("approval_denied", [2], facts={"decision": long_decision})

    text, _ = build_evidence_bundle([episode], [_traj(turn)])

    fact_line, excerpt_line = text.splitlines()[-3], text.splitlines()[-1]
    assert fact_line.startswith('- decision: {"comment": "xxx')
    assert fact_line.endswith(ELLIPSIS)
    assert len(fact_line) == len("- decision: ") + FACT_LIMIT
    assert excerpt_line.startswith('- seq 2 user: "word word word')
    assert excerpt_line.endswith(ELLIPSIS)
    assert len(excerpt_line) == len("- seq 2 user: ") + EXCERPT_LIMIT


def test_excerpt_count_is_capped_but_evidence_line_is_complete() -> None:
    turns = [_turn(f"run-{i}", 2 * i, f"message {i}") for i in range(1, 21)]
    seqs = [turn.seq_start for turn in turns]
    episode = _episode("skill_gap", seqs)

    text, valid = build_evidence_bundle([episode], [_traj(*turns)])

    lines = text.splitlines()
    assert valid == set(seqs)
    assert lines[1] == "Evidence: seq " + ", ".join(str(seq) for seq in seqs)
    excerpts = lines[lines.index("Excerpts:") + 1 :]
    assert len(excerpts) == EXCERPT_HEAD + 1 + EXCERPT_TAIL
    omitted = 20 - EXCERPT_HEAD - EXCERPT_TAIL
    assert excerpts[EXCERPT_HEAD] == f"- {ELLIPSIS} {omitted} more events omitted"
    assert excerpts[0] == '- seq 2 user: "message 1"'
    assert excerpts[-1] == '- seq 40 user: "message 20"'


# ---------------------------------------------------------------- collisions


@pytest.mark.parametrize(("run_id", "source"), [("run-x", "unknown"), (PRELUDE_RUN_ID, "prelude")])
def test_synthetic_turn_never_shadows_the_real_record(run_id: str, source: str) -> None:
    # The reader opens such turns at the seq of the first event it routes there.
    turn = _turn(run_id, 4, None, [_call("bash", {"cmd": "ls"}, seq=4)], source=source)

    text, _ = build_evidence_bundle([_episode("retry_loop", [4, 5])], [_traj(turn)])

    assert text.splitlines()[-2:] == [
        '- seq 4 tool.start bash: {"cmd": "ls"}',
        "- seq 5 tool.result bash (ok): (no output)",
    ]


def test_shared_seq_is_rendered_as_ambiguous(caplog: pytest.LogCaptureFixture) -> None:
    # Two writers (recorder restart) both counted up to seq 4 in one thread.
    first = _turn("run-1", 2, "one", [_call("bash", {"n": 1}, seq=4)])
    second = _turn("run-2", 2, "two", [_call("grep", {"n": 2}, seq=4)])
    episode = _episode("retry_loop", [2, 4])

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        text, seqs = build_evidence_bundle([episode], [_traj(first, second)])

    assert seqs == {2, 4}
    assert text.splitlines()[-2:] == [
        "- seq 2 (ambiguous: 2 records share this seq)",
        "- seq 4 (ambiguous: 2 records share this seq)",
    ]
    assert "2 records share seq 4 in thread 'thread-t'" in caplog.text


def test_duplicate_thread_keeps_the_first_copy() -> None:
    first = _traj(_turn("run-1", 2, "first copy"))
    second = _traj(_turn("run-1", 2, "second copy"))

    text, _ = build_evidence_bundle([_episode("skill_gap", [2])], [first, second])

    assert text.splitlines()[-1] == '- seq 2 user: "first copy"'


# -------------------------------------------------------------------- errors


def test_unknown_thread_raises() -> None:
    episode = _episode("skill_gap", [2], thread_id="thread-other")

    with pytest.raises(ValueError, match="cites thread 'thread-other', which is not among"):
        build_evidence_bundle([episode], [_sample()])


def test_unresolved_seq_raises_even_beyond_the_excerpt_cap() -> None:
    turns = [_turn(f"run-{i}", 2 * i, f"message {i}") for i in range(1, 21)]
    seqs = [turn.seq_start for turn in turns]
    seqs[10] = 21  # in the omitted middle, still validated
    episode = _episode("skill_gap", seqs)

    with pytest.raises(ValueError, match=r"cites seq \[21\], which is not in its trajectory"):
        build_evidence_bundle([episode], [_traj(*turns)])


def test_unresolved_seq_is_reported_regardless_of_budget() -> None:
    light = _episode("skill_gap", [2, 99])
    heavy = _episode("approval_denied", [12])

    with pytest.raises(ValueError, match=r"cites seq \[99\]"):
        build_evidence_bundle([light, heavy], [_sample()], max_chars=10**6)


# ------------------------------------------------------------------ fixtures


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=FIXTURE_IDS)
def test_fixture_episodes_render(path: Path) -> None:
    traj = load_trajectory(path)
    episodes = extract_episodes(traj, skill_index=BM25Index(DOCS))

    text, seqs = build_evidence_bundle(episodes, [traj])

    assert seqs == {seq for episode in episodes for seq in episode.evidence_seq}
    assert len(_blocks(text)) == (len(episodes) if episodes else 1)
    assert text.count("### Episode E") == len(episodes)
    for episode in episodes:
        evidence = ", ".join(str(seq) for seq in episode.evidence_seq)
        assert f"Evidence: seq {evidence}" in text


def test_cross_session_episodes_render() -> None:
    trajs = [load_trajectory(path) for path in FIXTURE_FILES]
    episodes = mine_cross_session(trajs)
    assert episodes  # the fixtures share procedures (see test_features)

    text, seqs = build_evidence_bundle(episodes, trajs)

    assert seqs == {seq for episode in episodes for seq in episode.evidence_seq}
    assert text.count("### Episode E") == len(episodes)
    assert all(header.group(2) == "repeated_procedure" for header in _headers(text))
