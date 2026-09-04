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

"""/skill-mine: mine several recorded threads into SKILL.md proposals.

One proposal per thread: every selected trajectory is turned into episodes by
code, gated on ``min_evidence_score`` and only then classified and rendered by
the LLM. ``--dry-run`` stops after the code-only stage and prints the episode
table, so detector behaviour can be inspected without spending a token.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from rich import box
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from msagent.cli.bootstrap.initializer import initializer
from msagent.cli.theme import console, theme
from msagent.core.constants import SKILL_EVOLVER_CONFIG_FOLDER_NAME
from msagent.core.logging import get_logger
from msagent.skill_evolver.bundle import build_evidence_bundle
from msagent.skill_evolver.classify import classify
from msagent.skill_evolver.direct_skill_generation import (
    CROSS_SESSION_LIMIT,
    DirectSkillGenerationConfig,
    DirectSkillGenerationHandler,
    _collect_episodes,
)
from msagent.skill_evolver.features import Episode, evidence_score
from msagent.skill_evolver.render import (
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
    load_trajectories,
    load_trajectory,
)

logger = get_logger(__name__)

USAGE = "/skill-mine [--threads N] [--since 7d] [--dry-run] [--thread <id>]"
# Threads mined by default: at most two LLM calls each, and the evidence gate
# usually cuts that further. Cross-session support is unaffected by this number.
DEFAULT_THREADS = 5
# Thread ids are uuid4; this prefix stays unique in practice and is still a
# valid --thread argument (find_trajectory_file resolves unique prefixes).
THREAD_ID_WIDTH = 12
MAX_TOOLS_SHOWN = 5
MAX_SEQ_SHOWN = 8

# --since grammar. Minutes/months ('m') are rejected on purpose: the unit is
# ambiguous and guessing would mask a typo instead of reporting it.
_SINCE_RE = re.compile(r"^(\d+)([hdw])$")
_SINCE_SECONDS = {"h": 3600.0, "d": 86400.0, "w": 604800.0}

_REJECTED = "SKILL.md rejected after one correction; nothing written:"
_NO_TOOL_EVENTS_NOTE = " ".join(
    (
        "Threads showing 0 tools have no tool.* events recorded.",
        "Files written before the recorder's ignore_agent fix contain only",
        "turn.*, message.ai and llm.* events, so every detector that needs",
        "tool calls yields nothing",
        "(ARCHITECTURE_trajectory_recorder.md, section 11).",
        "Record a new session to get mineable evidence.",
    ),
)


class MineArgsError(ValueError):
    """The command line of /skill-mine is not valid."""


class MineSelectionError(ValueError):
    """No trajectory matches the requested selection."""


@dataclass(frozen=True, slots=True)
class MineOptions:
    """Parsed /skill-mine flags."""

    threads: int = DEFAULT_THREADS
    since: timedelta | None = None
    dry_run: bool = False
    thread: str | None = None


@dataclass(slots=True)
class ThreadStats:
    """One selected trajectory with its shape counters and mined episodes."""

    trajectory: Trajectory
    turns: int
    tool_calls: int
    ai_messages: int
    episodes: list[Episode] = field(default_factory=list)

    @property
    def thread_id(self) -> str:
        """Thread the trajectory was recorded for."""
        return self.trajectory.thread_id

    @property
    def score(self) -> float:
        """Summed weight of the episodes mined from this thread."""
        return evidence_score(self.episodes)


@dataclass(frozen=True, slots=True)
class StagePrompts:
    """Templates and resolved sources of the classify and render stages."""

    classify: str
    classify_source: str
    render: str
    render_source: str


@dataclass(slots=True)
class LazyLlm:
    """Builds the LLM on first use, so a dry or fully gated run creates none."""

    session: Any
    llm: Any = None
    model: str = ""

    async def get(self) -> Any:
        """The analysis model of the session, created once per run."""
        if self.llm is None:
            ctx = self.session.context
            config = await initializer.load_llm_config(ctx.model, ctx.working_dir)
            self.llm = initializer.llm_factory.create(config)
            self.model = config.model
        return self.llm


# ------------------------------------------------------------ argument parsing


def _parse_threads(raw: str) -> int:
    """Positive thread count of --threads."""
    try:
        value = int(raw)
    except ValueError:
        raise MineArgsError(
            f"--threads: expected a whole number, got '{raw}'",
        ) from None
    if value < 1:
        raise MineArgsError(f"--threads: must be at least 1, got '{raw}'")
    return value


def _parse_since(raw: str) -> timedelta:
    """Age window of --since: <count> followed by h, d or w."""
    match = _SINCE_RE.match(raw.strip().lower())
    if match is None:
        expected = "expected <count><unit> with unit h, d or w (24h, 7d, 2w)"
        raise MineArgsError(f"--since: {expected}, got '{raw}'")
    count = int(match.group(1))
    if count < 1:
        raise MineArgsError(f"--since: the count must be at least 1, got '{raw}'")
    return timedelta(seconds=count * _SINCE_SECONDS[match.group(2)])


def parse_mine_args(args: list[str]) -> MineOptions:
    """Parse the flags of /skill-mine; every violation raises MineArgsError.

    Hand-rolled on purpose: argparse reports errors with ``sys.exit``, and
    ``SystemExit`` is a ``BaseException`` that no guard in the CLI catches, so
    a mistyped flag would end the user's session.
    """
    threads: int | None = None
    since: timedelta | None = None
    thread: str | None = None
    dry_run = False

    index = 0
    while index < len(args):
        token = args[index]
        if token == "--dry-run":
            if dry_run:
                raise MineArgsError("--dry-run given twice")
            dry_run = True
            index += 1
            continue
        if token in ("--threads", "--since", "--thread"):
            if index + 1 >= len(args):
                raise MineArgsError(f"{token} requires a value")
            value = args[index + 1]
            if token == "--threads":
                if threads is not None:
                    raise MineArgsError("--threads given twice")
                threads = _parse_threads(value)
            elif token == "--since":
                if since is not None:
                    raise MineArgsError("--since given twice")
                since = _parse_since(value)
            else:
                if thread is not None:
                    raise MineArgsError("--thread given twice")
                thread = value.strip()
            index += 2
            continue
        raise MineArgsError(f"unknown argument '{token}'")

    if thread is not None and (threads is not None or since is not None):
        raise MineArgsError(
            " ".join(
                (
                    "--thread selects one thread and cannot be combined",
                    "with --threads or --since",
                ),
            ),
        )
    return MineOptions(
        threads=DEFAULT_THREADS if threads is None else threads,
        since=since,
        dry_run=dry_run,
        thread=thread,
    )


# ---------------------------------------------------------------- formatting


def short_thread(thread_id: str) -> str:
    """Clip a thread id to a still-resolvable prefix."""
    if len(thread_id) <= THREAD_ID_WIDTH:
        return thread_id
    return f"{thread_id[:THREAD_ID_WIDTH]}…"


def format_since(since: timedelta | None) -> str:
    """Human form of the --since window."""
    if since is None:
        return ""
    hours = int(since.total_seconds() // 3600)
    if hours % 168 == 0:
        return f"{hours // 168}w"
    if hours % 24 == 0:
        return f"{hours // 24}d"
    return f"{hours}h"


def format_tool_sequence(names: list[str]) -> str:
    """Tool names of an episode, prefixed with their true count."""
    if not names:
        return "[0] (none)"
    shown = " → ".join(names[:MAX_TOOLS_SHOWN])
    extra = len(names) - MAX_TOOLS_SHOWN
    suffix = f" … +{extra}" if extra > 0 else ""
    return f"[{len(names)}] {shown}{suffix}"


def format_evidence_seq(seqs: list[int]) -> str:
    """Cited event seqs of an episode, prefixed with their true count."""
    shown = ", ".join(str(seq) for seq in seqs[:MAX_SEQ_SHOWN])
    extra = len(seqs) - MAX_SEQ_SHOWN
    suffix = f" … +{extra}" if extra > 0 else ""
    return f"[{len(seqs)}] {shown}{suffix}"


def count_shape(trajectory: Trajectory) -> tuple[int, int, int]:
    """Turns, tool calls and AI messages of one trajectory."""
    turns = len(trajectory.turns)
    tool_calls = sum(len(turn.tool_calls) for turn in trajectory.turns)
    ai_messages = sum(len(turn.ai_messages) for turn in trajectory.turns)
    return turns, tool_calls, ai_messages


def build_threads_table(stats: list[ThreadStats], *, min_score: float) -> Table:
    """One row per selected thread: shape counters, score and the gate."""
    table = Table(
        box=box.SIMPLE_HEAD,
        title=f"Threads (min_evidence_score {min_score:.2f})",
        title_style="accent",
        title_justify="left",
        header_style="accent",
        border_style="border",
        expand=False,
        pad_edge=False,
    )
    table.add_column("thread", style="command", no_wrap=True)
    table.add_column("turns", justify="right", style="muted")
    table.add_column("tools", justify="right")
    table.add_column("ai", justify="right", style="muted")
    table.add_column("episodes", justify="right", style="secondary")
    table.add_column("score", justify="right", style="primary")
    table.add_column("gate", no_wrap=True)
    for item in stats:
        # A zero tool count is the single most diagnostic number here: it
        # means no detector that needs tool calls could ever fire.
        tools_style = "warning" if item.tool_calls == 0 else "muted"
        passes = item.score >= min_score
        gate = Text("pass", style="success") if passes else Text("skip", style="muted")
        table.add_row(
            escape(short_thread(item.thread_id)),
            str(item.turns),
            Text(str(item.tool_calls), style=tools_style),
            str(item.ai_messages),
            str(len(item.episodes)),
            f"{item.score:.2f}",
            gate,
        )
    return table


def build_episodes_table(stats: list[ThreadStats]) -> Table:
    """Every mined episode, grouped by thread, in detector order.

    Detector order, not weight order: the bundle sorts by weight for the
    model, while a human reading this table wants to know which detector
    fired.
    """
    table = Table(
        box=box.SIMPLE_HEAD,
        title="Episodes",
        title_style="accent",
        title_justify="left",
        header_style="accent",
        border_style="border",
        expand=False,
        pad_edge=False,
    )
    table.add_column("thread", style="command", no_wrap=True)
    table.add_column("kind", style="secondary", no_wrap=True)
    table.add_column("weight", justify="right", style="primary", no_wrap=True)
    table.add_column(
        "tool sequence",
        style="default",
        max_width=48,
        overflow="ellipsis",
    )
    table.add_column("evidence seq", style="muted", max_width=30, overflow="ellipsis")

    first_group = True
    for item in stats:
        if not item.episodes:
            continue
        if not first_group:
            table.add_section()
        first_group = False
        for position, episode in enumerate(item.episodes):
            label = escape(short_thread(item.thread_id)) if position == 0 else ""
            table.add_row(
                label,
                escape(episode.kind),
                f"{episode.weight:.2f}",
                escape(format_tool_sequence(episode.tool_sequence)),
                escape(format_evidence_seq(episode.evidence_seq)),
            )
        table.add_row(
            "",
            Text("subtotal", style="muted.bold"),
            Text(f"{item.score:.2f}", style="muted.bold"),
            "",
            "",
        )
    return table


# ----------------------------------------------------------------- selection


def select_trajectories(
    trajectories_dir: Path,
    *,
    agent: str,
    options: MineOptions,
    now: float,
) -> tuple[list[Trajectory], list[Trajectory]]:
    """Pick the threads to mine plus the cross-session pool behind them.

    The pool is the agent's newest ``CROSS_SESSION_LIMIT`` trajectories, the
    same set the single-thread command builds, so repeated procedures keep
    their support. ``--since`` compares file mtime, not ``started_at``: mtime
    is always present and means last activity, while ``started_at`` is the
    first event's timestamp and may be empty. Because the listing is already
    mtime-descending, the filter is a contiguous prefix. Note that mtime does
    not survive copying files between machines.
    """
    if not trajectories_dir.is_dir() or not any(trajectories_dir.glob("*.jsonl")):
        raise MineSelectionError(
            f"No trajectories recorded in {trajectories_dir}; run a session first",
        )

    listing = load_trajectories(
        trajectories_dir,
        agent=agent,
        limit=max(options.threads, CROSS_SESSION_LIMIT),
    )
    pool = listing[:CROSS_SESSION_LIMIT]

    if options.thread is not None:
        return [_resolve_thread(trajectories_dir, options.thread, pool)], pool

    if not listing:
        hint = "use --thread <id> for another agent's session"
        where = f"in {trajectories_dir}"
        raise MineSelectionError(
            f"No trajectories for agent '{agent}' {where}; {hint}",
        )

    candidates = listing
    if options.since is not None:
        oldest = now - options.since.total_seconds()
        candidates = []
        for trajectory in listing:
            if trajectory.path.stat().st_mtime >= oldest:
                candidates.append(trajectory)
        if not candidates:
            window = format_since(options.since)
            detail = f"was modified within {window}; widen --since or drop it"
            raise MineSelectionError(f"No thread of agent '{agent}' {detail}")
    return candidates[: options.threads], pool


def _resolve_thread(
    trajectories_dir: Path,
    thread: str,
    pool: list[Trajectory],
) -> Trajectory:
    """Resolve --thread to one trajectory; the id may be a unique prefix."""
    path = find_trajectory_file(trajectories_dir, thread)
    if path is None:
        # find_trajectory_file returns None both for "no match" and for an
        # ambiguous prefix, so one message has to cover both.
        hint = "give a full id or a unique prefix"
        where = f"in {trajectories_dir}"
        raise MineSelectionError(
            f"No recorded trajectory for thread '{thread}' {where}; {hint}",
        )
    for trajectory in pool:
        if trajectory.path == path:
            return trajectory
    return load_trajectory(path)


def mine_stats(
    targets: list[Trajectory],
    pool: list[Trajectory],
    skills: list[Skill],
) -> list[ThreadStats]:
    """Run the code-only detectors over every selected thread."""
    docs = [SkillDoc(skill.display_name, skill.description) for skill in skills]
    index = BM25Index(docs)
    stats: list[ThreadStats] = []
    for target in targets:
        others = [item for item in pool if item.thread_id != target.thread_id]
        # The target goes in first: mine_cross_session attributes an n-gram to
        # the first thread it sees, so the kept episodes cite this thread's own
        # events and the bundle needs only this trajectory.
        episodes = _collect_episodes(target, others, skill_index=index)
        turns, tool_calls, ai_messages = count_shape(target)
        stats.append(
            ThreadStats(
                trajectory=target,
                turns=turns,
                tool_calls=tool_calls,
                ai_messages=ai_messages,
                episodes=episodes,
            ),
        )
    return stats


class SkillMiningHandler:
    """Mine recorded threads into one SKILL.md proposal per thread."""

    def __init__(self, session) -> None:
        self.session = session
        # Only used for its prompt loader, which is an instance method. Its
        # __init__ just stores the session, and reaching _load_config /
        # _load_stage_prompt through the class keeps both commands seeing the
        # same (patchable) class attributes.
        self._generator = DirectSkillGenerationHandler(session)

    async def handle(self, args: list[str]) -> None:
        """Mine recent threads for SKILL.md proposals: --dry-run shows episodes."""
        try:
            options = parse_mine_args(args)
        except MineArgsError as exc:
            console.print_error(escape(str(exc)))
            console.print(f"[muted]{escape(USAGE)}[/muted]")
            console.print("")
            return

        try:
            await self._run(options)
        except MineSelectionError as exc:
            console.print_warning(escape(str(exc)))
            console.print("")
        except Exception as exc:
            console.print_error(escape(f"Error mining skills: {exc}"))
            console.print("")
            logger.exception("Skill mining failed")

    async def _run(self, options: MineOptions) -> None:
        """Select threads, extract evidence, then report or mine."""
        ctx = self.session.context
        cfg = DirectSkillGenerationHandler._load_config()
        state_dir = initializer.get_project_paths(Path(ctx.working_dir)).root
        trajectories_dir = resolve_trajectories_dir(state_dir=state_dir)
        skills = await self._load_skills()
        now = time.time()

        with self._status("Extracting evidence..."):
            stats = await asyncio.to_thread(
                self._gather,
                trajectories_dir,
                ctx.agent,
                options,
                skills,
                now,
            )

        self._report_selection(stats, options, agent=ctx.agent)
        console.console.print(
            build_threads_table(stats, min_score=cfg.min_evidence_score),
        )
        if any(item.tool_calls == 0 for item in stats):
            console.print(f"[muted]{_NO_TOOL_EVENTS_NOTE}[/muted]")

        if options.dry_run:
            self._report_dry_run(stats, cfg.min_evidence_score)
            return

        await self._mine(stats, cfg=cfg, skills=skills)

    @staticmethod
    def _gather(
        trajectories_dir: Path,
        agent: str,
        options: MineOptions,
        skills: list[Skill],
        now: float,
    ) -> list[ThreadStats]:
        """Blocking part of a run: read the JSONL files and detect episodes."""
        targets, pool = select_trajectories(
            trajectories_dir,
            agent=agent,
            options=options,
            now=now,
        )
        return mine_stats(targets, pool, skills)

    @staticmethod
    def _report_selection(
        stats: list[ThreadStats],
        options: MineOptions,
        *,
        agent: str,
    ) -> None:
        """Say what was selected before the tables are printed."""
        prefix = "Dry run: no LLM will be created. " if options.dry_run else ""
        if options.thread is not None:
            scope = f"Selected thread {options.thread}"
        else:
            window = format_since(options.since)
            scope = f"Selected the newest {len(stats)} of agent '{agent}'"
            if window:
                scope = f"{scope} modified within {window}"
        console.print_info(f"{prefix}{escape(scope)}")

    @staticmethod
    def _report_dry_run(stats: list[ThreadStats], min_score: float) -> None:
        """Episode table and the totals; no LLM exists at this point."""
        episodes = sum(len(item.episodes) for item in stats)
        if episodes:
            console.console.print(build_episodes_table(stats))
        else:
            console.print_info(f"No episodes detected in {len(stats)} threads.")

        total = sum(item.score for item in stats)
        passing = sum(1 for item in stats if item.score >= min_score)
        summary = " ".join(
            (
                f"Dry run: {len(stats)} threads, {episodes} episodes,",
                f"total evidence score {total:.2f};",
                f"{passing} threads would reach the LLM (up to {passing * 2}",
                "LLM calls). Nothing was written and no LLM was created.",
            ),
        )
        console.print_info(summary)
        console.print("")

    async def _mine(
        self,
        stats: list[ThreadStats],
        *,
        cfg: DirectSkillGenerationConfig,
        skills: list[Skill],
    ) -> None:
        """Run the LLM stages for every thread above the evidence threshold."""
        passing = [item for item in stats if item.score >= cfg.min_evidence_score]
        below = len(stats) - len(passing)
        if not passing:
            threshold = f"{cfg.min_evidence_score:.2f}"
            detail = f"below min_evidence_score {threshold}"
            console.print_info(f"Nothing to mine: {below} threads {detail}")
            console.print("")
            return

        prompts = await self._load_prompts(cfg)
        console.print_info(
            f"Mining {len(passing)} threads (up to {len(passing) * 2} LLM calls)",
        )
        llm_slot = LazyLlm(self.session)
        written = 0
        nothing = 0
        failed: list[str] = []
        for position, item in enumerate(passing, start=1):
            outcome = await self._mine_thread(
                item,
                cfg=cfg,
                prompts=prompts,
                skills=skills,
                llm_slot=llm_slot,
                position=position,
                total=len(passing),
            )
            if outcome == "written":
                written += 1
            elif outcome == "failed":
                failed.append(item.thread_id)
            else:
                nothing += 1

        summary = (
            f"Mined {len(stats)} threads: {written} proposals,"
            f" {below} below threshold, {nothing} nothing to save,"
            f" {len(failed)} failed"
        )
        if failed:
            console.print_error(summary)
            console.print_error(escape(f"Failed threads: {', '.join(failed)}"))
        elif written:
            console.print_success(summary)
        else:
            console.print_info(summary)
        console.print("")

    async def _mine_thread(
        self,
        stats: ThreadStats,
        *,
        cfg: DirectSkillGenerationConfig,
        prompts: StagePrompts,
        skills: list[Skill],
        llm_slot: LazyLlm,
        position: int,
        total: int,
    ) -> str:
        """Bundle → classify → render → validate → proposal for one thread.

        Returns ``written``, ``nothing`` or ``failed``. A failure is printed
        and logged with its thread id, and the loop continues: aborting would
        discard the remaining threads after earlier ones already wrote files.
        """
        thread_id = stats.thread_id
        header = (
            f"[{position}/{total}] thread {short_thread(thread_id)} —"
            f" score {stats.score:.2f}, {len(stats.episodes)} episodes"
        )
        console.print_info(escape(header))
        try:
            return await self._generate(
                stats,
                cfg=cfg,
                prompts=prompts,
                skills=skills,
                llm_slot=llm_slot,
            )
        except Exception as exc:
            console.print_error(escape(f"thread {thread_id}: {exc}"))
            logger.exception("Mining thread %s failed", thread_id)
            return "failed"

    async def _generate(
        self,
        stats: ThreadStats,
        *,
        cfg: DirectSkillGenerationConfig,
        prompts: StagePrompts,
        skills: list[Skill],
        llm_slot: LazyLlm,
    ) -> str:
        """The LLM stages of one thread; nothing is written unless valid."""
        current = stats.trajectory
        thread_id = stats.thread_id
        bundle_text, valid_seq = build_evidence_bundle(stats.episodes, [current])
        llm = await llm_slot.get()
        library = DirectSkillGenerationHandler._skill_library_snapshot(skills)
        with self._status("Classifying evidence..."):
            result = await classify(
                bundle_text,
                valid_seq,
                llm,
                prompts.classify.replace("{skill_library}", library),
            )
        if result.verdict == "nothing":
            console.print_info(
                f"Nothing to save: no durable learning found in thread {thread_id}",
            )
            return "nothing"

        plan = plan_render(result.candidates, skills)
        for candidate, reason in plan.dropped:
            console.print_warning(escape(f"Dropped '{candidate.title}': {reason}"))
        for note in plan.notes:
            console.print_info(escape(note))
        if not plan.accepted:
            console.print_info("Nothing to save: no candidate left to render")
            return "nothing"

        existing_text: str | None = None
        expected_name: str | None = None
        taken_names: set[str] = set()
        if plan.existing is None:
            taken_names = {s.name for s in skills} | {s.display_name for s in skills}
        else:
            read = plan.existing.path.read_text
            text = await asyncio.to_thread(read, encoding="utf-8")
            existing_text = format_existing_skill(plan.existing.display_name, text)
            expected_name = plan.existing.name
        with self._status("Rendering SKILL.md..."):
            rendered = await render_skill_md(
                plan.accepted,
                llm=llm,
                template=prompts.render,
                existing_skill=existing_text,
                expected_name=expected_name,
                taken_names=taken_names,
            )
        if not rendered.ok:
            console.print_error(_REJECTED)
            for error in rendered.validation.errors:
                console.print_error(escape(f"  - {error}"))
            return "nothing"

        name = skill_name(rendered.content)
        provenance = build_provenance(
            thread_ids=DirectSkillGenerationHandler._cited_threads(
                current,
                stats.episodes,
            ),
            episodes=stats.episodes,
            candidates=result.candidates,
            model=llm_slot.model,
            prompt_variants={
                "classify": prompts.classify_source,
                "render": prompts.render_source,
            },
            category=cfg.category,
            target=DirectSkillGenerationHandler._target_record(plan),
        )
        ctx = self.session.context
        output_root = cfg.output_dir or (Path(ctx.working_dir) / "skills")
        skill_path = await asyncio.to_thread(
            write_proposal,
            rendered.content,
            root=output_root,
            name=name,
            provenance=provenance,
            thread_id=thread_id,
        )
        console.print_success(escape(f"Skill proposal saved to {skill_path}"))
        library_dir = output_root / cfg.category
        hint = DirectSkillGenerationHandler._activation_hint(
            skill_path,
            name,
            plan,
            library_dir,
        )
        console.print(f"[muted]{escape(hint)}[/muted]")
        return "written"

    async def _load_prompts(self, cfg: DirectSkillGenerationConfig) -> StagePrompts:
        """Resolve the classify and render templates before any LLM call."""
        root = initializer.app_paths.home / SKILL_EVOLVER_CONFIG_FOLDER_NAME
        prompts_root = root / "prompts"
        loader = self._generator._load_stage_prompt
        classify_template, classify_source = await loader(
            prompts_root,
            cfg,
            "classify",
        )
        render_template, render_source = await loader(prompts_root, cfg, "render")
        return StagePrompts(
            classify=classify_template,
            classify_source=classify_source,
            render=render_template,
            render_source=render_source,
        )

    async def _load_skills(self) -> list[Skill]:
        """The skill catalogue of the current agent (rescanned, project first)."""
        ctx = self.session.context
        return await initializer.refresh_cached_skills(
            agent=ctx.agent,
            working_dir=ctx.working_dir,
        )

    @staticmethod
    def _status(text: str):
        """Spinner shown while a pipeline stage runs."""
        color = theme.spinner_color
        return console.console.status(f"[{color}]{text}[/{color}]")
