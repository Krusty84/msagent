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

"""Append-friendly JSONL store: upsert by id, never rewrite source trajectories."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from msagent.exgraph.schema import (
    SCHEMA_VERSION,
    CaseRecord,
    Edge,
    ExperienceGraph,
    Node,
)

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
WORKSPACE_NAME = "_workspace"



def graph_dirname(agent: str, thread_id: str) -> str:
    safe_agent = _UNSAFE_RE.sub("-", (agent or "agent").strip()) or "agent"
    safe_thread = _UNSAFE_RE.sub("-", thread_id.strip()) or "thread"
    return f"{safe_agent}_{safe_thread}"


def graph_dir(root: Path, graph: ExperienceGraph) -> Path:
    return Path(root) / graph_dirname(graph.agent, graph.thread_id)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _index_by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("id")
        if isinstance(key, str) and key:
            indexed[key] = row
    return indexed


def save_graph(graph: ExperienceGraph, root: Path) -> Path:
    """Upsert graph files under ``root/<agent>_<thread>/`` and return that dir."""
    directory = graph_dir(root, graph)
    directory.mkdir(parents=True, exist_ok=True)

    nodes = _index_by_id(_read_jsonl(directory / "nodes.jsonl"))
    edges = _index_by_id(_read_jsonl(directory / "edges.jsonl"))
    cases = _index_by_id(_read_jsonl(directory / "cases.jsonl"))
    for node in graph.nodes.values():
        nodes[node.id] = node.to_dict()
    for edge in graph.edges.values():
        edges[edge.id] = edge.to_dict()
    for record in graph.cases.values():
        cases[record.id] = record.to_dict()

    _write_jsonl(directory / "nodes.jsonl", list(nodes.values()))
    _write_jsonl(directory / "edges.jsonl", list(edges.values()))
    _write_jsonl(directory / "cases.jsonl", list(cases.values()))

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "thread_id": graph.thread_id,
        "agent": graph.agent,
        "source_path": graph.source_path,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nodes": len(nodes),
        "edges": len(edges),
        "cases": len(cases),
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return directory


def load_graph(directory: Path) -> ExperienceGraph:
    """Rehydrate a previously saved graph directory."""
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    thread_id = directory.name
    agent = ""
    source_path = ""
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
        if isinstance(manifest, dict):
            thread_id = str(manifest.get("thread_id") or thread_id)
            agent = str(manifest.get("agent") or "")
            source_path = str(manifest.get("source_path") or "")
    graph = ExperienceGraph(
        schema_version=SCHEMA_VERSION,
        thread_id=thread_id,
        agent=agent,
        source_path=source_path,
    )
    for row in _read_jsonl(directory / "nodes.jsonl"):
        node_id = row.get("id")
        node_type = row.get("type")
        if not isinstance(node_id, str) or not isinstance(node_type, str):
            continue
        attrs = {key: value for key, value in row.items() if key not in {"id", "type"}}
        graph.add_node(Node(id=node_id, type=node_type, attrs=attrs))  # type: ignore[arg-type]
    for row in _read_jsonl(directory / "edges.jsonl"):
        edge_ident = row.get("id")
        edge_type = row.get("type")
        src = row.get("src")
        dst = row.get("dst")
        if not all(isinstance(value, str) and value for value in (edge_ident, edge_type, src, dst)):
            continue
        attrs = {
            key: value
            for key, value in row.items()
            if key not in {"id", "type", "src", "dst"}
        }
        graph.add_edge(
            Edge(id=edge_ident, type=edge_type, src=src, dst=dst, attrs=attrs),  # type: ignore[arg-type]
        )
    for row in _read_jsonl(directory / "cases.jsonl"):
        try:
            graph.add_case(CaseRecord(**row))
        except TypeError:
            continue
    return graph


def find_saved_graph(root: Path, thread_id: str) -> Path | None:
    """Locate ``<agent>_<thread>`` under the graph root; unique prefix allowed."""
    root = Path(root)
    if not root.is_dir():
        return None
    exact = sorted(
        path for path in root.iterdir()
        if path.is_dir() and path.name != WORKSPACE_NAME and path.name.endswith(f"_{thread_id}")
    )
    if exact:
        return exact[0]
    prefixed = sorted(
        path for path in root.iterdir()
        if path.is_dir() and path.name != WORKSPACE_NAME and f"_{thread_id}" in path.name
    )
    return prefixed[0] if len(prefixed) == 1 else None
