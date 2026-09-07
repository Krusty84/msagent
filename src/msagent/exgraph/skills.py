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

Looks at Skill Evolver proposals (``<working_dir>/.proposals/<thread>/``)
and at ``SKILL.md`` files whose footer or sibling ``provenance.json`` lists
the thread id. Failures are ignored: missing folders are normal.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from msagent.exgraph.schema import Edge, ExperienceGraph, Node, edge_id, skill_doc_id

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FOOTER_THREAD = re.compile(r"thread:\s*([A-Za-z0-9._-]+)")


def _batch_dir_name(thread_id: str) -> str:
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


def _iter_skill_files(working_dir: Path, thread_id: str) -> list[Path]:
    found: list[Path] = []
    proposals = working_dir / ".proposals" / _batch_dir_name(thread_id)
    if proposals.is_dir():
        found.extend(sorted(proposals.rglob("SKILL.md")))
    skills_root = working_dir / "skills"
    if skills_root.is_dir():
        for path in sorted(skills_root.rglob("SKILL.md")):
            if _cites_thread(path, thread_id):
                found.append(path)
    # Dedup while keeping order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def attach_skill_docs(graph: ExperienceGraph, *, working_dir: Path | None) -> None:
    """Add SkillDoc nodes and DERIVED_SKILL edges when files exist."""
    if working_dir is None:
        return
    root = Path(working_dir)
    if not root.is_dir():
        return
    thread = graph.thread_id
    task = f"task:thread:{thread}"
    container = f"thread:{thread}"
    for path in _iter_skill_files(root, thread):
        nid = skill_doc_id(str(path))
        graph.add_node(
            Node(
                id=nid,
                type="SkillDoc",
                attrs={"path": str(path), "name": path.parent.name},
            ),
        )
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
