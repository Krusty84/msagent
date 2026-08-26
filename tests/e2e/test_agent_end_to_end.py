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

"""End-to-end agent tests driven by a fake LLM (pattern from deepagents-code).

Runs the *real* compiled agent graph (AgentFactory.create -> create_deep_agent,
real backends, real middleware) with a deterministic fake chat model, so tool
calls, filesystem operations, subagent-style multi-step loops and automatic
summarization/offload are exercised end-to-end without any API key.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from msagent.agents.factory import AgentFactory
from msagent.llms.factory import LLMFactory
from msagent.tools.factory import ToolFactory


class FixedGenericFakeChatModel(GenericFakeChatModel):
    """GenericFakeChatModel that binds tools and captures every model call."""

    captured_calls: list[tuple[list[BaseMessage], ChatResult]] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        """Tool binding must return self for the graph to call _generate."""
        del tools, tool_choice, kwargs
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        self.captured_calls.append((messages, result))
        return result


class _FakeLLMFactory(LLMFactory):
    """LLMFactory stub that hands out the same fake model to every caller."""

    def __init__(self, messages: list[AIMessage]) -> None:
        self._model = FixedGenericFakeChatModel(messages=iter(messages))

    def create(self, _config=None, **_kwargs: Any) -> Any:
        return self._model


def _agent_config(*, compression: Any = None) -> SimpleNamespace:
    """Minimal but real-shaped AgentConfig the factory accepts.

    ``tools.patterns`` mirrors the default templates (``impl:deepagents:*``) so
    the runtime catalog tools and the filesystem middleware tools are exposed;
    an empty pattern list would filter every tool out.
    """
    return SimpleNamespace(
        name="e2e",
        prompt="You are an end-to-end test agent.",
        llm=SimpleNamespace(),
        tools=SimpleNamespace(
            patterns=["impl:deepagents:*"],
            execution_timeout_seconds=60,
            output_max_tokens=2000,
        ),
        skills=None,
        retry=None,
        compression=compression,
        subagents=[],
        checkpointer=None,
    )


async def _build_agent(
    tmp_path: Path,
    messages: list[AIMessage],
    *,
    compression: Any = None,
) -> tuple[Any, Any]:
    """Compile a real agent graph whose model is the fake chat model."""
    factory = AgentFactory(llm_factory=_FakeLLMFactory(messages), tool_factory=ToolFactory())
    graph = await factory.create(
        config=_agent_config(compression=compression),
        working_dir=tmp_path,
        mcp_client=None,
        llm_config=SimpleNamespace(),
        checkpointer=InMemorySaver(),
    )
    return graph, factory.llm_factory._model


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _ai_messages(result: dict[str, Any]) -> list[Any]:
    return [m for m in result["messages"] if m.type == "ai"]


class TestAgentEndToEnd:
    """End-to-end agent behavior with a fake LLM."""

    @pytest.mark.asyncio
    async def test_basic_message_flow(self, tmp_path: Path) -> None:
        graph, _model = await _build_agent(
            tmp_path,
            [AIMessage(content="Hello! I can help with profiling analysis.")],
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="Hi")]},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
        )
        assert "messages" in result
        assert len(_ai_messages(result)) >= 1
        assert "profiling" in _ai_messages(result)[-1].content

    @pytest.mark.asyncio
    async def test_tool_call_executes_real_tool(self, tmp_path: Path) -> None:
        """The agent calls the real fetch_tools tool and then answers."""
        graph, _model = await _build_agent(
            tmp_path,
            [
                _tool_call("fetch_tools", {}, "call-1"),
                AIMessage(content="工具列表已获取。"),
            ],
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="列出可用工具")]},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
        )
        types = [m.type for m in result["messages"]]
        assert "tool" in types
        tool_messages = [m for m in result["messages"] if m.type == "tool"]
        assert len(tool_messages) >= 1
        assert any("fetch_tools" in (m.content or "") for m in tool_messages)
        assert any("工具列表已获取" in (m.content or "") for m in _ai_messages(result))

    @pytest.mark.asyncio
    async def test_filesystem_tool_reads_real_file(self, tmp_path: Path) -> None:
        test_file = tmp_path / "note.txt"
        test_file.write_text("hello profiling", encoding="utf-8")

        graph, _model = await _build_agent(
            tmp_path,
            [
                _tool_call("read_file", {"file_path": str(test_file)}, "call-1"),
                AIMessage(content="文件内容已读取。"),
            ],
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="读文件")]},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
        )
        tool_messages = [m for m in result["messages"] if m.type == "tool"]
        assert len(tool_messages) >= 1
        assert any("hello profiling" in (m.content or "") for m in tool_messages)

    @pytest.mark.asyncio
    async def test_multiple_sequential_tool_calls(self, tmp_path: Path) -> None:
        graph, _model = await _build_agent(
            tmp_path,
            [
                _tool_call("fetch_skills", {}, "call-1"),
                _tool_call("fetch_tools", {}, "call-2"),
                AIMessage(content="两次调用都完成了。"),
            ],
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="先查技能再查工具")]},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
        )
        tool_messages = [m for m in result["messages"] if m.type == "tool"]
        assert len(tool_messages) >= 2
        assert any("两次调用都完成了" in (m.content or "") for m in _ai_messages(result))

    @pytest.mark.asyncio
    async def test_summarization_offloads_history(self, tmp_path: Path) -> None:
        """Oversized context triggers automatic compaction and offload."""
        compression = SimpleNamespace(
            prompt="Summarize the conversation.\n\n{messages}",
            messages_to_keep=0,
            llm=None,
        )
        graph, model = await _build_agent(
            tmp_path,
            [AIMessage(content="summary goes here"), AIMessage(content="response")],
            compression=compression,
        )
        model.profile = {"max_input_tokens": 200_000}

        big = "x" * 60_000 * 4  # ~60k tokens per message
        input_messages = [
            HumanMessage(content=big),
            AIMessage(content=big),
            HumanMessage(content=big),
            AIMessage(content=big),
            HumanMessage(content=big),
            AIMessage(content=big),  # ~180k tokens -> compact
            HumanMessage(content="query"),
        ]
        result = await graph.ainvoke(
            {"messages": input_messages},
            {"configurable": {"thread_id": "sum-thread"}},
        )
        assert len(result["messages"]) == len(input_messages) + 1
        assert result["messages"][-1].content == "response"
        # Two model calls: one summarization, one final response.
        assert len(model.captured_calls) >= 2

        # Conversation history must be persisted through the real backend.
        # 0.7.9 SummarizationMiddleware names offload files by a random
        # session id (session_<uuid>.md), so assert the directory has content
        # rather than guessing the filename.
        history_dir = tmp_path / ".msagent" / "conversation_history"
        assert history_dir.is_dir()
        md_files = list(history_dir.glob("*.md"))
        assert md_files, "conversation history must be offloaded to disk"
        assert any(
            "Summarized at" in f.read_text(encoding="utf-8")
            or "Offloaded at" in f.read_text(encoding="utf-8")
            for f in md_files
        )

    @pytest.mark.asyncio
    async def test_backend_is_composite(self, tmp_path: Path) -> None:
        from deepagents.backends.composite import CompositeBackend

        graph, _model = await _build_agent(tmp_path, [AIMessage(content="ok")])
        backend = getattr(graph, "_agent_backend", None)
        assert isinstance(backend, CompositeBackend)
        assert hasattr(graph, "_llm_tools")
        assert len(graph._llm_tools) > 0
