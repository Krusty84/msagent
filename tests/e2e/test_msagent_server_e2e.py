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

"""End-to-end tests for the serverized graph (langgraph dev + RemoteAgent)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from msagent.client.remote import RemoteAgent
from msagent.configs import ConfigRegistry
from msagent.server.config import ServerConfig
from msagent.server.process import ServerProcess


async def _prepare_workdir(tmp_path: Path) -> Path:
    """Create a .msagent config dir with external MCP servers disabled."""
    registry = ConfigRegistry(tmp_path)
    await registry.ensure_config_dir()
    mcp_path = tmp_path / ".msagent" / "config.mcp.json"
    mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    for server in mcp.get("mcpServers", {}).values():
        server["enabled"] = False
    mcp_path.write_text(json.dumps(mcp, indent=2), encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_server_lifecycle_and_remote_agent(tmp_path: Path) -> None:
    """Start the agent server, verify graph readiness and a client round-trip."""
    os.environ["OPENAI_API_KEY"] = "dummy"
    workdir = await _prepare_workdir(tmp_path)

    server = ServerProcess(
        server_config=ServerConfig(agent="Profiler", model="default", working_dir=str(workdir))
    )
    try:
        await server.start()
        assert server.running
        await server.wait_for_graph_ready("agent")

        agent = RemoteAgent(url=server.url)
        # Fresh thread: aget_state must tolerate the missing-thread 404.
        state = await agent.aget_state({"configurable": {"thread_id": "99999999-0000-0000-0000-000000000001"}})
        assert state is None

        # A run must reach the LLM layer (dummy key -> auth error surfaced from
        # the server as a RemoteException) proving the full stream pipeline
        # (HTTP/SSE, message conversion) is wired up.
        with pytest.raises(Exception) as excinfo:
            async for _ns, _mode, _data in agent.astream(
                {"messages": [{"role": "user", "content": "hi"}]},
                config={"configurable": {"thread_id": "99999999-0000-0000-0000-000000000002"}},
            ):
                pass
        message = str(excinfo.value)
        assert "api_key" in message.lower() or "OpenAI" in message or "Authentication" in message
    finally:
        server.stop()
        assert not server.running


@pytest.mark.asyncio
async def test_initializer_start_server_and_session_runtime(tmp_path: Path, monkeypatch) -> None:
    """Verify Initializer.start_server wires a RemoteAgent into a Session."""
    os.environ["OPENAI_API_KEY"] = "dummy"
    workdir = await _prepare_workdir(tmp_path)

    from msagent.cli.bootstrap.initializer import initializer
    from msagent.cli.core.context import Context
    from msagent.cli.core.session import Session

    context = await Context.create(agent="Profiler", model="default", approval_mode=None, working_dir=workdir)
    session = Session(context)
    try:
        remote_agent, server = await initializer.start_server(
            agent="Profiler",
            model="default",
            working_dir=workdir,
        )
        session.remote_agent = remote_agent
        session.agent_server = server
        assert session.runtime is remote_agent
        assert session.runtime is not None
    finally:
        await session._stop_server()
