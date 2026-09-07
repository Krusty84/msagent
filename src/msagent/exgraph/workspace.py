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


"""Workspace overlay for Recipes (cross-thread only).

Thread shards stay the source of truth for Cases. This overlay is rebuilt
from ``mine_cross_session`` over a caller-supplied pool — exgraph never
selects the last-N history itself. The pool size is the evolver's
``CROSS_SESSION_LIMIT`` when the CLI passes that list in.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from msagent.exgraph.schema import (
    SCHEMA_VERSION,
    Edge,
    ExperienceGraph,
    Node,
    edge_id,
    recipe_id,
)
from msagent.exgraph.store import WORKSPACE_NAME, _read_jsonl, _write_jsonl
from msagent.skill_evolver.features import FEATURES_VERSION, mine_cross_session
from msagent.trajectory_recorder.model import Trajectory


def workspace_dir(root: Path) -> Path:
    return Path(root) / WORKSPACE_NAME


def _contains_ngram(path: list[str], ngram: list[str]) -> bool:
    size = len(ngram)
    if size == 0 or size > len(path):
        return False
    return any(path[i : i + size] == ngram for i in range(len(path) - size + 1))


def rebuild_overlay(
    pool: list[Trajectory],
    graphs: Iterable[ExperienceGraph],
    root: Path,
) -> Path:
    """Replace the overlay from ``mine_cross_session(pool)``. Pool is caller-owned."""
    directory = workspace_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    recipes = mine_cross_session(pool) if len(pool) >= 2 else []
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    by_id = {graph.thread_id: graph for graph in graphs}
    for episode in recipes:
        ngram = list(episode.facts.get("ngram") or episode.tool_sequence)
        nid = recipe_id(ngram)
        nodes[nid] = Node(
            id=nid,
            type="Recipe",
            attrs={
                "ngram": ngram,
                "support": episode.facts.get("support"),
                "thread_ids": list(episode.facts.get("thread_ids") or []),
                "features_version": FEATURES_VERSION,
            },
        ).to_dict()
        for thread in episode.facts.get("thread_ids") or []:
            graph = by_id.get(thread)
            if graph is None:
                continue
            for record in graph.cases.values():
                path = [str(name) for name in (record.sigma.get("tool_path") or [])]
                if _contains_ngram(path, [str(part) for part in ngram]):
                    ident = edge_id("INSTANTIATES", record.id, nid)
                    edges[ident] = Edge(
                        id=ident,
                        type="INSTANTIATES",
                        src=record.id,
                        dst=nid,
                    ).to_dict()
    _write_jsonl(directory / "nodes.jsonl", list(nodes.values()))
    _write_jsonl(directory / "edges.jsonl", list(edges.values()))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": "workspace_overlay",
        "features_version": FEATURES_VERSION,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pool_threads": [traj.thread_id for traj in pool],
        "recipes": len(nodes),
        "edges": len(edges),
    }
    import json

    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return directory


def load_overlay(root: Path) -> tuple[list[dict], list[dict]]:
    directory = workspace_dir(root)
    if not directory.is_dir():
        return [], []
    return _read_jsonl(directory / "nodes.jsonl"), _read_jsonl(directory / "edges.jsonl")
