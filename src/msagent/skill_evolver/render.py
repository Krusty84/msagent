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

"""LLM rendering of accepted candidates into one validated SKILL.md.

The model receives the candidates the classify stage kept (and, for an
update, the text of the existing skill) and answers with a complete
``SKILL.md``. The reply is checked by :mod:`msagent.skill_evolver.validator`;
on failure the model gets exactly one corrective turn listing every error,
and a second failure is handed back to the caller, who writes nothing. The
LLM is duck-typed exactly as in :mod:`msagent.skill_evolver.classify`.
Stdlib + pydantic; this module never writes files.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from msagent.skill_evolver.classify import (
    EMPTY_REPLY,
    ERROR_TEXT_LIMIT,
    Candidate,
    reply_text,
    strip_code_fence,
    strip_think_blocks,
)
from msagent.skill_evolver.validator import ValidationResult, validate_skill_md
from msagent.skills.factory import Skill

logger = logging.getLogger(__name__)

CANDIDATES_PLACEHOLDER = "{candidates}"
EXISTING_SKILL_PLACEHOLDER = "{existing_skill}"
# Text of the "Existing skill" section when the proposal is a new skill.
NO_EXISTING_SKILL = "None. Create a new skill."

# One pass over the template, so a placeholder-looking string inside a rule
# or inside the existing skill text is never substituted.
_PLACEHOLDER_RE = re.compile(r"\{(candidates|existing_skill)\}")
_CORRECTION = (
    "Your previous reply is not a valid SKILL.md:\n{errors}\n\n"
    "Reply again with the complete corrected SKILL.md: frontmatter and every "
    "section, no code fences, nothing before or after it."
)


@dataclass(frozen=True, slots=True)
class RenderResult:
    """The last reply, its validation and the number of LLM calls (1 or 2)."""

    content: str
    validation: ValidationResult
    calls: int

    @property
    def ok(self) -> bool:
        return self.validation.ok


@dataclass(frozen=True, slots=True)
class RenderPlan:
    """What one render call gets, and what the classifier's output left out."""

    # Candidates to render: every ``create`` and every resolvable ``update``.
    accepted: list[Candidate]
    # The library skill every kept update points at; None for a new skill.
    existing: Skill | None
    # Console notes: reference candidates, several update targets.
    notes: list[str]
    # Update candidates naming no (or an ambiguous) library skill, with why.
    dropped: list[tuple[Candidate, str]]


def plan_render(
    candidates: Sequence[Candidate],
    skills: Sequence[Skill],
) -> RenderPlan:
    """Decide what gets rendered and which existing skill, if any, it revises.

    ``reference`` candidates are noted, not rendered: the library already
    holds the rule. An ``update`` must name a library skill (display name, or
    a bare name that is unique across categories); others are dropped with a
    warning, never turned into a ``create``. The existing skill is handed to
    the renderer only when every kept update points at the same skill.
    """
    by_display = {skill.display_name: skill for skill in skills}
    by_name: dict[str, Skill | None] = {}
    for skill in skills:
        by_name[skill.name] = None if skill.name in by_name else skill
    accepted: list[Candidate] = []
    notes: list[str] = []
    dropped: list[tuple[Candidate, str]] = []
    targets: dict[str, Skill] = {}
    for candidate in candidates:
        target = candidate.target
        if target.action == "reference":
            covered = target.existing_skill
            notes.append(f"already covered by {covered}: {candidate.title}")
            continue
        if target.action == "update":
            wanted = (target.existing_skill or "").strip()
            skill = by_display.get(wanted) or by_name.get(wanted)
            if skill is None:
                if wanted in by_name:
                    reason = f"existing_skill '{wanted}' is ambiguous"
                else:
                    reason = f"existing_skill '{wanted}' is not in the skill library"
                logger.warning(
                    "render: dropped candidate %r: %s",
                    candidate.title,
                    reason,
                )
                dropped.append((candidate, reason))
                continue
            targets[skill.display_name] = skill
        accepted.append(candidate)
    existing: Skill | None = None
    if len(targets) == 1:
        (existing,) = targets.values()
    elif len(targets) > 1:
        count = len(targets)
        names = ", ".join(sorted(targets))
        msg = f"{count} skills targeted ({names}); rendering a new skill instead"
        notes.append(msg)
    return RenderPlan(
        accepted=accepted,
        existing=existing,
        notes=notes,
        dropped=dropped,
    )


def format_candidates(candidates: Sequence[Candidate]) -> str:
    """Numbered candidate blocks for the ``{candidates}`` placeholder."""
    blocks: list[str] = []
    for number, candidate in enumerate(candidates, start=1):
        target = candidate.target
        if target.action == "create":
            where = "create a new skill"
        else:
            where = f"{target.action} `{target.existing_skill}`"
        applicability = candidate.future_applicability
        lines = [
            f"{number}. {candidate.title} (future applicability: {applicability})",
            f"   Rule: {candidate.rule}",
            f"   Target: {where}",
        ]
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def format_existing_skill(display_name: str, text: str) -> str:
    """Text of the ``{existing_skill}`` placeholder for an update."""
    intro = f"The candidates update the existing skill `{display_name}`; keep its name."
    return f"{intro} Current text:\n\n{text.strip()}\n"


def _clean(raw: str) -> str:
    """Strip reasoning blocks and a whole-reply fence; unify line endings."""
    text = strip_code_fence(strip_think_blocks(raw))
    return text.replace("\r\n", "\n").replace("\r", "\n")


async def render_skill_md(
    candidates: Sequence[Candidate],
    *,
    llm: Any,
    template: str,
    existing_skill: str | None = None,
    expected_name: str | None = None,
    taken_names: Collection[str] = (),
) -> RenderResult:
    """Ask the LLM for a SKILL.md, validate it, correct once, return the last try.

    ``existing_skill`` is the formatted text of the skill being updated and
    ``expected_name`` its name (both or neither). ``taken_names`` are library
    names a new skill must not reuse. Raises ``ValueError`` before any LLM
    call when there is nothing to render, the template lacks a placeholder,
    or the update arguments disagree. Whether the content may be written is
    ``result.validation.ok``.
    """
    if not candidates:
        raise ValueError("render: no candidates to render")
    placeholders = (CANDIDATES_PLACEHOLDER, EXISTING_SKILL_PLACEHOLDER)
    missing = [p for p in placeholders if p not in template]
    if missing:
        raise ValueError(f"render: template has no {missing} placeholder")
    if (existing_skill is None) != (expected_name is None):
        raise ValueError("render: existing_skill and expected_name go together")

    values = {
        "candidates": format_candidates(candidates),
        "existing_skill": existing_skill or NO_EXISTING_SKILL,
    }
    instruction = _PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], template)
    payload: list[tuple[str, str]] = [("human", instruction)]
    raw = reply_text(await llm.ainvoke(payload))
    content = _clean(raw)
    result = validate_skill_md(
        content,
        expected_name=expected_name,
        taken_names=taken_names,
    )
    if result.ok:
        return RenderResult(content=content, validation=result, calls=1)

    logger.warning("render: invalid SKILL.md, retrying once: %s", result.errors)
    bullets = "\n".join(f"- {error[:ERROR_TEXT_LIMIT]}" for error in result.errors)
    payload = [
        *payload,
        ("ai", strip_think_blocks(raw).strip() or EMPTY_REPLY),
        ("human", _CORRECTION.format(errors=bullets)),
    ]
    raw = reply_text(await llm.ainvoke(payload))
    content = _clean(raw)
    result = validate_skill_md(
        content,
        expected_name=expected_name,
        taken_names=taken_names,
    )
    return RenderResult(content=content, validation=result, calls=2)
