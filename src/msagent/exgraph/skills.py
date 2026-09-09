#!/usr/bin/python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This file is part of the MindStudio project.
#
# MindStudio is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of the Mulan PSL v2 at:
#
#    http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
# -------------------------------------------------------------------------

"""Attach SkillDoc nodes when a generated skill cites this thread.

P0.5 looks at Skill Evolver artifacts by path only; it imports only
``skill_evolver.config`` lazily (stdlib+yaml+pydantic, no pipeline modules):

- ``<working_dir>/skills/.proposals/<thread>/`` (current writer root)
- ``<output_dir>/.proposals/<thread>/`` when ``config.skill.evolver.yml``
  sets ``generation.output_dir`` (config loader, fail-open)
- ``<working_dir>/.proposals/<thread>/`` (legacy P0 path, still accepted)
- accepted library skills under ``skills/**/SKILL.md`` whose
  ``provenance.json`` or footer lists this thread

Proposal and accepted copies are different nodes: a review move must not
reuse a filesystem path as the identity.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

from msagent.exgraph.schema import (
    Edge,
    ExperienceGraph,
    Node,
    edge_id,
    skill_accepted_id,
    skill_proposal_id,
)

logger = logging.getLogger(__name__)

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FOOTER_THREAD = re.compile(r"thread:\s*([A-Za-z0-9._-]+)")
SkillStatus = Literal["proposal", "accepted"]


def _batch_dir_name(thread_id: str) -> str:
    """Path-safe thread folder. Must stay aligned with writer.batch_dir_name."""
    return _UNSAFE_RE.sub("-", thread_id.strip())[:64]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _cites_thread(path: Path, thread_id: str) -> bool:
    provenance = path.parent / "provenance.json"
    payload = _read_json(provenance)
    if payload is not None:
        ids = payload.get("thread_ids") or []
        if thread_id in ids:
            return True
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(match.group(1) == thread_id for match in _FOOTER_THREAD.finditer(text))


def _evolver_output_dir(working_dir: Path) -> Path | None:
    """Skill Evolver output_dir through the config loader; fail-open, a missing config is normal."""
    try:
        from msagent.skill_evolver.config import load_skill_evolver_config, resolve_output_dir

        return resolve_output_dir(load_skill_evolver_config(), working_dir)
    except Exception:
        logger.debug("Cannot read skill-evolver output_dir", exc_info=True)
        return None


def _unique_dirs(*dirs: Path | None) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for item in dirs:
        if item is None:
            continue
        try:
            resolved = item.resolve()
        except OSError:
            resolved = item
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(item)
    return out


def _is_under_proposals(path: Path) -> bool:
    return ".proposals" in path.parts


def _iter_skill_files(
    working_dir: Path,
    thread_id: str,
    *,
    output_dir: Path | None = None,
) -> list[tuple[Path, SkillStatus]]:
    batch = _batch_dir_name(thread_id)
    found: list[tuple[Path, SkillStatus]] = []
    proposal_roots = _unique_dirs(
        working_dir / "skills",
        output_dir,
        working_dir,
    )
    for root in proposal_roots:
        folder = root / ".proposals" / batch
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("SKILL.md")):
            found.append((path, "proposal"))

    accepted_roots = _unique_dirs(working_dir / "skills", output_dir)
    seen_files = {path.resolve() for path, _ in found}
    for root in accepted_roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("SKILL.md")):
            if _is_under_proposals(path):
                continue
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen_files:
                continue
            if _cites_thread(path, thread_id):
                found.append((path, "accepted"))
                seen_files.add(resolved)
    return found


def _category_for(path: Path, status: SkillStatus) -> str | None:
    if status != "accepted":
        return None
    parent = path.parent
    grand = parent.parent
    if grand.name and grand.name not in {".", "skills"}:
        return grand.name
    return None


def attach_skill_docs(
    graph: ExperienceGraph,
    *,
    working_dir: Path | None,
    output_dir: Path | None = None,
) -> None:
    """Add SkillDoc nodes and DERIVED_SKILL edges when files exist."""
    if working_dir is None:
        return
    root = Path(working_dir)
    if not root.is_dir():
        return
    extra = output_dir if output_dir is not None else _evolver_output_dir(root)
    thread = graph.thread_id
    task = f"task:thread:{thread}"
    container = f"thread:{thread}"
    for path, status in _iter_skill_files(root, thread, output_dir=extra):
        name = path.parent.name
        nid = (
            skill_proposal_id(thread, name)
            if status == "proposal"
            else skill_accepted_id(name)
        )
        attrs: dict[str, Any] = {
            "path": str(path),
            "name": name,
            "status": status,
        }
        category = _category_for(path, status)
        if category:
            attrs["category"] = category
        graph.add_node(Node(id=nid, type="SkillDoc", attrs=attrs))
        for src in (container, task):
            if src in graph.nodes:
                graph.add_edge(
                    Edge(
                        id=edge_id("DERIVED_SKILL", src, nid),
                        type="DERIVED_SKILL",
                        src=src,
                        dst=nid,
                    ),
                )
