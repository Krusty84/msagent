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

"""Deterministic L0 ingest: Trajectory model → experience graph."""

from __future__ import annotations

from pathlib import Path

from msagent.exgraph.schema import (
    SCHEMA_VERSION,
    CaseRecord,
    Edge,
    ExperienceGraph,
    Node,
    Outcome,
    OutcomeOverride,
    case_id,
    edge_id,
    step_id,
    subagent_id,
    task_anchor_id,
    thread_id,
)
from msagent.trajectory_recorder.model import PRELUDE_RUN_ID, Trajectory, Turn


def _final_text(turn: Turn) -> str | None:
    """Last root-agent assistant text; fall back to any last message."""
    root = [message for message in turn.ai_messages if not message.subagent]
    chosen = root[-1] if root else (turn.ai_messages[-1] if turn.ai_messages else None)
    if chosen is None:
        return None
    text = chosen.text.strip()
    return text or None


def _tool_errors(turn: Turn) -> list[dict[str, str | None]]:
    errors: list[dict[str, str | None]] = []
    for call in turn.tool_calls:
        if call.status != "error":
            continue
        errors.append(
            {
                "tool": call.name,
                "type": call.error_type,
                "error": call.error,
            },
        )
    return errors


def _edited_approval(turn: Turn) -> bool:
    """True when a HITL resume looks like edited tool arguments."""
    for approval in turn.approvals:
        decision = approval.decision
        if isinstance(decision, dict):
            if decision.get("edited") or decision.get("action") == "edit":
                return True
            decisions = decision.get("decisions")
            if isinstance(decisions, list):
                for item in decisions:
                    if isinstance(item, dict) and (
                        item.get("edited") or item.get("type") == "edit"
                    ):
                        return True
    return False


def label_outcome(
    turn: Turn,
    *,
    override: OutcomeOverride | None = None,
    policy: str = "v1",
) -> Outcome:
    """P0 outcome policy.

    * ``warning`` — turn ended in error or any tool span failed.
    * ``golden`` — only with ``--outcome success`` on a non-warning turn.
    * ``unknown`` — completed (or truncated) with no hard error.

    ``--outcome fail`` forces remaining non-warning turns to ``warning``.
    ``policy`` is recorded on the case; it does not change the rules here.
    """
    del policy
    hard_fail = turn.status == "error" or bool(_tool_errors(turn))
    if hard_fail:
        return "warning"
    if override == "fail":
        return "warning"
    if override == "success" and turn.status == "completed":
        return "golden"
    return "unknown"


def _link(graph: ExperienceGraph, edge_type, src: str, dst: str, **attrs) -> None:
    graph.add_edge(
        Edge(id=edge_id(edge_type, src, dst), type=edge_type, src=src, dst=dst, attrs=attrs),
    )


def _add_steps(graph: ExperienceGraph, turn: Turn, cid: str) -> list[str]:
    """Add LLM and tool steps; return tool names in file order."""
    tool_path: list[str] = []
    seen_spans: set[str] = set()

    for message in turn.ai_messages:
        sid = step_id(message.span_id)
        graph.add_node(
            Node(
                id=sid,
                type="Step",
                attrs={
                    "kind": "llm",
                    "span_id": message.span_id,
                    "seq": message.seq,
                    "subagent": message.subagent,
                    "duration_ms": message.duration_ms,
                    "tool_call_names": list(message.tool_call_names),
                },
            ),
        )
        _link(graph, "HAS_STEP", cid, sid, kind="llm")
        if message.subagent:
            ns = subagent_id(message.subagent)
            graph.add_node(
                Node(id=ns, type="SubagentRun", attrs={"namespace": message.subagent}),
            )
            _link(graph, "IN_SUBAGENT", sid, ns)
            _link(graph, "DELEGATES", cid, ns)

    for call in turn.tool_calls:
        tool_path.append(call.name)
        sid = step_id(call.span_id)
        seen_spans.add(call.span_id)
        graph.add_node(
            Node(
                id=sid,
                type="Step",
                attrs={
                    "kind": "tool",
                    "span_id": call.span_id,
                    "name": call.name,
                    "status": call.status,
                    "subagent": call.subagent,
                    "duration_ms": call.duration_ms,
                    "seq_start": call.seq_start,
                    "seq_end": call.seq_end,
                    "error_type": call.error_type,
                },
            ),
        )
        _link(graph, "HAS_STEP", cid, sid, kind="tool")
        if call.parent_span_id:
            parent = step_id(call.parent_span_id)
            if parent in graph.nodes:
                _link(graph, "PARENT_OF", parent, sid)
        if call.subagent:
            ns = subagent_id(call.subagent)
            graph.add_node(
                Node(id=ns, type="SubagentRun", attrs={"namespace": call.subagent}),
            )
            _link(graph, "IN_SUBAGENT", sid, ns)
            _link(graph, "DELEGATES", cid, ns)
        if call.name == "task" and call.status == "ok":
            # Delegation tool on the parent: mark the case as having a subagent
            # even when checkpoint_ns was not stamped on the task span itself.
            pass

    return tool_path


def build_graph(
    trajectory: Trajectory,
    *,
    outcome: OutcomeOverride | None = None,
    outcome_policy: str = "v1",
) -> ExperienceGraph:
    """Build the P0 experience graph from a typed trajectory."""
    graph = ExperienceGraph(
        schema_version=SCHEMA_VERSION,
        thread_id=trajectory.thread_id,
        agent=trajectory.agent,
        source_path=str(trajectory.path),
    )
    tid = thread_id(trajectory.thread_id)
    aid = task_anchor_id(trajectory.thread_id)
    graph.add_node(
        Node(
            id=tid,
            type="Thread",
            attrs={
                "thread_id": trajectory.thread_id,
                "agent": trajectory.agent,
                "model": trajectory.model,
                "working_dir": trajectory.working_dir,
                "started_at": trajectory.started_at,
            },
        ),
    )
    first_intent = next(
        (turn.user_message for turn in trajectory.turns if turn.user_message),
        None,
    )
    graph.add_node(
        Node(
            id=aid,
            type="TaskAnchor",
            attrs={
                "thread_id": trajectory.thread_id,
                "grain": "thread",
                "intent": first_intent,
            },
        ),
    )
    _link(graph, "HAS_TASK", tid, aid)

    previous_case: str | None = None
    for turn in trajectory.turns:
        if turn.run_id == PRELUDE_RUN_ID:
            continue
        cid = case_id(turn.run_id)
        label = label_outcome(turn, override=outcome, policy=outcome_policy)
        tokens_in = 0
        tokens_out = 0
        for message in turn.ai_messages:
            usage = message.usage or {}
            tokens_in += int(usage.get("input_tokens") or 0)
            tokens_out += int(usage.get("output_tokens") or 0)
        tool_path = _add_steps(graph, turn, cid)
        graph.add_node(
            Node(
                id=cid,
                type="Case",
                attrs={
                    "run_id": turn.run_id,
                    "source": turn.source,
                    "turn_status": turn.status,
                    "outcome": label,
                    "seq_start": turn.seq_start,
                    "duration_ms": turn.duration_ms,
                },
            ),
        )
        _link(graph, "CONTAINS", aid, cid)
        if previous_case is not None:
            _link(graph, "NEXT_CASE", previous_case, cid)
        previous_case = cid
        evidence = [turn.seq_start]
        evidence.extend(message.seq for message in turn.ai_messages)
        evidence.extend(call.seq_start for call in turn.tool_calls)
        graph.add_case(
            CaseRecord(
                id=cid,
                thread_id=trajectory.thread_id,
                agent=trajectory.agent,
                run_id=turn.run_id,
                x=turn.user_message,
                y=_final_text(turn),
                r=label,
                sigma={
                    "tool_path": tool_path,
                    "errors": _tool_errors(turn),
                    "retries": turn.retries,
                    "approvals": len(turn.approvals),
                    "approval_edited": _edited_approval(turn),
                    "compressions": turn.compressions,
                    "duration_ms": turn.duration_ms,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "subagents": sorted(
                        {
                            call.subagent
                            for call in turn.tool_calls
                            if call.subagent
                        }
                        | {
                            message.subagent
                            for message in turn.ai_messages
                            if message.subagent
                        },
                    ),
                    "skills_consulted": [
                        str(call.args.get("name"))
                        for call in turn.tool_calls
                        if call.name == "get_skill" and call.args.get("name")
                    ],
                    "turn_status": turn.status,
                    "error_type": turn.error_type,
                },
                evidence=sorted(set(evidence)),
                outcome_policy=outcome_policy,
            ),
        )
    return graph


def build_from_path(
    path: Path,
    *,
    outcome: OutcomeOverride | None = None,
    outcome_policy: str = "v1",
) -> ExperienceGraph:
    """Load a JSONL trajectory and build its P0 graph."""
    from msagent.trajectory_recorder.reader import load_trajectory

    return build_graph(
        load_trajectory(Path(path)),
        outcome=outcome,
        outcome_policy=outcome_policy,
    )
