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

"""Tests for the render stage on a scripted fake LLM (no network)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from msagent.skill_evolver.classify import EMPTY_REPLY, Candidate
from msagent.skill_evolver.render import (
    CANDIDATES_PLACEHOLDER,
    EXISTING_SKILL_PLACEHOLDER,
    NO_EXISTING_SKILL,
    format_candidates,
    format_existing_skill,
    plan_render,
    render_skill_md,
)
from msagent.skills.factory import Skill

REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = (
    REPO_ROOT / "resources" / "configs" / "default" / "skill-evolver" / "prompts" / "render" / "prompt_v1.md"
)
LOGGER = "msagent.skill_evolver.render"

TEMPLATE = "Render.\n\n# Candidates\n\n{candidates}\n\n# Existing\n\n{existing_skill}\n\nReply with SKILL.md."
VALID = "\n".join(
    [
        "---",
        "name: build-before-test",
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
INVALID = "---\nname: fix-build\ndescription: Instructions for debugging\n---\n\n## Workflow\n\n1. Only one.\n"


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


def _update(existing_skill: str, **overrides: Any) -> Candidate:
    return _candidate(target={"action": "update", "existing_skill": existing_skill}, **overrides)


def _skill(name: str, category: str = "default") -> Skill:
    return Skill(
        name=name,
        description="Use when testing.",
        category=category,
        path=Path("/tmp") / category / name / "SKILL.md",
    )


# ---------------------------------------------------------------- formatting


def test_format_candidates() -> None:
    text = format_candidates([_candidate(), _update("real", title="Second", future_applicability="low")])

    assert text == "\n".join(
        [
            "1. Build before test (future applicability: high)",
            "   Rule: Run make before invoking the test suite.",
            "   Target: create a new skill",
            "2. Second (future applicability: low)",
            "   Rule: Run make before invoking the test suite.",
            "   Target: update `real`",
        ]
    )


def test_format_existing_skill() -> None:
    text = format_existing_skill("profiler/real", "---\nname: real\n---\nbody\n\n")

    assert text.startswith("The candidates update the existing skill `profiler/real`; keep its name.")
    assert text.endswith("Current text:\n\n---\nname: real\n---\nbody\n")


# --------------------------------------------------------------- plan_render


def test_plan_create_only() -> None:
    plan = plan_render([_candidate()], [_skill("real")])

    assert [c.title for c in plan.accepted] == ["Build before test"]
    assert plan.existing is None
    assert plan.notes == [] and plan.dropped == []


def test_plan_update_by_display_name() -> None:
    skill = _skill("real", category="profiler")

    plan = plan_render([_update("profiler/real")], [skill, _skill("other")])

    assert plan.existing is skill
    assert len(plan.accepted) == 1


def test_plan_update_by_unique_bare_name() -> None:
    skill = _skill("real", category="profiler")

    plan = plan_render([_update("real")], [skill, _skill("other")])

    assert plan.existing is skill


def test_plan_update_ambiguous_bare_name_dropped(caplog: pytest.LogCaptureFixture) -> None:
    skills = [_skill("real", category="profiler"), _skill("real", category="modeling")]

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        plan = plan_render([_update("real")], skills)

    assert plan.accepted == [] and plan.existing is None
    ((candidate, reason),) = plan.dropped
    assert candidate.title == "Build before test"
    assert reason == "existing_skill 'real' is ambiguous"
    assert "dropped candidate 'Build before test'" in caplog.text


def test_plan_update_unknown_skill_dropped() -> None:
    plan = plan_render([_update("ghost")], [_skill("real")])

    assert plan.accepted == []
    assert plan.dropped[0][1] == "existing_skill 'ghost' is not in the skill library"


def test_plan_reference_noted_not_rendered() -> None:
    candidate = _candidate(target={"action": "reference", "existing_skill": "real"})

    plan = plan_render([candidate], [_skill("real")])

    assert plan.accepted == []
    assert plan.notes == ["already covered by real: Build before test"]


def test_plan_mixed_create_and_updates_of_one_skill() -> None:
    skill = _skill("real")
    candidates = [_candidate(), _update("real", title="Two"), _update("real", title="Three")]

    plan = plan_render(candidates, [skill])

    assert plan.existing is skill
    assert [c.title for c in plan.accepted] == ["Build before test", "Two", "Three"]


def test_plan_updates_of_two_skills_render_a_new_skill() -> None:
    plan = plan_render([_update("one"), _update("two")], [_skill("one"), _skill("two")])

    assert plan.existing is None
    assert len(plan.accepted) == 2
    assert plan.notes == ["2 skills targeted (one, two); rendering a new skill instead"]


def test_plan_empty() -> None:
    plan = plan_render([], [])

    assert plan.accepted == [] and plan.existing is None


# ----------------------------------------------------------- render_skill_md


@pytest.mark.asyncio
async def test_valid_first_reply_single_call(fake_llm_cls) -> None:
    llm = fake_llm_cls(VALID)

    result = await render_skill_md([_candidate()], llm=llm, template=TEMPLATE)

    assert result.ok and result.calls == 1
    assert result.content == VALID.strip()
    (payload,) = llm.payloads
    ((role, instruction),) = payload
    assert role == "human"
    assert format_candidates([_candidate()]) in instruction
    assert NO_EXISTING_SKILL in instruction
    assert CANDIDATES_PLACEHOLDER not in instruction
    assert EXISTING_SKILL_PLACEHOLDER not in instruction


@pytest.mark.asyncio
async def test_fenced_and_think_reply_cleaned(fake_llm_cls) -> None:
    llm = fake_llm_cls("<think>hmm</think>\n```markdown\n" + VALID + "```")

    result = await render_skill_md([_candidate()], llm=llm, template=TEMPLATE)

    assert result.ok
    assert result.content == VALID.strip()


@pytest.mark.asyncio
async def test_crlf_reply_normalized(fake_llm_cls) -> None:
    llm = fake_llm_cls(VALID.replace("\n", "\r\n"))

    result = await render_skill_md([_candidate()], llm=llm, template=TEMPLATE)

    assert result.ok
    assert "\r" not in result.content


@pytest.mark.asyncio
async def test_invalid_then_valid_retries_once(fake_llm_cls) -> None:
    llm = fake_llm_cls(INVALID, VALID)

    result = await render_skill_md([_candidate()], llm=llm, template=TEMPLATE)

    assert result.ok and result.calls == 2
    first, second = llm.payloads
    assert second[:1] == first
    assert second[1] == ("ai", INVALID.strip())
    role, correction = second[2]
    assert role == "human"
    assert correction.startswith("Your previous reply is not a valid SKILL.md:\n- ")
    for fragment in (
        "task identifier",
        "description: must start with",
        "missing section '## Inputs'",
        "missing section '## Outputs'",
        "Workflow: needs at least 2",
    ):
        assert fragment in correction


@pytest.mark.asyncio
async def test_blank_first_reply_replayed_as_empty(fake_llm_cls) -> None:
    llm = fake_llm_cls("   ", VALID)

    result = await render_skill_md([_candidate()], llm=llm, template=TEMPLATE)

    assert result.ok
    assert llm.payloads[1][1] == ("ai", EMPTY_REPLY)


@pytest.mark.asyncio
async def test_invalid_twice_returns_errors_without_third_call(fake_llm_cls) -> None:
    llm = fake_llm_cls(INVALID, "no frontmatter at all")

    result = await render_skill_md([_candidate()], llm=llm, template=TEMPLATE)

    assert not result.ok and result.calls == 2
    assert len(llm.payloads) == 2 and llm.replies == []
    assert result.content == "no frontmatter at all"
    assert any("no YAML frontmatter" in error for error in result.validation.errors)


@pytest.mark.asyncio
async def test_update_passes_existing_text_and_enforces_name(fake_llm_cls) -> None:
    existing = format_existing_skill("real", "---\nname: real\n---\nold body\n")
    llm = fake_llm_cls(VALID, VALID.replace("name: build-before-test", "name: real"))

    result = await render_skill_md(
        [_update("real")], llm=llm, template=TEMPLATE, existing_skill=existing, expected_name="real"
    )

    assert result.ok and result.calls == 2
    assert existing in llm.payloads[0][0][1]
    assert "name: must stay 'real' (update), got 'build-before-test'" in llm.payloads[1][2][1]
    assert "name: real" in result.content


@pytest.mark.asyncio
async def test_taken_name_rejected_then_corrected(fake_llm_cls) -> None:
    llm = fake_llm_cls(VALID, VALID.replace("build-before-test", "build-first"))

    result = await render_skill_md(
        [_candidate()], llm=llm, template=TEMPLATE, taken_names={"build-before-test"}
    )

    assert result.ok and result.calls == 2
    assert "already exists in the skill library" in llm.payloads[1][2][1]


@pytest.mark.asyncio
async def test_guards_raise_before_any_call(fake_llm_cls) -> None:
    llm = fake_llm_cls(VALID)

    with pytest.raises(ValueError, match="no candidates"):
        await render_skill_md([], llm=llm, template=TEMPLATE)
    with pytest.raises(ValueError, match="placeholder"):
        await render_skill_md([_candidate()], llm=llm, template="no placeholders here")
    with pytest.raises(ValueError, match="go together"):
        await render_skill_md([_candidate()], llm=llm, template=TEMPLATE, expected_name="real")
    assert llm.payloads == []


@pytest.mark.asyncio
async def test_placeholder_text_inside_a_rule_is_not_resubstituted(fake_llm_cls) -> None:
    llm = fake_llm_cls(VALID)
    candidate = _candidate(rule="Keep the literal {existing_skill} marker.")

    await render_skill_md([candidate], llm=llm, template=TEMPLATE)

    instruction = llm.payloads[0][0][1]
    assert "Keep the literal {existing_skill} marker." in instruction
    assert instruction.count(NO_EXISTING_SKILL) == 1


# ------------------------------------------------------------------- prompt


def test_packaged_render_prompt_contract() -> None:
    text = PROMPT_PATH.read_text(encoding="utf-8")

    assert text.count(CANDIDATES_PLACEHOLDER) == 1
    assert text.count(EXISTING_SKILL_PLACEHOLDER) == 1
    assert "# REQUIRED SKILL.md STRUCTURE" in text
    assert "## Description must be proactive" in text
    assert "Do not create an empty `Examples` section." in text
    assert "# Output contract" in text
    assert text.index("# REQUIRED SKILL.md STRUCTURE") < text.index("# Output contract")
    for absent in ("{evidence_bundle}", "{skill_library}", "Nothing to save", "skills_list"):
        assert absent not in text
