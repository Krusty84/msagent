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

"""/direct-skill-generation: distill a recorded session into a SKILL.md proposal.

The command never touches the analysed thread and never writes into the
skill library. Evidence is extracted from the thread's trajectory file by
code, classified and rendered by the LLM, validated by code again, and
written as a proposal under ``<skills root>/.proposals/`` for a human to
review and move.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from rich.markup import escape

from msagent.cli.bootstrap.initializer import initializer
from msagent.cli.handlers.session_history import load_history, trim_history
from msagent.cli.theme import console, theme
from msagent.core.constants import (
    CONFIG_SKILL_EVOLVER_FILE_NAME,
    SKILL_EVOLVER_CONFIG_FOLDER_NAME,
)
from msagent.core.logging import get_logger
from msagent.skill_evolver.bundle import EvidenceBundle, build_evidence_bundle
from msagent.skill_evolver.exgraph_context import attach_stored_graph
from msagent.skill_evolver.classify import (
    Candidate,
    classify,
    strip_code_fence,
    strip_think_blocks,
)
from msagent.skill_evolver.features import (
    DEFAULT_MIN_EVIDENCE_SCORE,
    GATE_NO_EPISODES,
    Episode,
    extract_episodes,
    gate_decision,
    mine_cross_session,
)
from msagent.skill_evolver.render import (
    RenderPlan,
    RenderPlans,
    format_existing_skill,
    plan_render,
    render_skill_md,
)
from msagent.skill_evolver.retrieval import BM25Index, SkillDoc
from msagent.skill_evolver.validator import skill_name
from msagent.skill_evolver.writer import build_provenance, write_proposal
from msagent.skills.factory import Skill
from msagent.trajectory_recorder.export import (
    find_trajectory_file,
    resolve_trajectories_dir,
)
from msagent.trajectory_recorder.reader import (
    Trajectory,
    load_trajectory,
    load_trajectories,
)

logger = get_logger(__name__)

DEFAULT_VARIANT = "default"
DEFAULT_CATEGORY = "default"
# Most render plans (SKILL.md proposals) per thread; each costs up to two
# LLM calls (render.plan_render defers the rest).
DEFAULT_MAX_PLANS = 3
# The evidence threshold (DEFAULT_MIN_EVIDENCE_SCORE) lives in features next
# to gate_decision, which applies it; it is imported above to stay a name of
# this module.
# Prompt stages of the evidence pipeline: folders under skill-evolver/prompts/.
STAGES = ("classify", "render")
# Newest trajectories of the agent mined for procedures shared across sessions.
CROSS_SESSION_LIMIT = 20
# Safe folder and file names inside the prompts root (no path separators).
_VARIANT_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")

_REJECTED = "SKILL.md rejected after one correction; nothing written:"
_DEPRECATION_HINT = "Use /skill-mine for trajectory-based generation."
_NO_MESSAGES_HINT = " ".join(
    (
        "Current thread has no messages.",
        "Use `/direct-skill-generation last` for the previous session.",
    ),
)

# ---------------------------------------------------------------------------
# Legacy replay path: ``_generate_skill_md`` replays the whole session to the
# LLM with the ``prompts/<active>/`` variant. ``handle()`` no longer uses it;
# the helpers stay until the evidence pipeline has proven itself.
_HISTORY_BUDGET_RATIO = 0.6  # part of context window for session reply
# Output-contract sentinel: the model found nothing worth saving. A stray pair
# of backticks is tolerated because models copy the phrase as inline code.
_NOTHING_TO_SAVE = re.compile(r"^\s*`?\s*Nothing to save\.?\s*`?\s*$", re.IGNORECASE)
_REPLAY_SYSTEM_PROMPT = (
    "You are reviewing a COMPLETED agent session. The conversation that follows is a "
    "replay of that session for analysis only. Do not continue it and do not execute "
    "its tasks. After the replay you will receive exactly one instruction message; "
    "follow only that instruction."
)
_OMITTED_NOTE = " ".join(
    (
        "[Note: {n} messages from the middle of the session were omitted",
        "due to context limits.]",
    ),
)


def _parse_min_evidence_score(raw: object) -> float:
    """Validate the configured evidence threshold; invalid values fall back."""
    if raw is None:
        return DEFAULT_MIN_EVIDENCE_SCORE
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = math.nan
    if math.isnan(value) or value < 0:
        logger.warning(
            "Invalid min_evidence_score %r; using %s",
            raw,
            DEFAULT_MIN_EVIDENCE_SCORE,
        )
        return DEFAULT_MIN_EVIDENCE_SCORE
    return value


def _parse_max_plans(raw: object) -> int:
    """Validate the configured plan ceiling; values below 1 or unparseable fall back."""
    if raw is None:
        return DEFAULT_MAX_PLANS
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = 0
    if value < 1:
        logger.warning("Invalid max_plans %r; using %s", raw, DEFAULT_MAX_PLANS)
        return DEFAULT_MAX_PLANS
    return value


@dataclass(frozen=True, slots=True)
class DirectSkillGenerationConfig:
    """Settings loaded from ~/.msagent/config/config.skill.evolver.yml."""

    # Variant folder of the legacy replay prompt (prompts/<active>/).
    active: str = DEFAULT_VARIANT
    # Prompt file inside each stage folder (prompts/classify/, prompts/render/)
    # and inside the legacy variant; unset = every *.md of the folder.
    prompt_file: str | None = None
    # Library category a proposal is meant for (recorded in its provenance).
    category: str = DEFAULT_CATEGORY
    # Root that receives .proposals/; default <working_dir>/skills.
    output_dir: Path | None = None
    # Threshold of features.gate_decision(): the incident score a session
    # needs for the LLM stages to run at all (a strong user correction passes
    # at or below the default); a thread without a recorded trajectory is
    # refused before that.
    min_evidence_score: float = DEFAULT_MIN_EVIDENCE_SCORE
    # Ceiling of render plans per thread (render.plan_render); plans past it
    # are reported as deferred and not rendered.
    max_plans: int = DEFAULT_MAX_PLANS


@dataclass(frozen=True, slots=True)
class PlanContext:
    """What every render plan of one thread shares: provenance inputs and the output root."""

    thread_id: str
    thread_ids: list[str]
    bundle: EvidenceBundle
    # The thread's classify rejections, recorded in every proposal's provenance.
    rejected: list[tuple[Candidate, str]]
    sources: dict[str, str]
    model: str
    prompt_variants: dict[str, str]
    category: str
    output_root: Path


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    """One plan's result: the written proposal, or the validation errors of the last try."""

    plan: RenderPlan
    calls: int
    skill_path: Path | None = None
    name: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def written(self) -> bool:
        return self.skill_path is not None


@dataclass(slots=True)
class PlanTally:
    """Counters over the render plans of one thread (or, summed, of a run)."""

    # Plans that ran: proposals + render_errors.
    plans: int = 0
    proposals: int = 0
    # A double validation failure or an exception inside the plan.
    render_errors: int = 0
    # Candidates whose target was unknown or ambiguous.
    rejected: int = 0
    deferred: int = 0

    @property
    def flagged(self) -> bool:
        """Whether anything besides clean proposals happened."""
        return bool(self.render_errors or self.rejected or self.deferred)

    def add(self, other: PlanTally) -> None:
        self.plans += other.plans
        self.proposals += other.proposals
        self.render_errors += other.render_errors
        self.rejected += other.rejected
        self.deferred += other.deferred

    def describe(self) -> str:
        """The plan-level part of a summary line."""
        return f"{self.render_errors} render errors, {self.rejected} rejected targets, {self.deferred} deferred"


def _collect_episodes(
    current: Trajectory,
    others: list[Trajectory],
    *,
    skill_index: BM25Index,
) -> list[Episode]:
    """Per-trajectory episodes plus the cross-session patterns the thread supports.

    ``current`` goes first into the mining, so every shared pattern it takes
    part in cites its own events and the evidence bundle needs only this
    trajectory.
    """
    episodes = extract_episodes(current, skill_index=skill_index)
    if not others:
        return episodes
    shared = mine_cross_session([current, *others])
    episodes.extend(e for e in shared if e.thread_id == current.thread_id)
    return episodes


def _supporting(others: list[Trajectory], episodes: list[Episode]) -> list[Trajectory]:
    """The trajectories among ``others`` that the episodes cite (a shared procedure's second session)."""
    cited = {item.ref.source for episode in episodes for item in episode.evidence}
    return [traj for traj in others if traj.source in cited]


class DirectSkillGenerationHandler:
    """Generate a SKILL.md proposal from a recorded thread without mutating it."""

    def __init__(self, session) -> None:
        self.session = session

    async def handle(self, args: list[str]) -> None:
        """Distill a thread into a SKILL.md proposal: [last|<thread-id>]."""
        console.print_info(_DEPRECATION_HINT)
        try:
            target = args[0].strip() if args else None
            thread_id, messages = await load_history(self.session, target)

            if not messages:
                if target is None:
                    console.print_warning(_NO_MESSAGES_HINT)
                else:
                    console.print_warning(f"No messages found for thread '{thread_id}'")
                console.print("")
                return

            await self._run_pipeline(thread_id)
        except Exception as exc:
            console.print_error(escape(f"Error generating skill: {exc}"))
            console.print("")
            logger.exception("Skill generation failed")

    # ------------------------------------------------------- evidence pipeline

    async def _run_pipeline(self, thread_id: str) -> None:
        """Trajectory → evidence → gate → classify → render → validate → proposal."""
        ctx = self.session.context
        cfg = self._load_config()
        root = initializer.app_paths.home / SKILL_EVOLVER_CONFIG_FOLDER_NAME / "prompts"
        classify_template, classify_source = await self._load_stage_prompt(
            root,
            cfg,
            "classify",
        )
        render_template, render_source = await self._load_stage_prompt(
            root,
            cfg,
            "render",
        )

        state_dir = initializer.get_project_paths(Path(ctx.working_dir)).root
        trajectories_dir = resolve_trajectories_dir(state_dir=state_dir)
        trajectory_path = find_trajectory_file(trajectories_dir, thread_id)
        if trajectory_path is None:
            where = f"thread {thread_id} in {trajectories_dir}"
            msg = f"No recorded trajectory for {where}; the LLM was not called"
            console.print_error(escape(msg))
            console.print("")
            return

        skills = await self._load_skills()
        with self._status(f"Extracting evidence from thread {thread_id}..."):
            current, supporting, episodes = await asyncio.to_thread(
                self._gather_evidence,
                trajectory_path,
                trajectories_dir,
                ctx.agent,
                skills,
            )
        decision = gate_decision(episodes, min_score=cfg.min_evidence_score)
        if not decision.passes:
            if decision.reason == GATE_NO_EPISODES:
                msg = f"Nothing to save: no episodes detected in thread {thread_id}"
            else:
                score = f"{decision.score:.2f} < min_evidence_score {cfg.min_evidence_score:.2f}"
                counts = f"{len(episodes)} episodes, {len(decision.incidents)} incidents"
                msg = f"Nothing to save: evidence score {score} ({counts})"
            console.print_info(msg)
            console.print("")
            return

        trajectories = [current, *supporting]
        bundle = build_evidence_bundle(episodes, trajectories)
        warnings, stop = self._report_bundle(bundle, cfg.min_evidence_score)
        for line in warnings:
            console.print_warning(escape(line))
        if stop is not None:
            console.print_info(stop)
            console.print("")
            return
        work = Path(ctx.working_dir)
        state = initializer.get_project_paths(work).root
        bundle_text = attach_stored_graph(
            bundle.text, current, working_dir=work, state_dir=state,
        )
        llm_config = await initializer.load_llm_config(ctx.model, ctx.working_dir)
        llm = initializer.llm_factory.create(llm_config)
        library = self._skill_library_snapshot(skills)
        with self._status("Classifying evidence..."):
            result = await classify(
                bundle_text,
                bundle.shown,
                llm,
                classify_template.replace("{skill_library}", library),
            )
        for candidate, reason in result.rejected:
            console.print_warning(escape(f"Rejected '{candidate.title}': {reason}"))
        if result.verdict == "nothing":
            msg = f"Nothing to save: no durable learning found in thread {thread_id}"
            console.print_info(msg)
            console.print("")
            return

        plans = plan_render(result.candidates, skills, max_plans=cfg.max_plans)
        warnings, notes = self._report_plans(plans)
        for line in warnings:
            console.print_warning(escape(line))
        for line in notes:
            console.print_info(escape(line))
        tally = PlanTally(rejected=len(plans.rejected), deferred=len(plans.deferred))
        if not plans.plans:
            console.print_info("Nothing to save: no candidate left to render")
            console.print("")
            return

        output_root = cfg.output_dir or (Path(ctx.working_dir) / "skills")
        library_dir = output_root / cfg.category
        taken = {s.name for s in skills} | {s.display_name for s in skills}
        context = PlanContext(
            thread_id=thread_id,
            thread_ids=self._cited_threads(current, episodes),
            bundle=bundle,
            rejected=result.rejected,
            sources={traj.source: str(traj.path) for traj in trajectories},
            model=llm_config.model,
            prompt_variants={"classify": classify_source, "render": render_source},
            category=cfg.category,
            output_root=output_root,
        )
        total = len(plans.plans)
        for position, plan in enumerate(plans.plans, start=1):
            label = plan.label
            tally.plans += 1
            try:
                status = f"Rendering SKILL.md (plan {position}/{total}: {escape(label)})..."
                with self._status(status):
                    outcome = await self._render_plan(
                        plan,
                        llm=llm,
                        template=render_template,
                        context=context,
                        taken=taken,
                    )
            except Exception as exc:
                tally.render_errors += 1
                console.print_error(escape(f"plan {label}: {exc}"))
                logger.exception("Rendering plan %s of thread %s failed", label, thread_id)
                continue
            if not outcome.written:
                tally.render_errors += 1
                console.print_error(escape(f"plan {label}: {_REJECTED}"))
                for error in outcome.errors:
                    console.print_error(escape(f"  - {error}"))
                continue
            tally.proposals += 1
            console.print_success(escape(f"Skill proposal saved to {outcome.skill_path}"))
            hint = self._activation_hint(outcome.skill_path, outcome.name, plan, library_dir)
            console.print(f"[muted]{escape(hint)}[/muted]")
        if tally.flagged or tally.plans > 1:
            line = f"Plans: {tally.proposals} proposals, {tally.describe()}"
            report = console.print_error if tally.render_errors else console.print_info
            report(line)
        sources = f"classify={classify_source}, render={render_source}"
        console.print(f"[muted]Prompts: {escape(sources)}[/muted]")
        console.print("")

    @staticmethod
    def _gather_evidence(
        trajectory_path: Path,
        trajectories_dir: Path,
        agent: str,
        skills: list[Skill],
    ) -> tuple[Trajectory, list[Trajectory], list[Episode]]:
        """Load the thread's trajectory plus the agent's newest; detect episodes.

        Returns the trajectory, the other trajectories its episodes cite (the
        evidence bundle must index them) and the episodes.
        """
        current = load_trajectory(trajectory_path)
        newest = load_trajectories(
            trajectories_dir,
            agent=agent,
            limit=CROSS_SESSION_LIMIT,
        )
        others = [traj for traj in newest if traj.thread_id != current.thread_id]
        docs = [SkillDoc(skill.display_name, skill.description) for skill in skills]
        episodes = _collect_episodes(current, others, skill_index=BM25Index(docs))
        return current, _supporting(others, episodes), episodes

    @staticmethod
    def _report_bundle(bundle: EvidenceBundle, min_score: float) -> tuple[list[str], str | None]:
        """One warning per excluded episode, and why nothing is saved when the kept ones fail the gate.

        An episode is excluded when even its required evidence does not fit
        the bundle budget (insufficient context). Its weight must not carry
        the thread to the LLM, so the gate is decided again on the kept
        episodes; a ``None`` second value means the pipeline goes on. The
        caller prints both (each handler has its own console).
        """
        excluded = [item.episode for item in bundle.episodes if item.status == "excluded"]
        warnings = [
            f"Excluded {episode.kind} (weight {episode.weight:.2f}): insufficient context for the bundle budget"
            for episode in excluded
        ]
        if not excluded:
            return warnings, None
        decision = gate_decision(bundle.kept, min_score=min_score)
        if decision.passes:
            return warnings, None
        stop = (
            f"Nothing to save: {len(excluded)} episodes excluded from the bundle; "
            f"remaining evidence score {decision.score:.2f} < min_evidence_score {min_score:.2f}"
        )
        return warnings, stop

    @staticmethod
    def _cited_threads(current: Trajectory, episodes: list[Episode]) -> list[str]:
        """The analysed thread first, then every thread a shared procedure relies on."""
        supporting: set[str] = set()
        for episode in episodes:
            supporting.update(episode.facts.get("thread_ids", []))
        supporting.discard(current.thread_id)
        return [current.thread_id, *sorted(supporting)]

    @staticmethod
    def _report_plans(plans: RenderPlans) -> tuple[list[str], list[str]]:
        """Warnings (rejected targets) and notes (references, deferred plans) for the console.

        The caller prints both (each handler has its own console).
        """
        warnings = [f"Rejected target '{item.candidate.title}': {item.code} — {item.detail}" for item in plans.rejected]
        notes = [f"already covered by {skill.display_name}: {candidate.title}" for candidate, skill in plans.references]
        notes.extend(f"Deferred plan: {plan.label} — {reason}" for plan, reason in plans.deferred)
        return warnings, notes

    @staticmethod
    async def _render_plan(
        plan: RenderPlan,
        *,
        llm: Any,
        template: str,
        context: PlanContext,
        taken: set[str],
    ) -> PlanOutcome:
        """Render one plan and write its proposal; never prints (the handlers do).

        A create checks its name against ``taken`` and, once written, adds
        the new name to it; an update passes ``expected_name`` instead and
        leaves ``taken`` alone. Exceptions propagate to the caller.
        """
        existing_text: str | None = None
        expected_name: str | None = None
        taken_names: Collection[str] = ()
        if plan.existing is None:
            taken_names = taken
        else:
            read = plan.existing.path.read_text
            text = await asyncio.to_thread(read, encoding="utf-8")
            existing_text = format_existing_skill(plan.existing.display_name, text)
            expected_name = plan.existing.name
        rendered = await render_skill_md(
            plan.candidates,
            llm=llm,
            template=template,
            existing_skill=existing_text,
            expected_name=expected_name,
            taken_names=taken_names,
            evidence=context.bundle.shown,
        )
        if not rendered.ok:
            return PlanOutcome(plan, rendered.calls, errors=list(rendered.validation.errors))

        name = skill_name(rendered.content)
        provenance = build_provenance(
            thread_ids=context.thread_ids,
            bundle=context.bundle,
            candidates=plan.candidates,
            rejected=context.rejected,
            sources=context.sources,
            model=context.model,
            prompt_variants=context.prompt_variants,
            category=context.category,
            target=DirectSkillGenerationHandler._target_record(plan),
        )
        skill_path = await asyncio.to_thread(
            write_proposal,
            rendered.content,
            root=context.output_root,
            name=name,
            provenance=provenance,
            thread_id=context.thread_id,
        )
        if plan.existing is None:
            taken.add(name)
        return PlanOutcome(plan, rendered.calls, skill_path=skill_path, name=name)

    @staticmethod
    def _target_record(plan: RenderPlan) -> dict[str, str | None]:
        """Provenance ``target``: new skill, or the library skill being revised."""
        if plan.existing is None:
            return {"action": "create", "existing_skill": None, "existing_path": None}
        return {
            "action": "update",
            "existing_skill": plan.existing.display_name,
            "existing_path": str(plan.existing.path),
        }

    @staticmethod
    def _activation_hint(
        skill_path: Path,
        name: str,
        plan: RenderPlan,
        library_dir: Path,
    ) -> str:
        """Tell the user the proposal is inactive and how to promote it by hand."""
        folder = skill_path.parent
        if plan.existing is None:
            return f"Not active: review it, then move {folder} to {library_dir / name}"
        return f"Not active: review it, then replace {plan.existing.path} with it"

    @staticmethod
    def _status(text: str):
        """Spinner shown while a pipeline stage runs."""
        color = theme.spinner_color
        return console.console.status(f"[{color}]{text}[/{color}]")

    async def _load_skills(self) -> list[Skill]:
        """The skill catalogue of the current agent (rescanned, project dir first)."""
        ctx = self.session.context
        return await initializer.refresh_cached_skills(
            agent=ctx.agent,
            working_dir=ctx.working_dir,
        )

    @staticmethod
    def _skill_library_snapshot(skills: list[Skill]) -> str:
        """Programmatic inventory injected as {skill_library} (replaces a tool call)."""
        if not skills:
            return "The skill library is currently empty."
        lines: list[str] = []
        for skill in sorted(skills, key=lambda s: s.display_name.casefold()):
            description = skill.description or "no description"
            lines.append(f"- {skill.display_name}: {description}")
        return "\n".join(lines)

    async def _build_skill_library_snapshot(self) -> str:
        """Legacy replay path: the snapshot of the freshly loaded catalogue."""
        return self._skill_library_snapshot(await self._load_skills())

    @classmethod
    def _sanitize_model_output(cls, text: str) -> str:
        """Remove reasoning blocks the model may emit around the payload."""
        return strip_think_blocks(text).strip()

    # ------------------------------------------------------------------ config

    @staticmethod
    def _load_config() -> DirectSkillGenerationConfig:
        """Read config.skill.evolver.yml; packaged default, then dataclass defaults."""
        from importlib.resources import files

        packaged_dir = Path(str(files("resources") / "configs" / "default"))
        candidates = (
            initializer.app_paths.config_dir / CONFIG_SKILL_EVOLVER_FILE_NAME.name,
            packaged_dir / CONFIG_SKILL_EVOLVER_FILE_NAME.name,
        )
        config_path = next((path for path in candidates if path.is_file()), None)
        if config_path is None:
            return DirectSkillGenerationConfig()

        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            logger.warning("Invalid %s; using defaults", config_path)
            return DirectSkillGenerationConfig()
        if not isinstance(data, dict):
            return DirectSkillGenerationConfig()

        active = str(data.get("active") or DEFAULT_VARIANT).strip()
        if not _VARIANT_NAME_PATTERN.fullmatch(active):
            logger.warning(
                "Unsafe variant name '%s'; using '%s'",
                active,
                DEFAULT_VARIANT,
            )
            active = DEFAULT_VARIANT

        raw_prompt_file = data.get("prompt_file")
        prompt_file = str(raw_prompt_file).strip() if raw_prompt_file else None
        if prompt_file and not _VARIANT_NAME_PATTERN.fullmatch(prompt_file):
            logger.warning("Unsafe prompt_file '%s'; ignoring it", prompt_file)
            prompt_file = None

        category = str(data.get("category") or DEFAULT_CATEGORY).strip()
        raw_output_dir = data.get("output_dir")
        output_dir = Path(str(raw_output_dir)).expanduser() if raw_output_dir else None
        min_evidence_score = _parse_min_evidence_score(data.get("min_evidence_score"))
        max_plans = _parse_max_plans(data.get("max_plans"))
        return DirectSkillGenerationConfig(
            active=active,
            prompt_file=prompt_file,
            category=category or DEFAULT_CATEGORY,
            output_dir=output_dir,
            min_evidence_score=min_evidence_score,
            max_plans=max_plans,
        )

    # ----------------------------------------------------------------- prompts

    async def _load_stage_prompt(
        self,
        root: Path,
        cfg: DirectSkillGenerationConfig,
        stage: str,
    ) -> tuple[str, str]:
        """Prompt of one evidence-pipeline stage: ``prompts/<stage>/``."""
        if stage not in STAGES:
            raise ValueError(f"Unknown prompt stage '{stage}'; expected {STAGES}")
        return await self._load_prompt_from(root, cfg, stage)

    async def _load_prompt_template(
        self,
        root: Path,
        cfg: DirectSkillGenerationConfig,
    ) -> tuple[str, str]:
        """Legacy replay prompt: the ``active`` variant folder."""
        return await self._load_prompt_from(root, cfg, cfg.active)

    async def _load_prompt_from(
        self,
        root: Path,
        cfg: DirectSkillGenerationConfig,
        folder: str,
    ) -> tuple[str, str]:
        """Resolve ``prompts/<folder>/``: user root first, then the packaged copy."""
        from importlib.resources import files as resource_files

        packaged_root = (
            Path(str(resource_files("resources") / "configs" / "default"))
            / SKILL_EVOLVER_CONFIG_FOLDER_NAME
            / "prompts"
        )

        for base in (root, packaged_root):
            prompt_dir = base / folder
            if not prompt_dir.is_dir():
                continue

            if cfg.prompt_file:
                candidate = prompt_dir / cfg.prompt_file
                found = [candidate] if candidate.is_file() else []
                if not found:
                    console.print_warning(
                        f"prompt_file '{cfg.prompt_file}' not found in {prompt_dir}; "
                        "using all *.md files of this folder instead"
                    )
                    found = sorted(prompt_dir.glob("*.md"))
            else:
                found = sorted(prompt_dir.glob("*.md"))

            if found:
                parts: list[str] = []
                for path in found:
                    text = await asyncio.to_thread(path.read_text, encoding="utf-8")
                    parts.append(text)
                source = str(found[0]) if len(found) == 1 else str(prompt_dir)
                return "\n\n".join(parts), source

        raise ValueError(
            f"No prompt in folder '{folder}': checked {root} and packaged defaults. "
            "Check config.skill.evolver.yml (prompt_file) and the seeded prompts."
        )

    # ------------------------------------------------- legacy replay (unused)

    @classmethod
    def _prepare_replay_messages(
        cls,
        messages: list[AnyMessage],
        llm,
        context_window: int | None,
    ) -> tuple[list[AnyMessage], int]:
        """Normalize and trim session messages so they replay as a valid chat."""
        normalized = [cls._normalize_replay_message(m) for m in messages]
        normalized = cls._drop_trailing_orphan_tool_calls(normalized)
        # head_tail: keep the task statement, cut the middle, keep the newest tail.
        return trim_history(
            normalized,
            llm,
            context_window,
            budget_ratio=_HISTORY_BUDGET_RATIO,
        )

    @staticmethod
    def _normalize_replay_message(message: AnyMessage) -> AnyMessage:
        """Fold private reasoning into visible text; strip fields APIs reject."""
        if not isinstance(message, AIMessage):
            return message
        kwargs = dict(message.additional_kwargs or {})
        reasoning = kwargs.pop("reasoning_content", None)
        if not reasoning:
            return message
        text = getattr(message, "text", None)
        if not isinstance(text, str):
            text = str(message.content)
        merged = f"<past_reasoning>\n{reasoning}\n</past_reasoning>\n\n{text}"
        return message.model_copy(
            update={"content": merged, "additional_kwargs": kwargs},
        )

    @staticmethod
    def _drop_trailing_orphan_tool_calls(
        messages: list[AnyMessage],
    ) -> list[AnyMessage]:
        """Drop a trailing tool call whose result never arrived (interrupted run)."""
        result = list(messages)
        while result:
            last = result[-1]
            if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
                result.pop()
                continue
            break
        return result

    async def _generate_skill_md(
        self,
        messages: list[AnyMessage],
        template: str,
        thread_id: str,
    ) -> str:
        """Replay the real session messages to the LLM, then send the instruction."""
        ctx = self.session.context
        llm_config = await initializer.load_llm_config(ctx.model, ctx.working_dir)
        llm = initializer.llm_factory.create(llm_config)

        replay, omitted = self._prepare_replay_messages(
            messages,
            llm,
            llm_config.context_window,
        )

        instruction = template
        for placeholder, value in (
            ("{skill_library}", await self._build_skill_library_snapshot()),
            ("{agent}", ctx.agent),
            ("{thread_id}", thread_id),
            ("{working_dir}", str(ctx.working_dir)),
            # Older templates carried a history placeholder; the replay replaces it.
            ("{history}", "(the full session is replayed above)"),
        ):
            instruction = instruction.replace(placeholder, value)
        if omitted:
            instruction = f"{_OMITTED_NOTE.format(n=omitted)}\n\n{instruction}"

        payload: list[AnyMessage] = [
            SystemMessage(content=_REPLAY_SYSTEM_PROMPT),
            *replay,
            HumanMessage(content=instruction),
        ]

        response = await llm.ainvoke(payload)
        text = getattr(response, "text", None)
        if not isinstance(text, str):
            text = str(response.content)
        return self._strip_code_fence(self._sanitize_model_output(text))

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """Unwrap the whole answer if the model fenced it despite instructions."""
        return strip_code_fence(text)
