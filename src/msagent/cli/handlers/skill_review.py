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

"""/skill-review: list, accept or reject the SKILL.md proposals on disk.

Proposals live in ``<root>/.proposals/<thread>/<name>/`` where no skill
scanner can see them. Accepting one re-validates it (it may have been edited
by hand) and moves the whole folder into ``<root>/<category>/<name>/``;
rejecting one deletes it after a confirmation.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.shortcuts import CompleteStyle
from rich import box
from rich.markup import escape
from rich.table import Table

from msagent.cli.theme import console
from msagent.cli.ui.shared import create_bottom_toolbar, create_prompt_style
from msagent.core.logging import get_logger
from msagent.skill_evolver.validator import skill_name, validate_skill_md
from msagent.skill_evolver.writer import PROPOSALS_DIR, PROVENANCE_FILE, SKILL_FILE
from msagent.skills.factory import SkillFactory
from msagent.utils.time import format_relative_time

logger = get_logger(__name__)

USAGE = "/skill-review [list | accept <name> | reject <name>]"
THREAD_ID_WIDTH = 12


@dataclass(slots=True)
class Proposal:
    """One proposal folder: its identity, metadata and any read error."""

    path: Path
    batch: str
    name: str
    description: str = ""
    category: str = ""
    thread_ids: list[str] | None = None
    generated_at: str = ""
    action: str = ""
    existing_path: str = ""
    error: str = ""

    @property
    def qualified(self) -> str:
        """Unambiguous identifier of this proposal."""
        return f"{self.batch}/{self.name}"


def _short(value: str) -> str:
    """Clip a thread id for display."""
    if len(value) <= THREAD_ID_WIDTH:
        return value
    return f"{value[:THREAD_ID_WIDTH]}…"


def load_proposals(root: Path) -> list[Proposal]:
    """Read every proposal under ``<root>/.proposals/``, newest batch first.

    A proposal whose metadata cannot be read is returned with ``error`` set
    rather than skipped: a half-written folder must stay visible to the human
    who has to decide about it.
    """
    base = root / PROPOSALS_DIR
    if not base.is_dir():
        return []

    proposals: list[Proposal] = []
    for batch_dir in sorted(base.iterdir(), key=_mtime_desc):
        if not batch_dir.is_dir():
            continue
        for skill_dir in sorted(batch_dir.iterdir(), key=_mtime_desc):
            if not skill_dir.is_dir():
                continue
            proposals.append(_read_proposal(skill_dir, batch_dir.name))
    return proposals


def _mtime_desc(path: Path) -> float:
    """Sort key placing the most recently written entry first."""
    return -path.stat().st_mtime


def _read_proposal(skill_dir: Path, batch: str) -> Proposal:
    """Metadata of one proposal folder."""
    proposal = Proposal(path=skill_dir, batch=batch, name=skill_dir.name)
    skill_file = skill_dir / SKILL_FILE
    if not skill_file.is_file():
        proposal.error = f"missing {SKILL_FILE}"
        return proposal

    try:
        content = skill_file.read_text(encoding="utf-8")
        frontmatter = SkillFactory.parse_frontmatter(content)
        proposal.description = str(frontmatter.get("description") or "")
    except (OSError, ValueError) as exc:
        proposal.error = f"unreadable {SKILL_FILE}: {exc}"
        return proposal

    provenance_file = skill_dir / PROVENANCE_FILE
    try:
        provenance = json.loads(provenance_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        proposal.error = f"unreadable {PROVENANCE_FILE}: {exc}"
        return proposal
    if not isinstance(provenance, dict):
        proposal.error = f"{PROVENANCE_FILE} is not an object"
        return proposal

    proposal.category = str(provenance.get("category") or "")
    threads = provenance.get("thread_ids")
    proposal.thread_ids = [str(item) for item in threads] if threads else []
    proposal.generated_at = str(provenance.get("generated_at") or "")
    target = provenance.get("target")
    if isinstance(target, dict):
        proposal.action = str(target.get("action") or "")
        proposal.existing_path = str(target.get("existing_path") or "")
    return proposal


def build_proposals_table(proposals: list[Proposal]) -> Table:
    """One row per proposal, newest first."""
    table = Table(
        box=box.SIMPLE_HEAD,
        title="Skill proposals",
        title_style="accent",
        title_justify="left",
        header_style="accent",
        border_style="border",
        expand=False,
        pad_edge=False,
    )
    table.add_column("name", style="command", no_wrap=True)
    table.add_column("category", style="secondary", no_wrap=True)
    table.add_column("action", style="secondary", no_wrap=True)
    table.add_column("threads", style="muted", no_wrap=True)
    table.add_column("generated", style="timestamp", no_wrap=True)
    table.add_column("description", style="default", overflow="ellipsis")
    for proposal in proposals:
        if proposal.error:
            table.add_row(
                escape(proposal.name),
                "?",
                "?",
                escape(proposal.batch and _short(proposal.batch) or "?"),
                "?",
                escape(f"unreadable: {proposal.error}"),
            )
            continue
        threads = ", ".join(_short(item) for item in proposal.thread_ids or [])
        table.add_row(
            escape(proposal.name),
            escape(proposal.category or "default"),
            escape(proposal.action or "create"),
            escape(threads or _short(proposal.batch)),
            escape(format_relative_time(proposal.generated_at)),
            escape(proposal.description or "(no description)"),
        )
    return table


class SkillReviewHandler:
    """Review the proposals written by /skill-mine and /direct-skill-generation."""

    def __init__(self, session) -> None:
        self.session = session

    async def handle(self, args: list[str]) -> None:
        """Review skill proposals: [list | accept <name> | reject <name>]."""
        try:
            action = args[0].lower() if args else "list"
            if action == "list":
                await self._list()
            elif action == "accept":
                await self._accept(args[1:])
            elif action == "reject":
                await self._reject(args[1:])
            else:
                console.print_error(f"Unknown subcommand: {escape(action)}")
                console.print(f"[muted]{escape(USAGE)}[/muted]")
                console.print("")
        except Exception as exc:
            console.print_error(escape(f"Error reviewing proposals: {exc}"))
            console.print("")
            logger.exception("Skill review failed")

    def _root(self) -> Path:
        """Skills root that receives ``.proposals/``."""
        # Imported here, not at module level: handlers/__init__ loads this
        # module before the generator, which re-enters the package for
        # session_history (the pre-existing cycle).
        from msagent.skill_evolver.direct_skill_generation import (
            DirectSkillGenerationHandler,
        )

        ctx = self.session.context
        cfg = DirectSkillGenerationHandler._load_config()
        return cfg.output_dir or (Path(ctx.working_dir) / "skills")

    async def _list(self) -> None:
        """Print every proposal on disk."""
        root = self._root()
        proposals = await asyncio.to_thread(load_proposals, root)
        if not proposals:
            console.print_info(f"No proposals in {root / PROPOSALS_DIR}")
            console.print("")
            return
        console.console.print(build_proposals_table(proposals))
        console.print("")

    def _resolve(self, proposals: list[Proposal], wanted: str) -> Proposal:
        """Find one proposal by name, or by ``<thread>/<name>`` when ambiguous."""
        if "/" in wanted:
            matches = [item for item in proposals if item.qualified == wanted]
        else:
            matches = [item for item in proposals if item.name == wanted]
        if not matches:
            raise LookupError(f"No proposal named '{wanted}'")
        if len(matches) > 1:
            names = ", ".join(item.qualified for item in matches)
            raise LookupError(
                f"'{wanted}' exists in several batches; use one of: {names}",
            )
        return matches[0]

    async def _selected(self, args: list[str], verb: str) -> Proposal | None:
        """Resolve the proposal named on the command line, or report why not."""
        if not args:
            console.print_error(f"{verb} requires a proposal name")
            console.print(f"[muted]{escape(USAGE)}[/muted]")
            console.print("")
            return None
        root = self._root()
        proposals = await asyncio.to_thread(load_proposals, root)
        try:
            return self._resolve(proposals, args[0].strip())
        except LookupError as exc:
            console.print_error(escape(str(exc)))
            console.print("")
            return None

    async def _accept(self, args: list[str]) -> None:
        """Validate a proposal again, then move it into its library category."""
        proposal = await self._selected(args, "accept")
        if proposal is None:
            return
        if proposal.error:
            console.print_error(
                escape(f"{proposal.qualified} is unreadable: {proposal.error}"),
            )
            console.print("")
            return

        if proposal.action == "update":
            # An update proposal carries the name of a library skill. Moving it
            # into <root>/<category>/<name>/ would create a second skill with
            # that name in another category, so the file is replaced by hand.
            target = proposal.existing_path or "the skill it revises"
            console.print_warning(
                escape(f"'{proposal.name}' revises an existing skill"),
            )
            hint = f"Replace {target} with {proposal.path / SKILL_FILE}"
            console.print(f"[muted]{escape(hint)}[/muted]")
            console.print("")
            return

        skill_file = proposal.path / SKILL_FILE
        content = await asyncio.to_thread(skill_file.read_text, encoding="utf-8")
        validation = validate_skill_md(content)
        if not validation.ok:
            console.print_error(
                escape(f"{proposal.qualified} is not a valid SKILL.md:"),
            )
            for error in validation.errors:
                console.print_error(escape(f"  - {error}"))
            console.print("")
            return

        name = skill_name(content)
        category = proposal.category or "default"
        destination = self._root() / category / name
        if destination.exists():
            console.print_error(escape(f"{destination} already exists; not moved"))
            console.print("")
            return

        await asyncio.to_thread(self._move, proposal.path, destination)
        console.print_success(escape(f"Accepted '{name}' into {destination}"))
        console.print("[muted]It appears in /skills after the next refresh.[/muted]")
        console.print("")

    @staticmethod
    def _move(source: Path, destination: Path) -> None:
        """Move a proposal folder, provenance.json included."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        _prune_batch(source.parent)

    async def _reject(self, args: list[str]) -> None:
        """Delete a proposal folder after an explicit confirmation."""
        proposal = await self._selected(args, "reject")
        if proposal is None:
            return

        console.print_warning(escape(f"About to delete {proposal.path}"))
        if not await self._confirm(f"Delete proposal '{proposal.name}'?"):
            console.print_info("Cancelled; nothing was deleted")
            console.print("")
            return

        await asyncio.to_thread(self._remove, proposal.path)
        console.print_success(escape(f"Rejected '{proposal.name}'"))
        console.print("")

    @staticmethod
    def _remove(path: Path) -> None:
        """Delete a proposal folder and its batch when that becomes empty."""
        shutil.rmtree(path)
        _prune_batch(path.parent)

    async def _confirm(self, question: str) -> bool:
        """Ask for an explicit yes; anything else (or Ctrl+C) means no."""
        ctx = self.session.context
        console.print(f"[accent]{escape(question)}[/accent] [muted](y/N)[/muted]")
        try:
            session: Any = PromptSession(
                completer=WordCompleter(["yes", "no"], ignore_case=True),
                complete_style=CompleteStyle.COLUMN,
                style=create_prompt_style(ctx),
                bottom_toolbar=create_bottom_toolbar(ctx, ctx.working_dir),
            )
            answer = await session.prompt_async("> ")
        except (KeyboardInterrupt, EOFError):
            return False
        return answer.strip().lower() in ("y", "yes")


def _prune_batch(batch_dir: Path) -> None:
    """Remove a ``.proposals/<thread>/`` folder once its last skill is gone."""
    try:
        if batch_dir.is_dir() and not any(batch_dir.iterdir()):
            batch_dir.rmdir()
    except OSError as exc:
        logger.debug("Could not prune %s: %s", batch_dir, exc)
