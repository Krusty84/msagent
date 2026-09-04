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

"""Tests for the proposal writer and for proposals staying out of every scanner."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from msagent.skill_evolver.classify import Candidate
from msagent.skill_evolver.features import FEATURES_VERSION, Episode, extract_episodes
from msagent.skill_evolver.writer import (
    PROPOSALS_DIR,
    REQUIRED_PROVENANCE_KEYS,
    batch_dir_name,
    build_provenance,
    write_proposal,
)
from msagent.skills.factory import SkillFactory
from msagent.trajectory_recorder.reader import iter_events, load_trajectory

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "trajectories" / "skill_evolver_signals.jsonl"
THREAD_ID = "thread-signals"
NAME = "build-before-test"

SKILL = "\n".join(
    [
        "---",
        f"name: {NAME}",
        "description: Use when the test suite needs generated code.",
        "---",
        "",
        "# Build before test",
        "",
        "## Inputs",
        "",
        "The repository.",
        "",
        "## Workflow",
        "",
        "1. Run make.",
        "2. Run the tests.",
        "",
        "## Outputs",
        "",
        "A green run.",
        "",
    ]
)


# ------------------------------------------------------------------ builders


def _candidate(**overrides: Any) -> Candidate:
    data: dict[str, Any] = {
        "title": "Build before test",
        "rule": "Run make before invoking the test suite.",
        "evidence_refs": [4, 5],
        "future_applicability": "high",
        "target": {"action": "create", "existing_skill": None},
    }
    data.update(overrides)
    return Candidate.model_validate(data)


def _episode(seq: list[int], thread_id: str = THREAD_ID) -> Episode:
    return Episode(
        kind="error_recovery",
        thread_id=thread_id,
        evidence_seq=seq,
        tool_sequence=["bash"],
        facts={"tool": "bash"},
        weight=0.6,
    )


def _provenance(**overrides: Any) -> dict[str, Any]:
    data = build_provenance(
        thread_ids=[THREAD_ID],
        episodes=[_episode([4, 5])],
        candidates=[_candidate()],
        model="fake-model",
        prompt_variants={"classify": "classify/prompt_v1.md", "render": "render/prompt_v1.md"},
        category="default",
        target={"action": "create", "existing_skill": None, "existing_path": None},
    )
    data.update(overrides)
    return data


def _write(root: Path, **kwargs: Any) -> Path:
    args: dict[str, Any] = {"root": root, "name": NAME, "provenance": _provenance(), "thread_id": THREAD_ID}
    args.update(kwargs)
    return write_proposal(SKILL, **args)


def _recorded_seqs(path: Path) -> set[int]:
    """Seqs of the events the reader accepts (its own schema-v1 filter)."""
    seqs: set[int] = set()
    for event in iter_events(path):
        valid = event.get("v") == 1 and isinstance(event.get("event"), str)
        if valid and isinstance(event.get("seq"), int):
            seqs.add(event["seq"])
    return seqs


def _real_skill(skill_dir: Path, name: str) -> Path:
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: Use when testing.\n---\nbody\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- provenance


def test_build_provenance_maps_episodes_and_candidates() -> None:
    episodes = [_episode([4, 5]), _episode([7, 8], thread_id="thread-other")]
    candidate = _candidate(target={"action": "update", "existing_skill": "real"})

    provenance = build_provenance(
        thread_ids=[THREAD_ID, "thread-other", THREAD_ID],
        episodes=episodes,
        candidates=[candidate],
        model="fake-model",
        prompt_variants={"classify": "c", "render": "r"},
        category="profiler",
        target={"action": "update", "existing_skill": "real", "existing_path": "/x"},
        generated_at="2026-09-04T10:00:00+00:00",
    )

    assert provenance["thread_ids"] == [THREAD_ID, "thread-other"]
    assert provenance["episodes"] == [
        {"kind": "error_recovery", "weight": 0.6, "evidence_seq": [4, 5], "thread_id": THREAD_ID},
        {"kind": "error_recovery", "weight": 0.6, "evidence_seq": [7, 8], "thread_id": "thread-other"},
    ]
    (stored,) = provenance["candidates"]
    assert stored["target"] == {"action": "update", "existing_skill": "real"}
    assert stored["evidence_refs"] == [4, 5]
    assert provenance["features_version"] == FEATURES_VERSION == 1
    assert provenance["generated_at"] == "2026-09-04T10:00:00+00:00"
    assert provenance["prompt_variants"] == {"classify": "c", "render": "r"}
    assert provenance["category"] == "profiler"
    assert provenance["target"]["existing_path"] == "/x"
    assert REQUIRED_PROVENANCE_KEYS <= set(provenance)


def test_build_provenance_stamps_utc_time() -> None:
    stamp = datetime.fromisoformat(_provenance()["generated_at"])

    assert stamp.tzinfo is not None
    assert stamp.utcoffset().total_seconds() == 0


# -------------------------------------------------------------------- writer


def test_write_proposal_writes_skill_and_provenance(tmp_path: Path) -> None:
    root = tmp_path / "skills"

    path = _write(root)

    assert path == root / PROPOSALS_DIR / THREAD_ID / NAME / "SKILL.md"
    assert path.read_text(encoding="utf-8") == SKILL
    provenance = json.loads((path.parent / "provenance.json").read_text(encoding="utf-8"))
    assert REQUIRED_PROVENANCE_KEYS <= set(provenance)
    assert provenance["features_version"] == 1
    assert sorted(p.name for p in path.parent.iterdir()) == ["SKILL.md", "provenance.json"]
    assert not (root / "default").exists()


def test_write_proposal_adds_exactly_one_trailing_newline(tmp_path: Path) -> None:
    path = _write(tmp_path)
    stripped = write_proposal(
        SKILL.rstrip(), root=tmp_path, name="other-name", provenance=_provenance(), thread_id=THREAD_ID
    )

    assert path.read_text(encoding="utf-8") == SKILL
    assert stripped.read_text(encoding="utf-8") == SKILL


def test_write_proposal_suffixes_collisions(tmp_path: Path) -> None:
    root = tmp_path / "skills"

    paths = [_write(root) for _ in range(3)]

    assert [p.parent.name for p in paths] == [NAME, f"{NAME}-2", f"{NAME}-3"]
    assert all(p.is_file() and (p.parent / "provenance.json").is_file() for p in paths)
    assert {p.parent.parent for p in paths} == {root / PROPOSALS_DIR / THREAD_ID}


def test_write_proposal_skips_half_written_dir(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    (root / PROPOSALS_DIR / THREAD_ID / NAME).mkdir(parents=True)

    assert _write(root).parent.name == f"{NAME}-2"


def test_batch_dir_name_sanitizes_thread_id() -> None:
    assert batch_dir_name("thread-signals") == "thread-signals"
    assert batch_dir_name("thread/../evil id") == "thread-..-evil-id"
    assert len(batch_dir_name("a" * 200)) == 64


@pytest.mark.parametrize("thread_id", ["", "..", "---", "/", " "])
def test_batch_dir_name_rejects_unsafe(thread_id: str) -> None:
    with pytest.raises(ValueError, match="unsafe thread id"):
        batch_dir_name(thread_id)


@pytest.mark.parametrize("name", ["../x", "Foo", "", "ab", "a b"])
def test_write_proposal_rejects_unsafe_name(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="unsafe proposal name"):
        _write(tmp_path, name=name)
    assert not (tmp_path / PROPOSALS_DIR).exists()


def test_write_proposal_rejects_unsafe_thread_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe thread id"):
        _write(tmp_path, thread_id="..")
    assert not (tmp_path / PROPOSALS_DIR).exists()


def test_write_proposal_requires_provenance_keys(tmp_path: Path) -> None:
    provenance = _provenance()
    del provenance["episodes"]
    del provenance["generated_at"]

    with pytest.raises(ValueError, match=r"missing \['episodes', 'generated_at'\]"):
        _write(tmp_path, provenance=provenance)
    assert not (tmp_path / PROPOSALS_DIR).exists()


@pytest.mark.parametrize("key", ["thread_ids", "candidates"])
def test_write_proposal_requires_threads_and_candidates(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError, match="thread_ids and candidates"):
        _write(tmp_path, provenance=_provenance(**{key: []}))
    assert not (tmp_path / PROPOSALS_DIR).exists()


def test_write_proposal_rejects_non_json_provenance(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        _write(tmp_path, provenance=_provenance(model=object()))
    assert not (tmp_path / PROPOSALS_DIR).exists()


# ---------------------------------------------------------- scanner guarantee


@pytest.mark.asyncio
async def test_proposals_not_scanned_by_skill_factory(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _real_skill(root / "cat" / "real", "real")
    _real_skill(root / "flat", "flat")
    _real_skill(root / ".hidden" / "deep" / "x" / "hidden-skill", "hidden-skill")
    _write(root)

    factory = SkillFactory()
    for skills_dir in (root, [root]):
        loaded = await factory.load_skills(skills_dir)

        names = sorted(skill.name for category in loaded.values() for skill in category.values())
        assert names == ["flat", "real"]
        assert factory.get_module_map() == {"cat:real": "cat", "default:flat": "default"}


@pytest.mark.asyncio
async def test_dot_base_dir_still_loads(tmp_path: Path) -> None:
    root = tmp_path / ".msagent" / "skills"
    _real_skill(root / "cat" / "s", "s")

    loaded = await SkillFactory().load_skills(root)

    assert "s" in loaded["cat"]


def test_proposals_invisible_to_agent_factory_sources(tmp_path: Path) -> None:
    # Read-only use of the middleware side: the skill sources deepagents scans.
    from msagent.agents.factory import AgentFactory

    root = tmp_path / "skills"
    _real_skill(root / "cat" / "real", "real")
    _write(root)

    sources = AgentFactory._resolve_existing_paths([root])

    assert sources == [str(root / "cat")]


# ------------------------------------------------------------- acceptance


def test_provenance_evidence_seq_subset_of_source(tmp_path: Path) -> None:
    trajectory = load_trajectory(FIXTURE)
    episodes = extract_episodes(trajectory)
    assert episodes
    provenance = build_provenance(
        thread_ids=[trajectory.thread_id],
        episodes=episodes,
        candidates=[_candidate(evidence_refs=list(episodes[0].evidence_seq))],
        model="fake-model",
        prompt_variants={"classify": "c", "render": "r"},
        category="default",
        target={"action": "create", "existing_skill": None, "existing_path": None},
    )

    path = write_proposal(
        SKILL, root=tmp_path / "skills", name=NAME, provenance=provenance, thread_id=trajectory.thread_id
    )

    stored = json.loads((path.parent / "provenance.json").read_text(encoding="utf-8"))
    recorded = _recorded_seqs(FIXTURE)
    assert stored["thread_ids"] == [THREAD_ID]
    assert len(stored["episodes"]) == len(episodes)
    for episode in stored["episodes"]:
        assert episode["thread_id"] == THREAD_ID
        assert episode["evidence_seq"]
        assert set(episode["evidence_seq"]) <= recorded
    for candidate in stored["candidates"]:
        assert set(candidate["evidence_refs"]) <= recorded
