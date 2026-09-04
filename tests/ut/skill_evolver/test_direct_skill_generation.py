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

"""Tests for /direct-skill-generation: the evidence pipeline on a scripted LLM."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from langchain_core.messages import AIMessage, HumanMessage

# The handlers package must be initialized before the module is imported directly:
# handlers/__init__ re-exports the handler while the handler imports session_history
# from that same package (a pre-existing import cycle that the CLI never triggers).
import msagent.cli.handlers  # noqa: F401
from msagent.skill_evolver import direct_skill_generation as module
from msagent.skill_evolver.bundle import build_evidence_bundle
from msagent.skill_evolver.direct_skill_generation import (
    DirectSkillGenerationConfig,
    DirectSkillGenerationHandler,
)
from msagent.skill_evolver.features import extract_episodes
from msagent.skill_evolver.retrieval import BM25Index
from msagent.skills.factory import Skill, SkillFactory
from msagent.trajectory_recorder.config import reset_config_cache
from msagent.trajectory_recorder.model import ToolCall, Trajectory, Turn
from msagent.trajectory_recorder.reader import iter_events, load_trajectory

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "trajectories" / "skill_evolver_signals.jsonl"
THREAD_ID = "thread-signals"
AGENT = "Profiler"

CLASSIFY_TEMPLATE = "Library:\n{skill_library}\n\nBundle:\n{evidence_bundle}\n"
RENDER_TEMPLATE = "Candidates:\n{candidates}\n\nExisting:\n{existing_skill}\n"
SKILL_NAME = "generated-source-debugging"
VALID_SKILL = "\n".join(
    [
        "---",
        f"name: {SKILL_NAME}",
        "description: Use when diagnosing failures involving generated source artifacts.",
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
    ]
)
INVALID_SKILL = "---\nname: fix-it\ndescription: Instructions for debugging\n---\n"


class _NullStatus:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _ConsoleSpy:
    def __init__(self) -> None:
        self.info: list[str] = []
        self.success: list[str] = []
        self.warning: list[str] = []
        self.error: list[str] = []
        self.console = SimpleNamespace(status=lambda *_args, **_kwargs: _NullStatus())

    def print(self, *_args, **_kwargs) -> None:
        pass

    def print_info(self, content: str) -> None:
        self.info.append(content)

    def print_success(self, content: str) -> None:
        self.success.append(content)

    def print_warning(self, content: str) -> None:
        self.warning.append(content)

    def print_error(self, content: str) -> None:
        self.error.append(content)


class _Pipeline:
    """The handler under test, its console spy and the knobs a test may turn."""

    def __init__(self, handler: DirectSkillGenerationHandler, spy: _ConsoleSpy, trajectories_dir: Path, fake_llm_cls):
        self.handler = handler
        self.spy = spy
        self.trajectories_dir = trajectories_dir
        self.fake_llm_cls = fake_llm_cls
        self.config = DirectSkillGenerationConfig()
        self.skills: list[Skill] = []
        self.llm = fake_llm_cls()

    def script(self, *replies: str) -> None:
        self.llm = self.fake_llm_cls(*replies)


def _session(working_dir: Path) -> SimpleNamespace:
    context = SimpleNamespace(agent=AGENT, thread_id=THREAD_ID, working_dir=working_dir, model="default")
    return SimpleNamespace(context=context, graph=None)


@pytest.fixture
def pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_llm_cls):
    spy = _ConsoleSpy()
    monkeypatch.setattr(module, "console", spy)
    monkeypatch.delenv("MSAGENT_TRAJECTORY_CONFIG", raising=False)
    reset_config_cache()

    # Where resolve_trajectories_dir() looks for this working dir (isolated home).
    trajectories_dir = module.initializer.get_project_paths(tmp_path).root / "trajectories"
    trajectories_dir.mkdir(parents=True)
    shutil.copy(FIXTURE, trajectories_dir / f"{AGENT}_{THREAD_ID}.jsonl")

    state = _Pipeline(DirectSkillGenerationHandler(_session(tmp_path)), spy, trajectories_dir, fake_llm_cls)

    async def fake_load_history(_session, _target):
        return THREAD_ID, [HumanMessage(content="профилируй")]

    async def fake_stage_prompt(self, _root, _cfg, stage):
        templates = {"classify": CLASSIFY_TEMPLATE, "render": RENDER_TEMPLATE}
        return templates[stage], f"packaged/{stage}/prompt_v1.md"

    async def fake_load_skills(self):
        return list(state.skills)

    async def fake_load_llm_config(_model, _working_dir):
        return SimpleNamespace(model="fake-model", context_window=1000)

    monkeypatch.setattr(module, "load_history", fake_load_history)
    monkeypatch.setattr(DirectSkillGenerationHandler, "_load_config", staticmethod(lambda: state.config))
    monkeypatch.setattr(DirectSkillGenerationHandler, "_load_stage_prompt", fake_stage_prompt)
    monkeypatch.setattr(DirectSkillGenerationHandler, "_load_skills", fake_load_skills)
    monkeypatch.setattr(module.initializer, "load_llm_config", fake_load_llm_config)
    monkeypatch.setattr(module.initializer.llm_factory, "create", lambda _config: state.llm)
    yield state
    reset_config_cache()


# ------------------------------------------------------------------ builders


def _classify_reply(*candidates: dict[str, Any], verdict: str = "save") -> str:
    return json.dumps({"verdict": verdict, "candidates": list(candidates)})


def _candidate(refs: list[int], **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "title": "Generated source debugging",
        "rule": "Regenerate sources before type checking.",
        "evidence_refs": list(refs),
        "future_applicability": "high",
        "target": {"action": "create", "existing_skill": None},
    }
    data.update(overrides)
    return data


def _valid_refs() -> list[int]:
    """Two seqs the classify stage keeps: they are in the fixture's evidence bundle."""
    trajectory = load_trajectory(FIXTURE)
    _, valid = build_evidence_bundle(extract_episodes(trajectory), [trajectory])
    return sorted(valid)[:2]


def _recorded_seqs(path: Path) -> set[int]:
    seqs: set[int] = set()
    for event in iter_events(path):
        valid = event.get("v") == 1 and isinstance(event.get("event"), str)
        if valid and isinstance(event.get("seq"), int):
            seqs.add(event["seq"])
    return seqs


def _library_skill(tmp_path: Path, name: str) -> Skill:
    skill_dir = tmp_path / "skills" / "default" / name
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: Use when testing.\n---\nold body\n", encoding="utf-8")
    return Skill(name=name, description="Use when testing.", category="default", path=path)


def _call(name: str, seq: int) -> ToolCall:
    return ToolCall(
        span_id=f"s{seq}",
        parent_span_id=None,
        name=name,
        args={},
        status="ok",
        output_text="",
        error_type=None,
        error=None,
        duration_ms=1,
        seq_start=seq,
        seq_end=seq + 1,
        subagent=None,
    )


def _trajectory(thread_id: str, names: list[str]) -> Trajectory:
    calls = [_call(name, seq=2 * index + 2) for index, name in enumerate(names)]
    turn = Turn(
        run_id="run-1",
        seq_start=1,
        user_message="do it",
        source="dispatch",
        tool_calls=calls,
        status="completed",
    )
    return Trajectory(
        path=Path(f"{thread_id}.jsonl"),
        thread_id=thread_id,
        agent=AGENT,
        model=None,
        working_dir="/w",
        started_at="2026-09-01T10:00:00.000+00:00",
        turns=[turn],
    )


# ------------------------------------------------------------ handle: writes


@pytest.mark.asyncio
async def test_handle_writes_proposal_not_library(pipeline: _Pipeline, tmp_path: Path) -> None:
    refs = _valid_refs()
    pipeline.script(_classify_reply(_candidate(refs)), VALID_SKILL)

    await pipeline.handler.handle([])

    proposal = tmp_path / "skills" / ".proposals" / THREAD_ID / SKILL_NAME / "SKILL.md"
    assert proposal.is_file(), (pipeline.spy.error, pipeline.spy.warning, pipeline.spy.info)
    assert proposal.read_text(encoding="utf-8") == VALID_SKILL
    assert not (tmp_path / "skills" / "default").exists()
    assert pipeline.spy.success == [f"Skill proposal saved to {proposal}"]
    assert pipeline.spy.error == [] and pipeline.spy.warning == []
    assert len(pipeline.llm.payloads) == 2
    classify_instruction = pipeline.llm.payloads[0][0][1]
    assert "Evidence: seq" in classify_instruction
    assert "The skill library is currently empty." in classify_instruction
    assert "Regenerate sources before type checking." in pipeline.llm.payloads[1][0][1]

    provenance = json.loads((proposal.parent / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["thread_ids"] == [THREAD_ID]
    assert provenance["model"] == "fake-model"
    assert provenance["prompt_variants"] == {
        "classify": "packaged/classify/prompt_v1.md",
        "render": "packaged/render/prompt_v1.md",
    }
    assert provenance["features_version"] == 1
    assert provenance["category"] == "default"
    assert provenance["target"] == {"action": "create", "existing_skill": None, "existing_path": None}
    recorded = _recorded_seqs(FIXTURE)
    assert provenance["episodes"]
    for episode in provenance["episodes"]:
        assert episode["thread_id"] == THREAD_ID
        assert set(episode["evidence_seq"]) <= recorded
    assert [candidate["evidence_refs"] for candidate in provenance["candidates"]] == [refs]


@pytest.mark.asyncio
async def test_proposal_invisible_to_skill_scanners(pipeline: _Pipeline, tmp_path: Path) -> None:
    from msagent.agents.factory import AgentFactory

    pipeline.script(_classify_reply(_candidate(_valid_refs())), VALID_SKILL)
    await pipeline.handler.handle([])
    skills_root = tmp_path / "skills"
    assert (skills_root / ".proposals").is_dir()

    assert await SkillFactory().load_skills(skills_root) == {}
    assert AgentFactory._resolve_existing_paths([skills_root]) == []


@pytest.mark.asyncio
async def test_handle_update_passes_existing_text(pipeline: _Pipeline, tmp_path: Path) -> None:
    skill = _library_skill(tmp_path, "real")
    original = skill.path.read_text(encoding="utf-8")
    pipeline.skills = [skill]
    target = {"action": "update", "existing_skill": "real"}
    revised = VALID_SKILL.replace(f"name: {SKILL_NAME}", "name: real")
    pipeline.script(_classify_reply(_candidate(_valid_refs(), target=target)), revised)

    await pipeline.handler.handle([])

    proposal = tmp_path / "skills" / ".proposals" / THREAD_ID / "real" / "SKILL.md"
    assert proposal.is_file(), (pipeline.spy.error, pipeline.spy.warning)
    assert original in pipeline.llm.payloads[1][0][1]
    assert "- real: Use when testing." in pipeline.llm.payloads[0][0][1]
    assert skill.path.read_text(encoding="utf-8") == original
    provenance = json.loads((proposal.parent / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["target"] == {"action": "update", "existing_skill": "real", "existing_path": str(skill.path)}
    assert pipeline.spy.success == [f"Skill proposal saved to {proposal}"]


# ----------------------------------------------------------- handle: refusals


@pytest.mark.asyncio
async def test_handle_refuses_without_trajectory(pipeline: _Pipeline, tmp_path: Path) -> None:
    for file in pipeline.trajectories_dir.iterdir():
        file.unlink()

    await pipeline.handler.handle([])

    assert pipeline.llm.payloads == []
    (error,) = pipeline.spy.error
    assert error.startswith(f"No recorded trajectory for thread {THREAD_ID} in ")
    assert error.endswith("; the LLM was not called")
    assert not (tmp_path / "skills").exists()


@pytest.mark.asyncio
async def test_handle_stops_below_evidence_threshold(pipeline: _Pipeline, tmp_path: Path) -> None:
    pipeline.config = DirectSkillGenerationConfig(min_evidence_score=100.0)

    await pipeline.handler.handle([])

    assert pipeline.llm.payloads == []
    hint, info = pipeline.spy.info
    assert hint == module._DEPRECATION_HINT
    assert info.startswith("Nothing to save: evidence score ")
    assert "< min_evidence_score 100.00" in info
    assert not (tmp_path / "skills").exists()


@pytest.mark.asyncio
async def test_handle_nothing_verdict(pipeline: _Pipeline, tmp_path: Path) -> None:
    pipeline.script(_classify_reply(verdict="nothing"))

    await pipeline.handler.handle([])

    assert pipeline.spy.info == [
        module._DEPRECATION_HINT,
        f"Nothing to save: no durable learning found in thread {THREAD_ID}",
    ]
    assert len(pipeline.llm.payloads) == 1
    assert not (tmp_path / "skills").exists()


@pytest.mark.asyncio
async def test_handle_fabricated_refs_dropped(pipeline: _Pipeline, tmp_path: Path) -> None:
    pipeline.script(_classify_reply(_candidate([9999])))

    await pipeline.handler.handle([])

    assert pipeline.spy.info == [
        module._DEPRECATION_HINT,
        f"Nothing to save: no durable learning found in thread {THREAD_ID}",
    ]
    assert len(pipeline.llm.payloads) == 1
    assert not (tmp_path / "skills").exists()


@pytest.mark.asyncio
async def test_handle_rejects_invalid_skill_twice(pipeline: _Pipeline, tmp_path: Path) -> None:
    pipeline.script(_classify_reply(_candidate(_valid_refs())), INVALID_SKILL, INVALID_SKILL)

    await pipeline.handler.handle([])

    assert len(pipeline.llm.payloads) == 3
    assert pipeline.spy.error[0] == "SKILL.md rejected after one correction; nothing written:"
    assert any("task identifier" in error for error in pipeline.spy.error)
    assert any("description: must start with" in error for error in pipeline.spy.error)
    assert any("missing section '## Inputs'" in error for error in pipeline.spy.error)
    assert pipeline.spy.success == []
    assert not (tmp_path / "skills").exists()


@pytest.mark.asyncio
async def test_handle_reference_only(pipeline: _Pipeline, tmp_path: Path) -> None:
    pipeline.skills = [_library_skill(tmp_path, "real")]
    target = {"action": "reference", "existing_skill": "real"}
    pipeline.script(_classify_reply(_candidate(_valid_refs(), target=target)))

    await pipeline.handler.handle([])

    assert len(pipeline.llm.payloads) == 1
    assert pipeline.spy.info == [
        module._DEPRECATION_HINT,
        "already covered by real: Generated source debugging",
        "Nothing to save: no candidate left to render",
    ]
    assert not (tmp_path / "skills" / ".proposals").exists()


@pytest.mark.asyncio
async def test_handle_unknown_update_target_dropped(pipeline: _Pipeline, tmp_path: Path) -> None:
    target = {"action": "update", "existing_skill": "ghost"}
    pipeline.script(_classify_reply(_candidate(_valid_refs(), target=target)))

    await pipeline.handler.handle([])

    assert len(pipeline.llm.payloads) == 1
    assert pipeline.spy.warning == [
        "Dropped 'Generated source debugging': existing_skill 'ghost' is not in the skill library"
    ]
    assert pipeline.spy.info == [
        module._DEPRECATION_HINT,
        "Nothing to save: no candidate left to render",
    ]
    assert not (tmp_path / "skills").exists()


@pytest.mark.asyncio
async def test_handle_reports_pipeline_errors(pipeline: _Pipeline, tmp_path: Path) -> None:
    pipeline.script("not json", "still not json")

    await pipeline.handler.handle([])

    (error,) = pipeline.spy.error
    assert error.startswith("Error generating skill: classify: reply is not valid JSON")
    assert not (tmp_path / "skills").exists()


# ------------------------------------------------------------ evidence


def test_collect_episodes_cross_session_cites_current_thread() -> None:
    current = _trajectory("thread-a", ["bash", "read_file", "grep"])
    other = _trajectory("thread-b", ["bash", "read_file", "grep"])

    episodes = module._collect_episodes(current, [other], skill_index=BM25Index([]))

    (episode,) = episodes
    assert episode.kind == "repeated_procedure"
    assert episode.thread_id == "thread-a"
    assert episode.tool_sequence == ["bash", "read_file", "grep"]
    assert episode.evidence_seq == [2, 4, 6]
    assert episode.facts["thread_ids"] == ["thread-a", "thread-b"]
    assert module._collect_episodes(current, [], skill_index=BM25Index([])) == []


def test_cited_threads_lists_supporting_threads_after_current() -> None:
    current = _trajectory("thread-a", ["bash", "read_file", "grep"])
    other = _trajectory("thread-b", ["bash", "read_file", "grep"])
    episodes = module._collect_episodes(current, [other], skill_index=BM25Index([]))

    cited = DirectSkillGenerationHandler._cited_threads(current, episodes)

    assert cited == ["thread-a", "thread-b"]


# ------------------------------------------------------------------ prompts


@pytest.mark.asyncio
async def test_load_stage_prompt_prefers_user_root_then_packaged(tmp_path: Path) -> None:
    handler = DirectSkillGenerationHandler(_session(tmp_path))
    root = tmp_path / "prompts"
    (root / "render").mkdir(parents=True)
    (root / "render" / "prompt_v1.md").write_text("user render {candidates} {existing_skill}", encoding="utf-8")
    cfg = DirectSkillGenerationConfig(prompt_file="prompt_v1.md")

    text, source = await handler._load_stage_prompt(root, cfg, "render")
    assert text.startswith("user render")
    assert source == str(root / "render" / "prompt_v1.md")

    text, source = await handler._load_stage_prompt(root, cfg, "classify")
    assert "{evidence_bundle}" in text and "{skill_library}" in text
    assert Path(source).parts[-2:] == ("classify", "prompt_v1.md")

    with pytest.raises(ValueError, match="Unknown prompt stage"):
        await handler._load_stage_prompt(root, cfg, "default")


@pytest.mark.asyncio
async def test_load_stage_prompt_missing_file_falls_back_to_glob(tmp_path: Path, monkeypatch) -> None:
    spy = _ConsoleSpy()
    monkeypatch.setattr(module, "console", spy)
    handler = DirectSkillGenerationHandler(_session(tmp_path))
    root = tmp_path / "prompts"
    (root / "classify").mkdir(parents=True)
    (root / "classify" / "a.md").write_text("A", encoding="utf-8")
    (root / "classify" / "b.md").write_text("B", encoding="utf-8")

    text, source = await handler._load_stage_prompt(root, DirectSkillGenerationConfig(prompt_file="missing.md"), "classify")

    assert text == "A\n\nB"
    assert source == str(root / "classify")
    assert len(spy.warning) == 1 and "missing.md" in spy.warning[0]


def test_packaged_render_prompt_is_resolved_by_default_config() -> None:
    cfg = DirectSkillGenerationHandler._load_config()
    packaged = REPO_ROOT / "resources" / "configs" / "default" / "skill-evolver" / "prompts"

    for stage in module.STAGES:
        assert (packaged / stage / cfg.prompt_file).is_file()


# ----------------------------------------------------------- legacy replay


@pytest.fixture
def legacy_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spy = _ConsoleSpy()
    monkeypatch.setattr(module, "console", spy)
    return DirectSkillGenerationHandler(_session(tmp_path)), spy


@pytest.mark.asyncio
async def test_generate_skill_md_reports_middle_omissions(legacy_handler, monkeypatch) -> None:
    instance, _spy = legacy_handler
    payloads: list[list] = []

    class _FakeLLM:
        async def ainvoke(self, payload):
            payloads.append(list(payload))
            return AIMessage(content="Nothing to save.")

    async def fake_load_llm_config(_model, _working_dir):
        return SimpleNamespace(context_window=1000)

    async def fake_snapshot(self):
        return "- demo-skill: no description"

    monkeypatch.setattr(module.initializer, "load_llm_config", fake_load_llm_config)
    monkeypatch.setattr(module.initializer.llm_factory, "create", lambda _config: _FakeLLM())
    monkeypatch.setattr(module, "trim_history", lambda messages, _llm, _window, **_kw: (list(messages), 3))
    monkeypatch.setattr(DirectSkillGenerationHandler, "_build_skill_library_snapshot", fake_snapshot)

    result = await instance._generate_skill_md(
        [HumanMessage(content="привет")], "Library:\n{skill_library}", THREAD_ID
    )

    assert result == "Nothing to save."
    instruction = payloads[0][-1].content
    assert instruction.startswith(
        "[Note: 3 messages from the middle of the session were omitted due to context limits.]"
    )
    assert "- demo-skill: no description" in instruction


# ------------------------------------------------------------------- config


def test_load_config_reads_valid_packaged_default(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        cfg = DirectSkillGenerationHandler._load_config()

    assert cfg.active == "default"
    assert cfg.prompt_file == "prompt_v1.md"
    assert "Unsafe variant name" not in caplog.text


def test_packaged_config_active_passes_variant_validation() -> None:
    config_path = REPO_ROOT / "resources" / "configs" / "default" / "config.skill.evolver.yml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert module._VARIANT_NAME_PATTERN.fullmatch(str(data["active"]))
    assert module._VARIANT_NAME_PATTERN.fullmatch(str(data["prompt_file"]))


def test_packaged_prompt_matches_single_call_pipeline() -> None:
    prompt_path = (
        REPO_ROOT / "resources" / "configs" / "default" / "skill-evolver" / "prompts" / "default" / "prompt_v1.md"
    )
    text = prompt_path.read_text(encoding="utf-8")

    assert "# Output contract" in text
    assert "{skill_library}" in text
    for agentic_marker in (
        "skills_list",
        "skill_view",
        "skill_manage",
        "`mkdir`",
        "# Standard filesystem and shell access",
        "# Execution rules",
        "# Final response",
    ):
        assert agentic_marker not in text


def test_load_config_default_min_evidence_score() -> None:
    assert DirectSkillGenerationHandler._load_config().min_evidence_score == 1.0


def _write_user_config(text: str) -> None:
    config_dir = module.initializer.app_paths.config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / module.CONFIG_SKILL_EVOLVER_FILE_NAME.name).write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("raw", "expected", "warns"),
    [("2.5", 2.5, False), ("0", 0.0, False), ("abc", 1.0, True), ("-1", 1.0, True)],
)
def test_load_config_parses_min_evidence_score(
    raw: str, expected: float, warns: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[tuple] = []

    def record(*args, **_kwargs) -> None:
        warnings.append(args)

    monkeypatch.setattr(module.logger, "warning", record)
    _write_user_config(f"active: default\nmin_evidence_score: {raw}\n")

    cfg = DirectSkillGenerationHandler._load_config()

    assert cfg.min_evidence_score == expected
    assert bool(warnings) is warns
    if warns:
        assert "min_evidence_score" in warnings[0][0]


def test_load_config_ignores_unsafe_prompt_file(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[tuple] = []
    monkeypatch.setattr(module.logger, "warning", lambda *args, **_kwargs: warnings.append(args))
    _write_user_config("prompt_file: ../../etc/passwd\n")

    cfg = DirectSkillGenerationHandler._load_config()

    assert cfg.prompt_file is None
    assert "Unsafe prompt_file" in warnings[0][0]
