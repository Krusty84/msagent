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


"""Read-only view of a stored experience graph for Skill Evolver.

Does not load trajectories, does not change CROSS_SESSION_LIMIT, and does
not invent evidence seqs. Missing shards yield an empty string.
"""

from __future__ import annotations

from pathlib import Path

from msagent.exgraph.sources import resolve_graph_dir
from msagent.exgraph.store import find_saved_graph, load_graph
from msagent.exgraph.workspace import load_overlay


def render_thread_context(
    thread_id: str,
    *,
    working_dir: Path | None = None,
    state_dir: Path | None = None,
) -> str:
    """Markdown appendix for the classify bundle. Empty if nothing is stored."""
    try:
        from msagent.exgraph.config import is_exgraph_enabled

        if not is_exgraph_enabled():
            return ""
        root = resolve_graph_dir(working_dir=working_dir, state_dir=state_dir)
        saved = find_saved_graph(root, thread_id)
        if saved is None:
            return ""
        graph = load_graph(saved)
    except Exception:
        return ""
    lines = ["## Experience graph (stored)", ""]
    lines.append(f"Thread `{graph.thread_id}` agent `{graph.agent}`.")
    for record in graph.cases.values():
        tools = " → ".join(str(name) for name in (record.sigma.get("tool_path") or [])[:8])
        extra = f" tools={tools}" if tools else ""
        lines.append(f"- Case `{record.run_id}` outcome={record.r}{extra}")
    episodes = [node for node in graph.nodes.values() if node.type == "Episode"]
    if episodes:
        lines.append("Episodes:")
        for node in episodes:
            kind = node.attrs.get("kind")
            weight = node.attrs.get("weight")
            lines.append(f"- {kind} weight={weight}")
    fixes = [edge for edge in graph.edges.values() if edge.type == "FIXED_BY"]
    if fixes:
        lines.append("Corrections:")
        for edge in fixes:
            lines.append(f"- {edge.src} fixed_by {edge.dst} via {edge.attrs.get('via')}")
    try:
        recipe_nodes, recipe_edges = load_overlay(root)
    except Exception:
        recipe_nodes, recipe_edges = [], []
    case_ids = set(graph.cases)
    hits = [edge for edge in recipe_edges if edge.get("src") in case_ids]
    if hits:
        by_id = {node.get("id"): node for node in recipe_nodes}
        lines.append("Recipes instantiated:")
        seen: set[str] = set()
        for edge in hits:
            node = by_id.get(edge.get("dst"))
            if not node:
                continue
            ngram = ">".join(str(part) for part in (node.get("ngram") or []))
            if ngram and ngram not in seen:
                seen.add(ngram)
                lines.append(f"- {ngram} support={node.get('support')}")
    if len(lines) <= 3:
        return ""
    return "\n".join(lines)
