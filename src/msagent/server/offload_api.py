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

"""Server-owned HTTP boundary for conversation offload and tool catalog.

Mounted via ``langgraph.json``'s ``http.app`` so the CLI talks to the same
process that owns the graph and its backend: offload reads thread state, runs
summarization, writes conversation history through the real backend (no
client-side backend reconstruction), and writes back the summarization event.
``/catalog`` serves the authoritative tool/skill/MCP listing so the CLI's
``/tools`` etc. match what the server actually loaded.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

_client: Any = None


def _thread_client() -> Any:
    """Return a process-wide in-process LangGraph SDK client.

    Talking to the server through its own HTTP API (rather than calling the
    graph object directly) keeps thread-state access on the checkpointer's
    owning context; direct ``graph.aget_state`` from a custom ASGI route
    reuses the sqlite connection across threads and fails.
    """
    global _client  # noqa: PLW0603
    if _client is None:
        from langgraph_sdk import get_client

        _client = get_client(url=None, api_key=None)
    return _client


async def _offload(request: Request) -> JSONResponse:
    """Execute server-side conversation offload for a thread."""
    body = await request.json()
    thread_id = str(body.get("thread_id") or "")
    keep_messages = int(body.get("keep_messages") or 0)
    summary_prompt = body.get("summary_prompt")

    if not thread_id:
        return JSONResponse({"status": "error", "reason": "thread_id is required"}, status_code=400)

    from msagent.client.remote import _convert_message_list
    from msagent.server.server_graph import get_server_runtime

    graph, backend = get_server_runtime()
    client = _thread_client()
    from langgraph_sdk.errors import NotFoundError

    try:
        current = await client.threads.get_state(thread_id)
    except NotFoundError:
        return JSONResponse({"status": "noop", "reason": "no conversation history"})
    except Exception as exc:
        logger.warning("offload get_state failed for thread %s", thread_id, exc_info=True)
        return JSONResponse({"status": "error", "reason": str(exc)})

    values = current.get("values") if isinstance(current, dict) else {}
    if not isinstance(values, dict):
        return JSONResponse({"status": "noop", "reason": "no conversation history"})
    messages = _convert_message_list(values.get("messages") or [])
    if not messages:
        return JSONResponse({"status": "noop", "reason": "no conversation history"})

    prior_event = values.get("_summarization_event")

    from msagent.cli.bootstrap.initializer import initializer
    from msagent.configs import CompressionConfig
    from msagent.server.config import ServerConfig
    from msagent.utils.offload import perform_conversation_offload

    cfg = ServerConfig.from_env()
    working_dir = Path(cfg.working_dir)
    try:
        agents = await initializer.load_agents_config(working_dir)
        agent_config = agents.get_agent_config(cfg.agent)
        if agent_config is None:
            return JSONResponse({"status": "error", "reason": f"agent '{cfg.agent}' not found"}, status_code=400)
        compression_config = getattr(agent_config, "compression", None) or CompressionConfig()
        compression_llm_config = getattr(compression_config, "llm", None) or getattr(agent_config, "llm", None)
        model = initializer.llm_factory.create(compression_llm_config)
    except Exception as exc:
        logger.warning("offload model setup failed", exc_info=True)
        return JSONResponse({"status": "error", "reason": str(exc)})

    if backend is None:
        return JSONResponse({"status": "error", "reason": "server backend unavailable"}, status_code=500)

    try:
        result = await perform_conversation_offload(
            messages=messages,
            prior_event=prior_event,
            thread_id=thread_id,
            model=model,
            backend=backend,
            keep_messages=keep_messages,
            summary_prompt=summary_prompt,
        )
    except Exception as exc:
        logger.warning("offload failed for thread %s", thread_id, exc_info=True)
        return JSONResponse({"status": "error", "reason": str(exc)})

    if result is None:
        return JSONResponse({"status": "noop", "reason": "already within retention window"})

    try:
        await client.threads.update_state(thread_id, {"_summarization_event": result.new_event})
    except Exception as exc:
        logger.warning("offload state write-back failed for thread %s", thread_id, exc_info=True)
        return JSONResponse({"status": "error", "reason": str(exc)})

    return JSONResponse(
        {
            "status": "ok",
            "messages_offloaded": result.messages_offloaded,
            "messages_kept": result.messages_kept,
            "tokens_before": result.tokens_before,
            "tokens_after": result.tokens_after,
            "pct_decrease": result.pct_decrease,
            "file_path": result.new_event.get("file_path"),
            "offload_warning": result.offload_warning,
        }
    )


async def _catalog(request: Request) -> JSONResponse:  # noqa: ARG001
    """Return the authoritative tool/skill/MCP listing from the server."""
    from msagent.cli.bootstrap.initializer import initializer

    tools = []
    for tool in initializer.cached_llm_tools:
        tools.append(
            {
                "name": getattr(tool, "name", ""),
                "description": getattr(tool, "description", ""),
            }
        )
    skills = [
        {
            "name": getattr(skill, "name", ""),
            "category": getattr(skill, "category", ""),
            "description": getattr(skill, "description", ""),
        }
        for skill in initializer.cached_agent_skills
    ]
    return JSONResponse(
        {
            "tools": tools,
            "skills": skills,
            "mcp_servers": list(initializer.cached_mcp_server_names),
        }
    )


app = Starlette(
    routes=[
        Route("/offload", _offload, methods=["POST"]),
        Route("/catalog", _catalog, methods=["GET"]),
    ]
)
