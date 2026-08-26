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

"""Unit tests for the RemoteAgent server-message conversion layer."""

from __future__ import annotations

import pytest

from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage, ToolMessage
from langgraph.types import Interrupt

from msagent.client.remote import (
    _convert_ai_message,
    _convert_human_message,
    _convert_interrupts,
    _convert_message_data,
    _convert_tool_message,
    _convert_update_data,
)


def test_convert_ai_message_with_tool_calls() -> None:
    message = _convert_ai_message(
        {
            "type": "ai",
            "content": "plan",
            "id": "msg-1",
            "tool_calls": [
                {"name": "read_file", "args": {"path": "/a"}, "id": "call-1", "type": "tool_call"}
            ],
            "usage_metadata": {"input_tokens": 10, "output_tokens": 5},
        }
    )
    assert isinstance(message, AIMessageChunk)
    assert message.content == "plan"
    assert message.id == "msg-1"
    assert message.tool_calls[0]["name"] == "read_file"
    assert message.usage_metadata["input_tokens"] == 10


def test_convert_ai_message_with_streaming_chunks() -> None:
    message = _convert_ai_message(
        {
            "type": "ai",
            "content": "",
            "tool_call_chunks": [
                {"name": "grep", "args": '{"pattern": "x', "id": "call-2", "index": 0}
            ],
        }
    )
    assert isinstance(message, AIMessageChunk)
    assert message.tool_call_chunks[0]["args"] == '{"pattern": "x'


def test_convert_human_message() -> None:
    message = _convert_human_message({"type": "human", "content": "hello", "id": "h-1"})
    assert isinstance(message, HumanMessage)
    assert message.content == "hello"
    assert message.id == "h-1"


def test_convert_tool_message() -> None:
    message = _convert_tool_message(
        {"type": "tool", "content": "result", "tool_call_id": "call-1", "name": "run_tool", "id": "t-1"}
    )
    assert isinstance(message, ToolMessage)
    assert message.content == "result"
    assert message.tool_call_id == "call-1"
    assert message.name == "run_tool"


def test_convert_message_data_dispatch() -> None:
    assert isinstance(_convert_message_data({"type": "ai", "content": "x"}), AIMessageChunk)
    assert isinstance(_convert_message_data({"type": "human", "content": "x"}), HumanMessage)
    assert _convert_message_data({"type": "weird", "content": "x"}) is None


def test_convert_interrupts() -> None:
    interrupts = _convert_interrupts([{"value": {"approved": True}, "id": "int-1"}])
    assert len(interrupts) == 1
    assert isinstance(interrupts[0], Interrupt)
    assert interrupts[0].value == {"approved": True}
    assert interrupts[0].id == "int-1"

    # Already-Interrupt objects pass through; non-list raw values are wrapped.
    existing = Interrupt(value=1, id="int-2")
    assert _convert_interrupts([existing])[0] is existing
    assert _convert_interrupts("raw") == ["raw"]


def test_convert_update_data_converts_message_dicts() -> None:
    """Server ``updates`` payloads carry model_dump()-ed message dicts; the
    CLI rendering layer requires LangChain message objects."""
    updates = {
        "model_node": {
            "messages": [
                {"type": "ai", "content": "● 中间结论", "id": "m1"},
                {"type": "tool", "content": "result", "tool_call_id": "c1", "name": "run_tool", "id": "t1"},
            ]
        },
        "other": {"x": 1},
    }
    converted = _convert_update_data(updates)
    messages = converted["model_node"]["messages"]
    assert all(isinstance(message, BaseMessage) for message in messages)
    assert isinstance(messages[0], AIMessageChunk) and messages[0].id == "m1"
    assert isinstance(messages[1], ToolMessage) and messages[1].tool_call_id == "c1"
    assert converted["other"]["x"] == 1


def test_convert_update_data_handles_overwrite_wrapper() -> None:
    updates = {"node": {"messages": {"value": [{"type": "ai", "content": "x", "id": "m2"}]}}}
    converted = _convert_update_data(updates)
    inner = converted["node"]["messages"]["value"]
    assert isinstance(inner[0], AIMessageChunk)
    assert inner[0].id == "m2"


@pytest.mark.asyncio
async def test_aupdate_state_retries_after_conflict(monkeypatch) -> None:
    """409 Conflict -> cancel active runs -> retry once."""
    from langgraph_sdk.errors import ConflictError

    calls = []

    class _FakeGraph:
        async def aupdate_state(self, config, values, *, as_node=None):
            calls.append(("update", config))
            if len(calls) == 1:
                response = type("R", (), {"status_code": 409, "text": "busy", "headers": {}})()
                raise ConflictError("thread busy", response=response, body=None)
            return {"ok": True}

    from msagent.client.remote import RemoteAgent

    agent = RemoteAgent(url="http://127.0.0.1:1")
    agent._graph = _FakeGraph()
    cancelled = []

    async def _fake_cancel(graph, thread_id):
        cancelled.append(thread_id)

    monkeypatch.setattr("msagent.client.remote._cancel_active_runs", _fake_cancel)

    result = await agent.aupdate_state(
        {"configurable": {"thread_id": "11111111-2222-3333-4444-777777777777"}},
        {"x": 1},
    )
    assert result == {"ok": True}
    assert len(calls) == 2
    assert cancelled == ["11111111-2222-3333-4444-777777777777"]
    # caller's config dict must not be mutated
