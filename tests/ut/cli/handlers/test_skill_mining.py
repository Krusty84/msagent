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

"""Unit tests for /skill-mine and /skill-review."""

from __future__ import annotations

import json
import shutil
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from rich.console import Console

# The handlers package must be initialized before these modules are imported
# directly: handlers/__init__ re-exports them while the generator imports
# session_history from that same package (a pre-existing import cycle).
import msagent.cli.handlers  # noqa: F401
from msagent.cli.handlers import skill_review as review_module
from msagent.cli.theme import theme
from msagent.skill_evolver import mining as module
from msagent.skill_evolver.direct_skill_generation import DirectSkillGenerationConfig
from msagent.skills.factory import Skill
from msagent.trajectory_recorder.config import reset_config_cache

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "trajectories"
SIGNALS = FIXTURES / "skill_evolver_signals.jsonl"
AGENT = "Profiler"
SIGNALS_THREAD = "thread-signals"

SKILL_MD = """---
name: profiling-recipe
description: Use when profiling a model on Ascend hardware
---

# Profiling recipe

## Inputs

- A model checkpoint

## Workflow

1. Collect the profile.
2. Analyse the hotspots.

## Outputs

- A report
"""


def _recorder() -> Console:
    """A recording console that knows the app's style names."""
    return Console(record=True, width=200, no_color=True, theme=theme.rich_theme)


class _NullStatus:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _ConsoleSpy:
    """Records the themed console calls and the renderables printed."""

    def __init__(self) -> None:
        self.info: list[str] = []
        self.success: list[str] = []
        self.warning: list[str] = []
        self.error: list[str] = []
        self.plain: list[str] = []
        self.renderables: list[object] = []
        self.console = SimpleNamespace(
            status=lambda *_a, **_k: _NullStatus(),
            print=self.renderables.append,
        )

    def print(self, *args, **_kwargs) -> None:
        self.plain.extend(str(arg) for arg in args)

    def print_info(self, content: str) -> None:
        self.info.append(content)

    def print_success(self, content: str) -> None:
        self.success.append(content)

    def print_warning(self, content: str) -> None:
        self.warning.append(content)

    def print_error(self, content: str) -> None:
        self.error.append(content)

    def rendered_text(self) -> str:
        """Everything printed as a renderable, as plain text."""
        recorder = _recorder()
        for renderable in self.renderables:
            recorder.print(renderable)
        return recorder.export_text()


def _session(working_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        context=SimpleNamespace(
            agent=AGENT,
            thread_id="thread-current",
            working_dir=working_dir,
            model="default",
        ),
        graph=None,
    )


def _boom(*_args, **_kwargs):
    raise AssertionError("the LLM must not be created")


def _episode(kind: str, seqs: list[int], **overrides):
    """A hand-built episode of thread-x citing ``seqs`` of its file (line == seq)."""
    from msagent.skill_evolver.features import Episode, EvidenceItem
    from msagent.trajectory_recorder.model import EvidenceRef

    fields = {
        "kind": kind,
        "thread_id": "thread-x",
        "source": "thread-x.jsonl",
        "evidence": [
            EvidenceItem(EvidenceRef(source="thread-x.jsonl", line=seq, seq=seq), "event", True) for seq in seqs
        ],
        "tool_sequence": [],
        "facts": {},
        "weight": 0.7,
    }
    fields.update(overrides)
    return Episode(**fields)


@pytest.fixture
def mine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A mining handler over a tmp project, with the LLM wired to explode."""
    spy = _ConsoleSpy()
    monkeypatch.setattr(module, "console", spy)
    monkeypatch.delenv("MSAGENT_TRAJECTORY_CONFIG", raising=False)
    reset_config_cache()

    trajectories = module.initializer.get_project_paths(tmp_path).root / "trajectories"
    trajectories.mkdir(parents=True)

    state = SimpleNamespace(
        handler=module.SkillMiningHandler(_session(tmp_path)),
        spy=spy,
        trajectories=trajectories,
        root=tmp_path,
        # A test may replace these before calling handle().
        config=DirectSkillGenerationConfig(output_dir=tmp_path / "skills"),
        skills=[],
    )
    monkeypatch.setattr(
        module.DirectSkillGenerationHandler,
        "_load_config",
        staticmethod(lambda: state.config),
    )

    async def fake_refresh(*, agent: str, working_dir: Path) -> list:
        return list(state.skills)

    monkeypatch.setattr(module.initializer, "refresh_cached_skills", fake_refresh)
    monkeypatch.setattr(module.initializer, "load_llm_config", _boom)
    monkeypatch.setattr(module.initializer.llm_factory, "create", _boom)
    yield state
    reset_config_cache()


def _copy(trajectories: Path, source: Path, thread_id: str) -> Path:
    """Copy a fixture trajectory; the reader reads its thread id from inside."""
    target = trajectories / f"{AGENT}_{thread_id}.jsonl"
    shutil.copy(source, target)
    return target


def _copy_as(trajectories: Path, thread_id: str) -> Path:
    """The signals fixture recorded under another thread id (every event rewritten)."""
    lines: list[str] = []
    for raw in SIGNALS.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        event = json.loads(raw)
        event["thread_id"] = thread_id
        lines.append(json.dumps(event, ensure_ascii=False))
    target = trajectories / f"{AGENT}_{thread_id}.jsonl"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _library_skill(root: Path, name: str, category: str = "default") -> Skill:
    """A library skill with a real SKILL.md the update plan can read."""
    skill_dir = root / "library" / category / name
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: Use when testing.\n---\nold body\n", encoding="utf-8")
    return Skill(name=name, description="Use when testing.", category=category, path=path)


def _write_toolless(trajectories: Path, thread_id: str) -> Path:
    """A trajectory recorded before the ignore_agent fix: no tool.* events."""
    lines: list[str] = []
    for raw in SIGNALS.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        event = json.loads(raw)
        kind = str(event.get("event", ""))
        if kind.startswith("tool.") or kind.startswith("approval."):
            continue
        event["thread_id"] = thread_id
        lines.append(json.dumps(event, ensure_ascii=False))
    target = trajectories / f"{AGENT}_{thread_id}.jsonl"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


# ------------------------------------------------------------ argument parsing


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("30d", timedelta(days=30)),
        ("2w", timedelta(days=14)),
        ("7D", timedelta(days=7)),
    ],
)
def test_parse_since_accepts_hours_days_weeks(raw: str, expected: timedelta) -> None:
    assert module.parse_mine_args(["--since", raw]).since == expected


@pytest.mark.parametrize("raw", ["30m", "7", "d", "7 d", "-1d", "1mo"])
def test_parse_since_rejects_unknown_units(raw: str) -> None:
    with pytest.raises(module.MineArgsError, match="--since"):
        module.parse_mine_args(["--since", raw])


def test_parse_since_rejects_zero() -> None:
    with pytest.raises(module.MineArgsError, match="at least 1"):
        module.parse_mine_args(["--since", "0d"])


def test_parse_mine_args_defaults() -> None:
    options = module.parse_mine_args([])
    assert options.threads == module.DEFAULT_THREADS
    assert options.since is None
    assert options.dry_run is False
    assert options.thread is None


def test_parse_mine_args_full_line() -> None:
    options = module.parse_mine_args(["--dry-run", "--threads", "3", "--since", "7d"])
    assert options.dry_run is True
    assert options.threads == 3
    assert options.since == timedelta(days=7)


def test_parse_mine_args_thread_conflicts_with_since() -> None:
    with pytest.raises(module.MineArgsError, match="cannot be combined"):
        module.parse_mine_args(["--thread", "abc", "--since", "7d"])


def test_parse_mine_args_thread_conflicts_with_threads() -> None:
    with pytest.raises(module.MineArgsError, match="cannot be combined"):
        module.parse_mine_args(["--thread", "abc", "--threads", "2"])


def test_parse_mine_args_thread_allows_dry_run() -> None:
    options = module.parse_mine_args(["--thread", "abc", "--dry-run"])
    assert options.thread == "abc"
    assert options.dry_run is True


@pytest.mark.parametrize(
    "args",
    [
        ["--threads"],
        ["--threads", "x"],
        ["--threads", "0"],
        ["--threads", "1", "--threads", "2"],
        ["--dry-run", "--dry-run"],
        ["list"],
        ["--unknown"],
    ],
)
def test_parse_mine_args_rejects(args: list[str]) -> None:
    with pytest.raises(module.MineArgsError):
        module.parse_mine_args(args)


# ---------------------------------------------------------------- formatting


def test_format_tool_sequence_keeps_the_true_count() -> None:
    names = [f"tool{index}" for index in range(9)]
    rendered = module.format_tool_sequence(names)
    assert rendered.startswith("[9] ")
    assert rendered.endswith("+4")
    assert module.format_tool_sequence([]) == "[0] (none)"
    assert module.format_tool_sequence(["a", "b"]) == "[2] a → b"


def test_format_evidence_seq_keeps_the_true_count() -> None:
    rendered = module.format_evidence_seq(list(range(1, 12)))
    assert rendered.startswith("[11] ")
    assert rendered.endswith("+3")
    assert module.format_evidence_seq([4, 5]) == "[2] 4, 5"


def test_episodes_table_shows_markup_literally() -> None:
    episode = _episode("retry_loop", [1, 2], tool_sequence=["[bold]evil", "read_file"], weight=0.7)
    stats = module.ThreadStats(
        trajectory=SimpleNamespace(thread_id="thread-x"),
        turns=1,
        tool_calls=2,
        ai_messages=1,
        episodes=[episode],
    )
    recorder = _recorder()
    recorder.print(module.build_episodes_table([stats]))
    text = recorder.export_text()
    assert "[bold]evil" in text
    assert "I1" in text
    assert "subtotal" in text
    assert "0.70" in text


def test_threads_table_explains_a_strong_correction() -> None:
    # One strong correction scores 0.9, below the default 1.0: the gate
    # admits it by its own rule and the table has to say so.
    episode = _episode(
        "user_correction",
        [2, 10],
        tool_sequence=["grep"],
        facts={"strength": "strong"},
        weight=0.9,
        anchors=["run-2#10"],
    )
    stats = module.ThreadStats(
        trajectory=SimpleNamespace(thread_id="thread-x"),
        turns=2,
        tool_calls=2,
        ai_messages=2,
        episodes=[episode],
    )
    recorder = _recorder()
    recorder.print(module.build_threads_table([stats], min_score=1.0))
    text = recorder.export_text()
    assert "pass" in text
    assert "strong user correction" in text


# ------------------------------------------------------------------- dry run


@pytest.mark.asyncio
async def test_dry_run_never_creates_an_llm(mine) -> None:
    _copy(mine.trajectories, SIGNALS, SIGNALS_THREAD)

    await mine.handler.handle(["--dry-run"])

    assert mine.spy.error == []
    text = mine.spy.rendered_text()
    assert "Threads (min_evidence_score 1.00)" in text
    assert "Episodes" in text
    assert "error_recovery" in text
    assert any("Nothing was written and no LLM was created" in m for m in mine.spy.info)


@pytest.mark.asyncio
async def test_dry_run_reports_incidents_and_the_gate_reason(mine) -> None:
    _copy(mine.trajectories, SIGNALS, SIGNALS_THREAD)

    await mine.handler.handle(["--dry-run"])

    assert mine.spy.error == []
    text = mine.spy.rendered_text()
    assert "incidents" in text
    assert "reason" in text
    assert "score >= min_evidence_score" in text
    assert "I1" in text
    # Three incidents, not five episodes: the two recoveries and the retry
    # loop share the bash chain, so the score is 2.60 rather than 3.80.
    assert any("5 episodes, 3 incidents" in line for line in mine.spy.info)
    assert "2.60" in text


@pytest.mark.asyncio
async def test_dry_run_reports_call_bound_with_max_plans(mine) -> None:
    _copy(mine.trajectories, SIGNALS, SIGNALS_THREAD)
    mine.config = DirectSkillGenerationConfig(output_dir=mine.root / "skills", max_plans=2)

    await mine.handler.handle(["--dry-run"])

    assert any("1 threads would reach the LLM (up to 6 LLM calls)" in line for line in mine.spy.info)


def test_llm_call_bound() -> None:
    assert module.llm_call_bound(0, 3) == 0
    assert module.llm_call_bound(1, 1) == 4
    assert module.llm_call_bound(5, 3) == 40


@pytest.mark.asyncio
async def test_dry_run_explains_a_trajectory_without_tool_events(mine) -> None:
    _write_toolless(mine.trajectories, "thread-plain")

    await mine.handler.handle(["--dry-run"])

    assert mine.spy.error == []
    assert any("ignore_agent" in line for line in mine.spy.plain)
    text = mine.spy.rendered_text()
    assert "thread-plain" in text
    assert any("No episodes detected" in line for line in mine.spy.info)


@pytest.mark.asyncio
async def test_dry_run_keeps_the_note_quiet_when_tools_were_recorded(mine) -> None:
    _copy(mine.trajectories, SIGNALS, SIGNALS_THREAD)

    await mine.handler.handle(["--dry-run"])

    assert not any("ignore_agent" in line for line in mine.spy.plain)


@pytest.mark.asyncio
async def test_dry_run_reports_an_empty_directory(mine) -> None:
    await mine.handler.handle(["--dry-run"])

    assert mine.spy.error == []
    assert any("No trajectories recorded" in line for line in mine.spy.warning)


@pytest.mark.asyncio
async def test_dry_run_reports_an_unknown_thread(mine) -> None:
    _copy(mine.trajectories, SIGNALS, SIGNALS_THREAD)

    await mine.handler.handle(["--dry-run", "--thread", "missing"])

    assert any("No recorded trajectory" in line for line in mine.spy.warning)


@pytest.mark.asyncio
async def test_dry_run_selects_one_thread(mine) -> None:
    _copy(mine.trajectories, SIGNALS, SIGNALS_THREAD)
    _write_toolless(mine.trajectories, "thread-plain")

    await mine.handler.handle(["--dry-run", "--thread", SIGNALS_THREAD])

    text = mine.spy.rendered_text()
    assert "thread-signa" in text
    assert "thread-plain" not in text


@pytest.mark.asyncio
async def test_dry_run_honours_the_thread_limit(mine) -> None:
    _copy(mine.trajectories, SIGNALS, SIGNALS_THREAD)
    _write_toolless(mine.trajectories, "thread-plain")

    await mine.handler.handle(["--dry-run", "--threads", "1"])

    assert any("Selected the newest 1" in line for line in mine.spy.info)


@pytest.mark.asyncio
async def test_bad_flag_reports_usage_and_stops(mine) -> None:
    await mine.handler.handle(["--since", "30m"])

    assert any("--since" in line for line in mine.spy.error)
    assert any(module.USAGE in line for line in mine.spy.plain)
    assert mine.spy.renderables == []


@pytest.mark.asyncio
async def test_real_run_below_threshold_creates_no_llm(mine) -> None:
    # The LLM factory raises; a run whose threads all fail the gate must still
    # finish cleanly, which proves the gate comes first.
    _write_toolless(mine.trajectories, "thread-plain")

    await mine.handler.handle([])

    assert mine.spy.error == []
    assert any("Nothing to mine" in line for line in mine.spy.info)


@pytest.mark.asyncio
async def test_real_run_bundle_exclusion_creates_no_llm(mine, monkeypatch: pytest.MonkeyPatch) -> None:
    # The thread passes the gate, but no episode's required evidence fits the
    # bundle budget: everything is excluded and the LLM (wired to explode)
    # is never created.
    from functools import partial

    from msagent.skill_evolver.bundle import build_evidence_bundle

    _copy(mine.trajectories, SIGNALS, SIGNALS_THREAD)
    monkeypatch.setattr(module, "build_evidence_bundle", partial(build_evidence_bundle, max_chars=1))

    await mine.handler.handle([])

    assert mine.spy.error == []
    assert any(line.startswith("Excluded ") for line in mine.spy.warning)
    assert any("episodes excluded from the bundle; remaining evidence score 0.00" in line for line in mine.spy.info)
    assert any("1 nothing to save" in line for line in mine.spy.info)
    assert not (mine.root / "skills").exists()


# -------------------------------------------------------------- skill review


def _write_proposal(
    root: Path,
    *,
    thread: str = "thread-signals",
    name: str = "profiling-recipe",
    content: str = SKILL_MD,
    action: str = "create",
    category: str = "default",
) -> Path:
    """Create a proposal folder the way writer.write_proposal does."""
    folder = root / ".proposals" / thread / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(content, encoding="utf-8")
    provenance = {
        "thread_ids": [thread],
        "episodes": [],
        "candidates": [{"title": "t"}],
        "model": "fake-model",
        "prompt_variants": {"classify": "c", "render": "r"},
        "features_version": 2,
        "generated_at": "2026-09-04T10:00:00+00:00",
        "category": category,
        "target": {
            "action": action,
            "existing_skill": "other/profiling-recipe" if action == "update" else None,
            "existing_path": "/library/profiling-recipe/SKILL.md",
        },
    }
    (folder / "provenance.json").write_text(
        json.dumps(provenance, indent=2),
        encoding="utf-8",
    )
    return folder


@pytest.fixture
def review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A review handler whose proposals root is a tmp directory."""
    spy = _ConsoleSpy()
    monkeypatch.setattr(review_module, "console", spy)
    root = tmp_path / "skills"
    root.mkdir()
    config = DirectSkillGenerationConfig(output_dir=root)
    monkeypatch.setattr(
        module.DirectSkillGenerationHandler,
        "_load_config",
        staticmethod(lambda: config),
    )
    handler = review_module.SkillReviewHandler(_session(tmp_path))
    return SimpleNamespace(handler=handler, spy=spy, root=root)


@pytest.mark.asyncio
async def test_review_list_shows_metadata(review) -> None:
    _write_proposal(review.root)

    await review.handler.handle(["list"])

    text = review.spy.rendered_text()
    assert "profiling-recipe" in text
    assert "Use when profiling" in text
    assert "create" in text


@pytest.mark.asyncio
async def test_review_list_reports_an_unreadable_proposal(review) -> None:
    folder = _write_proposal(review.root)
    (folder / "provenance.json").write_text("{not json", encoding="utf-8")

    await review.handler.handle(["list"])

    assert "unreadable" in review.spy.rendered_text()


@pytest.mark.asyncio
async def test_review_list_empty(review) -> None:
    await review.handler.handle(["list"])

    assert any("No proposals in" in line for line in review.spy.info)


@pytest.mark.asyncio
async def test_accept_moves_the_directory(review) -> None:
    folder = _write_proposal(review.root)

    await review.handler.handle(["accept", "profiling-recipe"])

    destination = review.root / "default" / "profiling-recipe"
    assert review.spy.error == []
    assert not folder.exists()
    assert (destination / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD
    assert (destination / "provenance.json").is_file()
    # The emptied batch folder is pruned, so .proposals does not accumulate.
    assert not (review.root / ".proposals" / "thread-signals").exists()


@pytest.mark.asyncio
async def test_accept_uses_the_configured_category(review) -> None:
    _write_proposal(review.root, category="profiler")

    await review.handler.handle(["accept", "profiling-recipe"])

    assert (review.root / "profiler" / "profiling-recipe" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_accept_refuses_a_hand_broken_skill(review) -> None:
    folder = _write_proposal(review.root, content=SKILL_MD.replace("## Outputs", "##"))

    await review.handler.handle(["accept", "profiling-recipe"])

    assert any("not a valid SKILL.md" in line for line in review.spy.error)
    assert folder.exists()
    assert not (review.root / "default" / "profiling-recipe").exists()


@pytest.mark.asyncio
async def test_accept_refuses_an_occupied_destination(review) -> None:
    _write_proposal(review.root)
    (review.root / "default" / "profiling-recipe").mkdir(parents=True)

    await review.handler.handle(["accept", "profiling-recipe"])

    assert any("already exists" in line for line in review.spy.error)


@pytest.mark.asyncio
async def test_accept_refuses_an_update_proposal(review) -> None:
    folder = _write_proposal(review.root, action="update")

    await review.handler.handle(["accept", "profiling-recipe"])

    assert any("revises an existing skill" in line for line in review.spy.warning)
    assert folder.exists()


@pytest.mark.asyncio
async def test_accept_reports_an_ambiguous_name(review) -> None:
    _write_proposal(review.root, thread="thread-a")
    _write_proposal(review.root, thread="thread-b")

    await review.handler.handle(["accept", "profiling-recipe"])

    assert any("several batches" in line for line in review.spy.error)
    assert not (review.root / "default").exists()


@pytest.mark.asyncio
async def test_accept_takes_a_qualified_name(review) -> None:
    _write_proposal(review.root, thread="thread-a")
    _write_proposal(review.root, thread="thread-b")

    await review.handler.handle(["accept", "thread-b/profiling-recipe"])

    assert (review.root / "default" / "profiling-recipe" / "SKILL.md").is_file()
    assert (review.root / ".proposals" / "thread-a" / "profiling-recipe").is_dir()


@pytest.mark.asyncio
async def test_reject_deletes_after_confirmation(review, monkeypatch) -> None:
    folder = _write_proposal(review.root)

    async def confirm(_question: str) -> bool:
        return True

    monkeypatch.setattr(review.handler, "_confirm", confirm)

    await review.handler.handle(["reject", "profiling-recipe"])

    assert not folder.exists()
    assert not (review.root / ".proposals" / "thread-signals").exists()
    assert any("Rejected" in line for line in review.spy.success)


@pytest.mark.asyncio
async def test_reject_keeps_the_folder_when_declined(review, monkeypatch) -> None:
    folder = _write_proposal(review.root)

    async def decline(_question: str) -> bool:
        return False

    monkeypatch.setattr(review.handler, "_confirm", decline)

    await review.handler.handle(["reject", "profiling-recipe"])

    assert folder.exists()
    assert any("Cancelled" in line for line in review.spy.info)


@pytest.mark.asyncio
async def test_reject_unknown_name(review, monkeypatch) -> None:
    async def confirm(_question: str) -> bool:
        raise AssertionError("must not ask about a proposal that does not exist")

    monkeypatch.setattr(review.handler, "_confirm", confirm)

    await review.handler.handle(["reject", "missing"])

    assert any("No proposal named" in line for line in review.spy.error)


@pytest.mark.asyncio
async def test_unknown_subcommand(review) -> None:
    await review.handler.handle(["frobnicate"])

    assert any("Unknown subcommand" in line for line in review.spy.error)


# ------------------------------------------------------------------ real run

CLASSIFY_TEMPLATE = "Library:\n{skill_library}\n\nBundle:\n{evidence_bundle}\n"
RENDER_TEMPLATE = "Candidates:\n{candidates}\n\nExisting:\n{existing_skill}\n"
GENERATED_NAME = "generated-source-debugging"
GENERATED_SKILL = "\n".join(
    [
        "---",
        f"name: {GENERATED_NAME}",
        "description: Use when diagnosing failures in generated sources.",
        "---",
        "",
        "# Generated Source Debugging",
        "",
        "## Inputs",
        "",
        "- The failing output.",
        "",
        "## Workflow",
        "",
        "1. Reproduce the failure.",
        "2. Regenerate the sources before type checking.",
        "",
        "## Outputs",
        "",
        "A verified diagnosis.",
        "",
    ],
)


SECOND_NAME = "kernel-profile-first"
SECOND_SKILL = GENERATED_SKILL.replace(f"name: {GENERATED_NAME}", f"name: {SECOND_NAME}")


def _revised(name: str) -> str:
    """GENERATED_SKILL under the name of the library skill it updates."""
    return GENERATED_SKILL.replace(f"name: {GENERATED_NAME}", f"name: {name}")


def _candidate(refs: list[str], **overrides) -> dict:
    data = {
        "title": "Generated source debugging",
        "rule": "Regenerate sources before type checking.",
        "evidence_refs": list(refs),
        "future_applicability": "high",
        "target": {"action": "create", "existing_skill": None},
    }
    data.update(overrides)
    return data


def _update(refs: list[str], existing: str, **overrides) -> dict:
    return _candidate(refs, target={"action": "update", "existing_skill": existing}, **overrides)


def _classify_reply(*candidates: dict, verdict: str = "save") -> str:
    return json.dumps({"verdict": verdict, "candidates": list(candidates)})


class _FakeLLM:
    """Answers with the scripted replies in order; an extra call is a failure."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.payloads: list[list[tuple[str, str]]] = []

    async def ainvoke(self, payload: list[tuple[str, str]]):
        self.payloads.append(list(payload))
        if not self.replies:
            raise RuntimeError("fake LLM called more often than scripted")
        return AIMessage(content=self.replies.pop(0))


class _FlakyLLM(_FakeLLM):
    """The ``fail_at``-th call (1-based) raises instead of answering: a transport error."""

    def __init__(self, *replies: str, fail_at: int) -> None:
        super().__init__(*replies)
        self.fail_at = fail_at

    async def ainvoke(self, payload: list[tuple[str, str]]):
        if len(self.payloads) + 1 == self.fail_at:
            self.payloads.append(list(payload))
            raise RuntimeError("transport down")
        return await super().ainvoke(payload)


@pytest.fixture
def scripted(mine, monkeypatch: pytest.MonkeyPatch):
    """The mining fixture with the LLM stages wired to a scripted fake."""

    async def fake_stage_prompt(_self, _root, _cfg, stage):
        templates = {"classify": CLASSIFY_TEMPLATE, "render": RENDER_TEMPLATE}
        return templates[stage], f"packaged/{stage}/prompt_v1.md"

    monkeypatch.setattr(
        module.DirectSkillGenerationHandler,
        "_load_stage_prompt",
        fake_stage_prompt,
    )

    async def fake_load_llm_config(_model, _working_dir):
        return SimpleNamespace(model="fake-model", context_window=1000)

    monkeypatch.setattr(module.initializer, "load_llm_config", fake_load_llm_config)

    def script(*replies, fail_at: int | None = None):
        llm = _FakeLLM(*replies) if fail_at is None else _FlakyLLM(*replies, fail_at=fail_at)
        monkeypatch.setattr(module.initializer.llm_factory, "create", lambda _c: llm)
        mine.llm = llm
        return llm

    mine.script = script
    return mine


@pytest.mark.asyncio
async def test_real_run_writes_one_proposal_per_thread(scripted) -> None:
    _copy(scripted.trajectories, SIGNALS, SIGNALS_THREAD)
    scripted.script(_classify_reply(_candidate(["ev1", "ev2"])), GENERATED_SKILL)

    await scripted.handler.handle([])

    proposal = (
        scripted.root
        / "skills"
        / ".proposals"
        / SIGNALS_THREAD
        / GENERATED_NAME
        / "SKILL.md"
    )
    assert proposal.is_file(), (scripted.spy.error, scripted.spy.warning)
    assert proposal.read_text(encoding="utf-8") == GENERATED_SKILL
    provenance = json.loads(
        (proposal.parent / "provenance.json").read_text(encoding="utf-8"),
    )
    assert provenance["model"] == "fake-model"
    assert provenance["thread_ids"][0] == SIGNALS_THREAD
    assert provenance["provenance_version"] == 3
    assert set(provenance["candidates"][0]["evidence_refs"]) <= set(provenance["evidence_shown"])
    assert provenance["render_evidence"] == {"c1": ["ev1", "ev2"]}
    assert any("1 proposals" in line for line in scripted.spy.success)
    # The per-thread header names the gate decision that let it through.
    assert any("incidents" in line for line in scripted.spy.info)
    # Two calls for one thread: classify and render.
    assert len(scripted.llm.payloads) == 2
    # The default max_plans (3) bounds the run at 2 + 2 * 3 calls per thread.
    assert any("(up to 8 LLM calls)" in line for line in scripted.spy.info)


@pytest.mark.asyncio
async def test_real_run_writes_nothing_on_a_nothing_verdict(scripted) -> None:
    _copy(scripted.trajectories, SIGNALS, SIGNALS_THREAD)
    scripted.script(_classify_reply(_candidate(["ev1", "ev2"]), verdict="nothing"))

    await scripted.handler.handle([])

    assert not (scripted.root / "skills" / ".proposals").exists()
    assert any("1 nothing to save" in line for line in scripted.spy.info)


@pytest.mark.asyncio
async def test_real_run_render_transport_error_is_a_render_error(scripted) -> None:
    _copy(scripted.trajectories, SIGNALS, SIGNALS_THREAD)
    # One scripted reply, but the pipeline needs two: the render call raises,
    # which is a render error of that plan, not a failed thread.
    scripted.script(_classify_reply(_candidate(["ev1", "ev2"])))

    await scripted.handler.handle([])

    assert any(line.startswith("plan create: ") and "more often than scripted" in line for line in scripted.spy.error)
    summary = next(line for line in scripted.spy.error if line.startswith("Mined "))
    assert "0 proposals" in summary and "0 failed" in summary
    assert "Plans: 1 render errors, 0 rejected targets, 0 deferred" in scripted.spy.error
    assert not (scripted.root / "skills" / ".proposals").exists()


@pytest.mark.asyncio
async def test_real_run_reports_a_failing_thread_and_continues(scripted) -> None:
    _copy(scripted.trajectories, SIGNALS, SIGNALS_THREAD)
    # No scripted reply at all: the classify call raises, outside any plan.
    scripted.script()

    await scripted.handler.handle([])

    assert any(SIGNALS_THREAD in line for line in scripted.spy.error)
    assert any("1 failed" in line for line in scripted.spy.error)
    assert not (scripted.root / "skills" / ".proposals").exists()


@pytest.mark.asyncio
async def test_real_run_two_updates_two_proposals(scripted) -> None:
    _copy(scripted.trajectories, SIGNALS, SIGNALS_THREAD)
    scripted.skills = [_library_skill(scripted.root, "alpha"), _library_skill(scripted.root, "beta")]
    scripted.script(
        _classify_reply(_update(["ev1"], "alpha", title="Alpha rule"), _update(["ev2"], "beta", title="Beta rule")),
        _revised("alpha"),
        _revised("beta"),
    )

    await scripted.handler.handle([])

    assert len(scripted.llm.payloads) == 3 and scripted.llm.replies == []
    proposals = scripted.root / "skills" / ".proposals" / SIGNALS_THREAD
    assert (proposals / "alpha" / "SKILL.md").is_file(), scripted.spy.error
    assert (proposals / "beta" / "SKILL.md").is_file()
    assert "Beta rule" not in scripted.llm.payloads[1][0][1]
    assert "Alpha rule" not in scripted.llm.payloads[2][0][1]
    assert any("2 proposals" in line for line in scripted.spy.success)
    assert scripted.spy.error == []
    assert not any(line.startswith("Plans:") for line in scripted.spy.info)


@pytest.mark.asyncio
async def test_real_run_taken_names_span_threads(scripted) -> None:
    _copy_as(scripted.trajectories, "thread-a")
    _copy_as(scripted.trajectories, "thread-b")
    # Both threads propose the same new name: the second gets a corrective
    # turn (the name is taken by the first proposal of this run) and renames.
    scripted.script(
        _classify_reply(_candidate(["ev1", "ev2"])),
        GENERATED_SKILL,
        _classify_reply(_candidate(["ev1", "ev2"])),
        GENERATED_SKILL,
        SECOND_SKILL,
    )

    await scripted.handler.handle([])

    assert len(scripted.llm.payloads) == 5 and scripted.llm.replies == []
    assert f"'{GENERATED_NAME}' already exists in the skill library" in scripted.llm.payloads[4][-1][1]
    proposals = scripted.root / "skills" / ".proposals"
    assert sorted(p.name for p in proposals.glob("*/*")) == [GENERATED_NAME, SECOND_NAME]
    assert any("2 proposals" in line for line in scripted.spy.success)


@pytest.mark.asyncio
async def test_real_run_first_plan_error_does_not_stop_second(scripted) -> None:
    _copy(scripted.trajectories, SIGNALS, SIGNALS_THREAD)
    second = _candidate(["ev1", "ev2"], title="Profile before summary")
    scripted.script(_classify_reply(_candidate(["ev1", "ev2"]), second), SECOND_SKILL, fail_at=2)

    await scripted.handler.handle([])

    assert len(scripted.llm.payloads) == 3 and scripted.llm.replies == []
    (plan_error,) = [line for line in scripted.spy.error if line.startswith("plan ")]
    assert "Generated source debugging" in plan_error and "transport down" in plan_error
    proposals = scripted.root / "skills" / ".proposals" / SIGNALS_THREAD
    assert (proposals / SECOND_NAME / "SKILL.md").is_file()
    assert not (proposals / GENERATED_NAME).exists()
    summary = next(line for line in scripted.spy.error if line.startswith("Mined "))
    assert "1 proposals" in summary and "0 failed" in summary
    assert "Plans: 1 render errors, 0 rejected targets, 0 deferred" in scripted.spy.error


@pytest.mark.asyncio
async def test_real_run_defers_plans_over_max_plans(scripted) -> None:
    _copy(scripted.trajectories, SIGNALS, SIGNALS_THREAD)
    scripted.config = DirectSkillGenerationConfig(output_dir=scripted.root / "skills", max_plans=1)
    second = _candidate(["ev1", "ev2"], title="Profile before summary")
    scripted.script(_classify_reply(_candidate(["ev1", "ev2"]), second), GENERATED_SKILL)

    await scripted.handler.handle([])

    assert len(scripted.llm.payloads) == 2 and scripted.llm.replies == []
    assert "Deferred plan: create: Profile before summary — max_plans 1 reached" in scripted.spy.info
    assert "Plans: 0 render errors, 0 rejected targets, 1 deferred" in scripted.spy.info
    proposals = scripted.root / "skills" / ".proposals" / SIGNALS_THREAD
    assert sorted(p.name for p in proposals.iterdir()) == [GENERATED_NAME]
