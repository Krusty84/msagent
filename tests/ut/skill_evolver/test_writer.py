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

from msagent.skill_evolver.bundle import BundleEpisode, EvidenceBundle, ShownFragment, build_evidence_bundle
from msagent.skill_evolver.classify import Candidate
from msagent.skill_evolver.features import FEATURES_VERSION, Episode, EvidenceItem, extract_episodes
from msagent.skill_evolver.writer import (
    PROPOSALS_DIR,
    PROVENANCE_VERSION,
    REQUIRED_PROVENANCE_KEYS,
    batch_dir_name,
    build_provenance,
    write_proposal,
)
from msagent.skills.factory import SkillFactory
from msagent.trajectory_recorder.model import EvidenceRef
from msagent.trajectory_recorder.reader import load_trajectory

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "trajectories"
FIXTURE = FIXTURES / "skill_evolver_signals.jsonl"
THREAD_ID = "thread-signals"
SOURCE = f"{THREAD_ID}.jsonl"
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
        "evidence_refs": ["ev1", "ev2"],
        "future_applicability": "high",
        "target": {"action": "create", "existing_skill": None},
        "candidate_id": "c1",
    }
    data.update(overrides)
    return Candidate.model_validate(data)


def _ref(seq: int, source: str = SOURCE) -> EvidenceRef:
    return EvidenceRef(source=source, line=seq, seq=seq)


def _episode(seqs: list[int], thread_id: str = THREAD_ID, *, optional: list[int] = ()) -> Episode:
    source = f"{thread_id}.jsonl"
    return Episode(
        kind="error_recovery",
        thread_id=thread_id,
        source=source,
        evidence=[EvidenceItem(_ref(seq, source), "event", seq not in optional) for seq in seqs],
        tool_sequence=["bash"],
        facts={"tool": "bash"},
        weight=0.6,
    )


def _fragment(fragment_id: str, seq: int, text: str, *, required: bool = True) -> ShownFragment:
    return ShownFragment(id=fragment_id, ref=_ref(seq), role="event", required=required, text=text)


def _bundle() -> EvidenceBundle:
    """One shown episode citing seq 4 and 5 as fragments ev1 and ev2."""
    shown = {
        "ev1": _fragment("ev1", 4, 'tool.start bash: {"cmd": "make"}'),
        "ev2": _fragment("ev2", 5, "tool.error bash (error): exit 2"),
    }
    return EvidenceBundle("(text)", shown, [BundleEpisode(_episode([4, 5]), "shown")])


def _provenance(**overrides: Any) -> dict[str, Any]:
    candidate = _candidate()
    data = build_provenance(
        thread_ids=[THREAD_ID],
        bundle=_bundle(),
        candidates=[candidate],
        rejected=[],
        sources={SOURCE: f"/trajectories/{SOURCE}"},
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


def _seq_at(path: Path, line: int) -> int:
    """The ``seq`` written on physical ``line`` of ``path`` (read like the reader does)."""
    with path.open(encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            if number == line:
                return json.loads(raw)["seq"]
    raise AssertionError(f"{path} has no line {line}")


def _real_skill(skill_dir: Path, name: str) -> Path:
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: Use when testing.\n---\nbody\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- provenance


def test_build_provenance_maps_episodes_candidates_and_evidence() -> None:
    shown_episode = _episode([4, 5, 6], optional=[6])
    excluded = _episode([7, 8], thread_id="thread-other")
    shown = {
        "ev1": _fragment("ev1", 4, 'tool.start bash: {"cmd": "make"}'),
        "ev2": _fragment("ev2", 5, "tool.error bash (error): exit 2"),
    }
    bundle = EvidenceBundle(
        "(text)",
        shown,
        [BundleEpisode(shown_episode, "trimmed"), BundleEpisode(excluded, "excluded")],
    )
    kept = _candidate(
        target={"action": "update", "existing_skill": "real"},
        applies_when="the build is stale",
        constraints=["run make once"],
        expected_outcome="green tests",
    )
    rejected = _candidate(title="Fabricated", evidence_refs=["ev1", "ev9"], candidate_id="")

    provenance = build_provenance(
        thread_ids=[THREAD_ID, "thread-other", THREAD_ID],
        bundle=bundle,
        candidates=[kept],
        rejected=[(rejected, "evidence not shown in the bundle: ['ev9']")],
        sources={SOURCE: "/t/a.jsonl", "thread-other.jsonl": "/t/b.jsonl"},
        model="fake-model",
        prompt_variants={"classify": "c", "render": "r"},
        category="profiler",
        target={"action": "update", "existing_skill": "real", "existing_path": "/x"},
        generated_at="2026-09-04T10:00:00+00:00",
    )

    assert provenance["provenance_version"] == PROVENANCE_VERSION == 3
    assert provenance["features_version"] == FEATURES_VERSION == 3
    assert provenance["thread_ids"] == [THREAD_ID, "thread-other"]
    assert provenance["sources"] == {SOURCE: "/t/a.jsonl", "thread-other.jsonl": "/t/b.jsonl"}
    # Extracted episodes with their bundle outcome; unshown events have no id.
    assert provenance["episodes"] == [
        {
            "kind": "error_recovery",
            "weight": 0.6,
            "thread_id": THREAD_ID,
            "source": SOURCE,
            "bundle_status": "trimmed",
            "evidence": [
                {"id": "ev1", "source": SOURCE, "line": 4, "seq": 4, "role": "event", "required": True},
                {"id": "ev2", "source": SOURCE, "line": 5, "seq": 5, "role": "event", "required": True},
                {"id": None, "source": SOURCE, "line": 6, "seq": 6, "role": "event", "required": False},
            ],
        },
        {
            "kind": "error_recovery",
            "weight": 0.6,
            "thread_id": "thread-other",
            "source": "thread-other.jsonl",
            "bundle_status": "excluded",
            "evidence": [
                {"id": None, "source": "thread-other.jsonl", "line": 7, "seq": 7, "role": "event", "required": True},
                {"id": None, "source": "thread-other.jsonl", "line": 8, "seq": 8, "role": "event", "required": True},
            ],
        },
    ]
    # Exactly what the classify model saw, by id.
    assert provenance["evidence_shown"] == {
        "ev1": {
            "source": SOURCE,
            "line": 4,
            "seq": 4,
            "role": "event",
            "required": True,
            "text": 'tool.start bash: {"cmd": "make"}',
        },
        "ev2": {
            "source": SOURCE,
            "line": 5,
            "seq": 5,
            "role": "event",
            "required": True,
            "text": "tool.error bash (error): exit 2",
        },
    }
    (stored,) = provenance["candidates"]
    assert stored["target"] == {"action": "update", "existing_skill": "real"}
    assert stored["evidence_refs"] == ["ev1", "ev2"]
    assert stored["candidate_id"] == "c1"
    assert (stored["applies_when"], stored["constraints"], stored["expected_outcome"]) == (
        "the build is stale",
        ["run make once"],
        "green tests",
    )
    assert provenance["candidates_rejected"] == [
        {"title": "Fabricated", "reason": "evidence not shown in the bundle: ['ev9']", "evidence_refs": ["ev1", "ev9"]}
    ]
    # What the renderer was quoted, per rendered candidate.
    assert provenance["render_evidence"] == {"c1": ["ev1", "ev2"]}
    assert provenance["generated_at"] == "2026-09-04T10:00:00+00:00"
    assert provenance["prompt_variants"] == {"classify": "c", "render": "r"}
    assert provenance["category"] == "profiler"
    assert provenance["target"]["existing_path"] == "/x"
    assert REQUIRED_PROVENANCE_KEYS <= set(provenance)


def test_build_provenance_stamps_utc_time() -> None:
    stamp = datetime.fromisoformat(_provenance()["generated_at"])

    assert stamp.tzinfo is not None
    assert stamp.utcoffset().total_seconds() == 0


def _two_plan_bundle() -> EvidenceBundle:
    """One shown episode over seq 4..7 as fragments ev1..ev4."""
    shown = {
        "ev1": _fragment("ev1", 4, 'tool.start bash: {"cmd": "make"}'),
        "ev2": _fragment("ev2", 5, "tool.error bash (error): exit 2"),
        "ev3": _fragment("ev3", 6, "tool.start pytest: only-b-sees-this"),
        "ev4": _fragment("ev4", 7, "tool.result pytest (ok): nobody-cites-this"),
    }
    return EvidenceBundle("(text)", shown, [BundleEpisode(_episode([4, 5, 6, 7]), "shown")])


def _scoped(candidates: list[Candidate]) -> dict[str, Any]:
    return build_provenance(
        thread_ids=[THREAD_ID],
        bundle=_two_plan_bundle(),
        candidates=candidates,
        rejected=[],
        sources={SOURCE: "/t/a.jsonl"},
        model="fake-model",
        prompt_variants={"classify": "c", "render": "r"},
        category="default",
        target={"action": "create", "existing_skill": None, "existing_path": None},
    )


def test_build_provenance_is_scoped_to_the_rendered_plan() -> None:
    a = _candidate(candidate_id="c1", evidence_refs=["ev1", "ev2"])
    b = _candidate(title="Other", rule="Only B says so.", candidate_id="c2", evidence_refs=["ev3"])

    for_a = _scoped([a])
    for_b = _scoped([b])

    assert set(for_a["evidence_shown"]) == {"ev1", "ev2"}
    assert [c["candidate_id"] for c in for_a["candidates"]] == ["c1"]
    assert for_a["render_evidence"] == {"c1": ["ev1", "ev2"]}
    # The episode row stays complete; events this plan does not cite have no id.
    assert [i["id"] for i in for_a["episodes"][0]["evidence"]] == ["ev1", "ev2", None, None]
    dumped = json.dumps(for_a, ensure_ascii=False)
    assert "Only B says so." not in dumped and "only-b-sees-this" not in dumped

    assert set(for_b["evidence_shown"]) == {"ev3"}
    assert [c["candidate_id"] for c in for_b["candidates"]] == ["c2"]
    assert for_b["render_evidence"] == {"c2": ["ev3"]}
    assert [i["id"] for i in for_b["episodes"][0]["evidence"]] == [None, None, "ev3", None]
    assert '"cmd": "make"' not in json.dumps(for_b, ensure_ascii=False)


def test_build_provenance_ignores_refs_outside_the_registry() -> None:
    provenance = _scoped([_candidate(candidate_id="c1", evidence_refs=["ev1", "ev9"])])

    assert set(provenance["evidence_shown"]) == {"ev1"}
    assert provenance["render_evidence"] == {"c1": ["ev1"]}


# -------------------------------------------------------------------- writer


def test_write_proposal_writes_skill_and_provenance(tmp_path: Path) -> None:
    root = tmp_path / "skills"

    path = _write(root)

    assert path == root / PROPOSALS_DIR / THREAD_ID / NAME / "SKILL.md"
    assert path.read_text(encoding="utf-8") == SKILL
    provenance = json.loads((path.parent / "provenance.json").read_text(encoding="utf-8"))
    assert REQUIRED_PROVENANCE_KEYS <= set(provenance)
    assert provenance["features_version"] == 3
    assert provenance["provenance_version"] == 3
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
    del provenance["evidence_shown"]
    del provenance["provenance_version"]

    with pytest.raises(
        ValueError, match=r"missing \['episodes', 'evidence_shown', 'generated_at', 'provenance_version'\]"
    ):
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


@pytest.mark.parametrize("fixture", ["skill_evolver_signals.jsonl", "malformed_lines.jsonl"])
def test_provenance_shown_fragments_resolve_to_source_lines(tmp_path: Path, fixture: str) -> None:
    # Real detectors, real bundle; the written provenance must let a reader
    # go from every candidate to the fragments the model saw and from every
    # fragment to the physical line that holds that very event — even with
    # corrupted lines in between (malformed_lines.jsonl).
    source = FIXTURES / fixture
    trajectory = load_trajectory(source)
    episodes = extract_episodes(trajectory)
    assert episodes
    bundle = build_evidence_bundle(episodes, [trajectory])
    cited = sorted(bundle.shown)[:2]
    kept = _candidate(evidence_refs=cited, applies_when="the tool fails on the first attempt")
    rejected = _candidate(title="Fabricated", evidence_refs=["ev999"], candidate_id="")
    provenance = build_provenance(
        thread_ids=[trajectory.thread_id],
        bundle=bundle,
        candidates=[kept],
        rejected=[(rejected, "evidence not shown in the bundle: ['ev999']")],
        sources={trajectory.source: str(trajectory.path)},
        model="fake-model",
        prompt_variants={"classify": "c", "render": "r"},
        category="default",
        target={"action": "create", "existing_skill": None, "existing_path": None},
    )

    path = write_proposal(
        SKILL, root=tmp_path / "skills", name=NAME, provenance=provenance, thread_id=trajectory.thread_id
    )

    stored = json.loads((path.parent / "provenance.json").read_text(encoding="utf-8"))
    assert stored["provenance_version"] == 3
    assert stored["thread_ids"] == [trajectory.thread_id]
    assert stored["sources"] == {source.name: str(source)}
    shown = stored["evidence_shown"]
    # Scoped to what the rendered candidate cites, not the whole registry.
    assert set(shown) == set(cited) <= set(bundle.shown)
    for fragment_id, entry in shown.items():
        assert entry["source"] == source.name
        assert _seq_at(source, entry["line"]) == entry["seq"]
        assert f"- [{fragment_id}] {entry['text']}" in bundle.text
    assert len(stored["episodes"]) == len(episodes)
    for episode in stored["episodes"]:
        assert episode["thread_id"] == trajectory.thread_id
        assert episode["bundle_status"] == "shown"
        for item in episode["evidence"]:
            if item["id"] is None:
                continue
            assert item["id"] in shown
            assert (shown[item["id"]]["source"], shown[item["id"]]["line"]) == (item["source"], item["line"])
    resolved = {item["id"] for episode in stored["episodes"] for item in episode["evidence"]} - {None}
    assert resolved == set(cited)
    (candidate,) = stored["candidates"]
    assert candidate["candidate_id"] == "c1"
    assert candidate["evidence_refs"] == cited
    assert set(candidate["evidence_refs"]) <= set(shown)
    assert candidate["applies_when"] == "the tool fails on the first attempt"
    assert stored["candidates_rejected"] == [
        {"title": "Fabricated", "reason": "evidence not shown in the bundle: ['ev999']", "evidence_refs": ["ev999"]}
    ]
    assert set(stored["render_evidence"]) == {"c1"}
    assert set(stored["render_evidence"]["c1"]) <= set(cited)
