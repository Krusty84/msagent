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

"""LLM rendering of planned candidates into validated SKILL.md files.

:func:`plan_render` splits the candidates the classify stage kept into render
plans: one per new skill, one per library skill being updated; every named
target is resolved against the library once, and a candidate whose target is
unknown or ambiguous gets a rejection code, never a new plan.
:func:`render_skill_md` turns one plan into a ``SKILL.md``: the model receives
its candidates (and, for an update, the text of the existing skill) and
answers with the complete file. The reply is checked by
:mod:`msagent.skill_evolver.validator`; on failure the model gets exactly one
corrective turn listing every error, and a second failure is handed back to
the caller, who writes nothing. The LLM is duck-typed exactly as in
:mod:`msagent.skill_evolver.classify`. Stdlib + pydantic; this module never
writes files.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from msagent.skill_evolver.bundle import ShownFragment
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
# Most evidence fragments quoted to the renderer per candidate.
RENDER_EVIDENCE_LIMIT = 3
# Rejection codes of plan_render: the candidate names no library skill, or a
# bare name that exists in several categories.
INVALID_TARGET = "invalid_target"
AMBIGUOUS_TARGET = "ambiguous_target"

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
    """One render call: its candidates and the library skill they revise."""

    # Classification order; one candidate for a create, every kept update of
    # ``existing`` otherwise.
    candidates: list[Candidate]
    # The library skill every candidate updates; None for a new skill.
    existing: Skill | None

    @property
    def label(self) -> str:
        """Console/error line: ``update <skill> (N candidates)`` or ``create: <title>``."""
        if self.existing is None:
            return f"create: {self.candidates[0].title}"
        count = len(self.candidates)
        noun = "candidate" if count == 1 else "candidates"
        return f"update {self.existing.display_name} ({count} {noun})"


@dataclass(frozen=True, slots=True)
class PlanRejection:
    """A candidate no plan takes: ``code`` is INVALID_TARGET or AMBIGUOUS_TARGET."""

    candidate: Candidate
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class RenderPlans:
    """What plan_render decided for one thread; every candidate is in exactly one list."""

    # Plans to render, ordered by the first appearance of their first candidate.
    plans: list[RenderPlan]
    # Plans past ``max_plans`` with the reason; content and target untouched.
    deferred: list[tuple[RenderPlan, str]]
    # ``reference`` candidates naming a library skill: reported, never rendered.
    references: list[tuple[Candidate, Skill]]
    # Update/reference candidates whose target is unknown or ambiguous.
    rejected: list[PlanRejection]


def _resolve_target(
    wanted: str,
    by_display: Mapping[str, Skill],
    by_name: Mapping[str, Skill | None],
) -> Skill | str:
    """The library skill ``wanted`` names, or a rejection code.

    A display name wins; a bare name resolves only when it is unique across
    categories (``by_name`` stores None for a name found in several).
    """
    skill = by_display.get(wanted)
    if skill is not None:
        return skill
    if wanted in by_name:
        return by_name[wanted] or AMBIGUOUS_TARGET
    return INVALID_TARGET


def _rejection_detail(code: str, wanted: str) -> str:
    """Human text of a rejection code for one target name."""
    if code == AMBIGUOUS_TARGET:
        return f"existing_skill '{wanted}' is ambiguous"
    return f"existing_skill '{wanted}' is not in the skill library"


def plan_render(
    candidates: Sequence[Candidate],
    skills: Sequence[Skill],
    *,
    max_plans: int,
) -> RenderPlans:
    """Split the kept candidates into render plans; resolve every named target once.

    Each ``create`` is its own plan (no merging), every ``update`` of one
    library skill shares a plan, and ``reference`` candidates are checked
    with the same resolver and reported, never rendered. A target must name
    a library skill (display name, or a bare name that is unique across
    categories); otherwise the candidate is rejected with a warning and
    never turned into a ``create``. Plans beyond ``max_plans`` are deferred,
    not rendered.
    """
    by_display = {skill.display_name: skill for skill in skills}
    by_name: dict[str, Skill | None] = {}
    for skill in skills:
        by_name[skill.name] = None if skill.name in by_name else skill
    groups: list[tuple[Skill | None, list[Candidate]]] = []
    updates: dict[str, list[Candidate]] = {}
    references: list[tuple[Candidate, Skill]] = []
    rejected: list[PlanRejection] = []
    for candidate in candidates:
        target = candidate.target
        if target.action == "create":
            groups.append((None, [candidate]))
            continue
        wanted = (target.existing_skill or "").strip()
        resolved = _resolve_target(wanted, by_display, by_name)
        if isinstance(resolved, str):
            detail = _rejection_detail(resolved, wanted)
            logger.warning(
                "render: dropped candidate %r: %s",
                candidate.title,
                detail,
            )
            rejected.append(PlanRejection(candidate, resolved, detail))
            continue
        if target.action == "reference":
            references.append((candidate, resolved))
            continue
        group = updates.get(resolved.display_name)
        if group is None:
            # Registered on the first update, so the plan keeps the position
            # of its first candidate while later updates still join it.
            group = updates[resolved.display_name] = []
            groups.append((resolved, group))
        group.append(candidate)
    ordered = [RenderPlan(candidates=list(group), existing=skill) for skill, group in groups]
    reason = f"max_plans {max_plans} reached"
    deferred = [(plan, reason) for plan in ordered[max_plans:]]
    for plan, _ in deferred:
        logger.warning("render: deferred plan %s: %s", plan.label, reason)
    return RenderPlans(
        plans=ordered[:max_plans],
        deferred=deferred,
        references=references,
        rejected=rejected,
    )


def select_render_evidence(
    candidate: Candidate,
    evidence: Mapping[str, ShownFragment],
) -> list[ShownFragment]:
    """The fragments quoted to the renderer for one candidate.

    Its cited fragments, required ones first (stable otherwise), at most
    RENDER_EVIDENCE_LIMIT. Provenance records the same selection, so what
    the renderer saw is reproducible from the candidate and the bundle.
    """
    fragments = [evidence[ref] for ref in candidate.evidence_refs if ref in evidence]
    fragments.sort(key=lambda fragment: not fragment.required)
    return fragments[:RENDER_EVIDENCE_LIMIT]


def format_candidates(
    candidates: Sequence[Candidate],
    evidence: Mapping[str, ShownFragment] | None = None,
) -> str:
    """Numbered candidate blocks for the ``{candidates}`` placeholder.

    Each block carries the rule and its conditions (``When``,
    ``Constraints``, ``Expected outcome`` — only when the classifier filled
    them), the target, and the text of its evidence fragments. Fragment ids
    never appear: they belong in provenance, not in a prompt whose reply is
    the user-facing SKILL.md.
    """
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
        ]
        if candidate.applies_when:
            lines.append(f"   When: {candidate.applies_when}")
        if candidate.constraints:
            lines.append("   Constraints:")
            lines.extend(f"   - {constraint}" for constraint in candidate.constraints)
        if candidate.expected_outcome:
            lines.append(f"   Expected outcome: {candidate.expected_outcome}")
        lines.append(f"   Target: {where}")
        fragments = select_render_evidence(candidate, evidence or {})
        if fragments:
            lines.append("   Evidence:")
            lines.extend(f"   - {fragment.text}" for fragment in fragments)
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
    evidence: Mapping[str, ShownFragment] | None = None,
) -> RenderResult:
    """Ask the LLM for a SKILL.md, validate it, correct once, return the last try.

    ``existing_skill`` is the formatted text of the skill being updated and
    ``expected_name`` its name (both or neither). ``taken_names`` are library
    names a new skill must not reuse. ``evidence`` is the bundle's registry
    of shown fragments; each candidate is rendered with the text of its own
    (:func:`select_render_evidence`). Raises ``ValueError`` before any LLM
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
        "candidates": format_candidates(candidates, evidence),
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
