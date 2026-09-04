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

"""Typed, langchain-free view of one recorded trajectory.

Instances are built by :mod:`msagent.trajectory_recorder.reader` from the
append-only JSONL files written by the recorder (schema version 1) and hold
plain Python data only, so downstream analysis can run in tests and CI
without an LLM.

``ToolCall`` and ``AiMessage`` are frozen but carry ``dict``/``list`` fields,
which makes them unhashable; nothing needs to hash them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ToolStatus = Literal["ok", "error", "orphan"]
TurnStatus = Literal["completed", "error", "truncated"]

# run_id of the synthetic turn holding events seen before any turn.start.
PRELUDE_RUN_ID = "__prelude__"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool span: ``tool.start`` joined with ``tool.result``/``tool.error``."""

    span_id: str
    parent_span_id: str | None
    name: str
    # Structured input from tool.start; {} when starts are not captured.
    args: dict[str, Any]
    # "orphan": start recorded but no result before EOF (interrupted session).
    status: ToolStatus
    # Flattened result text; "" for orphans and tool.error results.
    output_text: str
    error_type: str | None
    error: str | None
    duration_ms: int | None
    # seq of tool.start (or of the result when the start is missing).
    seq_start: int
    # seq of tool.result / tool.error; None for orphans.
    seq_end: int | None
    # graph.checkpoint_ns without its last segment; None for the root agent.
    subagent: str | None


@dataclass(frozen=True, slots=True)
class AiMessage:
    """One ``message.ai`` event, i.e. one LLM generation."""

    seq: int
    span_id: str
    text: str
    tool_call_names: list[str]
    usage: dict[str, Any] | None
    duration_ms: int | None
    subagent: str | None


@dataclass(frozen=True, slots=True)
class Approval:
    """One ``approval.decision`` event: the human answer to a HITL interrupt."""

    seq: int
    run_id: str | None
    interrupt_id: str | None
    # Raw interrupt payload and resume value as recorded (JSON data). Their
    # shape depends on the interrupt kind; for deepagents HITL interrupts
    # ``request["action_requests"]`` and ``decision["decisions"]`` hold one
    # entry per tool. Interpreting them is left to downstream analysis.
    request: Any
    decision: Any


@dataclass(slots=True)
class Turn:
    """One user turn: ``turn.start`` .. ``turn.end`` plus everything routed to it.

    Trailing fields default so the reader can fill them incrementally; a turn
    stays ``truncated`` until its ``turn.end`` is seen.
    """

    run_id: str
    seq_start: int
    user_message: str | None
    source: str
    ai_messages: list[AiMessage] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    # approval.decision events routed to this turn, in file order.
    approvals: list[Approval] = field(default_factory=list)
    retries: int = 0
    compressions: int = 0
    status: TurnStatus = "truncated"
    error_type: str | None = None
    duration_ms: int | None = None


@dataclass(slots=True)
class Trajectory:
    """A whole trajectory file."""

    path: Path
    thread_id: str
    agent: str
    model: str | None
    working_dir: str
    started_at: str
    turns: list[Turn] = field(default_factory=list)
    # A recorder.limit event was seen: recording stopped mid-session.
    truncated_by_limit: bool = False
    # Skill names passed to get_skill, in first-seen order, deduplicated.
    skills_consulted: list[str] = field(default_factory=list)
    # Lines that could not be read as schema-v1 events (skipped silently).
    malformed_lines: int = 0
