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

"""CLI for the experience graph.

    python -m msagent.exgraph.export build --thread <id> [--outcome success|fail]
    python -m msagent.exgraph.export show --thread <id>
    python -m msagent.exgraph.export export --thread <id> --format json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from msagent.exgraph.cases import build_graph
from msagent.exgraph.config import load_exgraph_config
from msagent.exgraph.schema import ExperienceGraph, OutcomeOverride
from msagent.exgraph.skills import attach_skill_docs
from msagent.exgraph.sources import load_source_trajectory, resolve_graph_dir
from msagent.exgraph.store import graph_dirname, load_graph, save_graph


def _clip(text: str | None, limit: int = 160) -> str:
    if not text:
        return ""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


def render_markdown(graph: ExperienceGraph) -> str:
    """Human-readable tree of cases and steps."""
    lines: list[str] = [
        f"# Experience graph: {graph.agent} / {graph.thread_id}",
        "",
        f"- source: `{graph.source_path}`",
        f"- nodes: {len(graph.nodes)}  edges: {len(graph.edges)}  cases: {len(graph.cases)}",
        "",
    ]
    cases = list(graph.cases.values())
    for index, record in enumerate(cases, start=1):
        lines.append(f"## Case {index} `{record.run_id}` — {record.r}")
        if record.x:
            lines.append(f"**Task:** {_clip(record.x)}")
        tool_path = record.sigma.get("tool_path") or []
        if tool_path:
            lines.append(f"**Tools:** {' → '.join(str(name) for name in tool_path)}")
        errors = record.sigma.get("errors") or []
        for error in errors:
            lines.append(
                f"- error `{error.get('tool')}` {error.get('type') or ''}: "
                f"{_clip(str(error.get('error') or ''), 120)}",
            )
        if record.sigma.get("approval_edited"):
            lines.append("- approval edited arguments")
        if record.y:
            lines.append(f"**Output:** {_clip(record.y)}")
        lines.append("")
    skills = [node for node in graph.nodes.values() if node.type == "SkillDoc"]
    if skills:
        lines.append("## Skills derived from this thread")
        for node in skills:
            lines.append(f"- {node.attrs.get('name')}: `{node.attrs.get('path')}`")
        lines.append("")
    return "\n".join(lines)


def _graph_root(args: argparse.Namespace) -> Path:
    return resolve_graph_dir(
        working_dir=Path(args.working_dir) if args.working_dir else None,
        state_dir=Path(args.state_dir) if args.state_dir else None,
    )


def _find_saved(root: Path, thread_id: str) -> Path | None:
    if not root.is_dir():
        return None
    exact = sorted(root.glob(f"*_{thread_id}"))
    if exact:
        return exact[0]
    prefixed = sorted(root.glob(f"*_{thread_id}*"))
    return prefixed[0] if len(prefixed) == 1 else None


def cmd_build(args: argparse.Namespace) -> int:
    config = load_exgraph_config()
    if not config.is_active:
        print("Experience graph is disabled (config or MSAGENT_EXGRAPH_DISABLED)", file=sys.stderr)
        return 1
    outcome: OutcomeOverride | None = args.outcome
    source, trajectory = load_source_trajectory(
        thread_id=args.thread or "",
        working_dir=Path(args.working_dir) if args.working_dir else None,
        state_dir=Path(args.state_dir) if args.state_dir else None,
        path=Path(args.path) if args.path else None,
    )
    graph = build_graph(trajectory, outcome=outcome, outcome_policy=config.outcome.policy)
    if config.skills.enabled:
        working = Path(trajectory.working_dir) if trajectory.working_dir else None
        if args.working_dir:
            working = Path(args.working_dir)
        attach_skill_docs(graph, working_dir=working)
    directory = save_graph(graph, _graph_root(args))
    print(
        f"Built {graph.thread_id}: {len(graph.cases)} cases, "
        f"{len(graph.nodes)} nodes, {len(graph.edges)} edges → {directory}",
    )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    if args.path:
        from msagent.exgraph.cases import build_from_path

        graph = build_from_path(Path(args.path), outcome=args.outcome)
    else:
        if not args.thread:
            print("--thread is required unless --path is set", file=sys.stderr)
            return 2
        saved = _find_saved(_graph_root(args), args.thread)
        if saved is None:
            # Build in memory from the trajectory without writing.
            _, trajectory = load_source_trajectory(
                thread_id=args.thread,
                working_dir=Path(args.working_dir) if args.working_dir else None,
                state_dir=Path(args.state_dir) if args.state_dir else None,
            )
            graph = build_graph(trajectory, outcome=args.outcome)
        else:
            graph = load_graph(saved)
    print(render_markdown(graph))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    if args.path:
        from msagent.exgraph.cases import build_from_path

        graph = build_from_path(Path(args.path), outcome=args.outcome)
    else:
        if not args.thread:
            print("--thread is required unless --path is set", file=sys.stderr)
            return 2
        saved = _find_saved(_graph_root(args), args.thread)
        if saved is None:
            print(f"No saved graph for thread '{args.thread}'", file=sys.stderr)
            return 1
        graph = load_graph(saved)
    document = {
        "schema_version": graph.schema_version,
        "thread_id": graph.thread_id,
        "agent": graph.agent,
        "source_path": graph.source_path,
        "nodes": [node.to_dict() for node in graph.nodes.values()],
        "edges": [edge.to_dict() for edge in graph.edges.values()],
        "cases": [record.to_dict() for record in graph.cases.values()],
    }
    text = json.dumps(document, ensure_ascii=False, indent=2)
    if args.output and args.output != "-":
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(f"Written to {args.output}")
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="msagent.exgraph.export",
        description="Build and inspect experience graphs from recorded trajectories",
    )
    parser.add_argument("command", choices=["build", "show", "export"])
    parser.add_argument("-w", "--working-dir", default=None)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("-t", "--thread", default=None, help="Thread id or unique prefix")
    parser.add_argument("--path", default=None, help="Trajectory JSONL path (skips thread lookup)")
    parser.add_argument("--outcome", choices=["success", "fail"], default=None)
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("-f", "--format", choices=["json"], default="json")
    args = parser.parse_args(argv)

    if args.command != "show" and not args.thread and not args.path:
        parser.error("--thread or --path is required")

    if args.command == "build":
        return cmd_build(args)
    if args.command == "show":
        return cmd_show(args)
    return cmd_export(args)


if __name__ == "__main__":
    sys.exit(main())
