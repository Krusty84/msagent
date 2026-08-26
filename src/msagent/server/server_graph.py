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

"""Server-side graph factory referenced by the generated ``langgraph.json``.

The CLI spawns a ``langgraph dev`` server whose config points at
``msagent.server.server_graph:make_graph``. This module deserializes the
``MSAGENT_SERVER_*`` environment written by ``ServerConfig.to_env()`` and
reuses the exact same ``Initializer.create_graph()`` path the CLI used to run
in-process, so the server graph is byte-for-byte the same assembly (agents,
middleware, tools, MCP client, skills, checkpointer).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from msagent.server.config import ServerConfig

logger = logging.getLogger(__name__)

STARTUP_ERROR_MARKER = "MSAGENT_STARTUP_ERROR:"

# One-per-process runtime handle set by make_graph() and consumed by the
# custom HTTP routes (offload_api) so the server owns its backend resources.
_server_runtime: tuple[Any, Any] | None = None  # (graph, backend)


def get_server_runtime() -> tuple[Any, Any]:
    """Return the (graph, backend) built by the agent server.

    Raises:
        RuntimeError: If make_graph() has not completed yet.
    """
    if _server_runtime is None:
        raise RuntimeError("Agent server runtime is not initialized")
    return _server_runtime


async def make_graph() -> Any:
    """Build the agent graph for the ``langgraph dev`` server.

    Returns:
        The compiled agent graph. The server-side ``Initializer`` singleton
        also gets its catalog caches populated here, which is what the
        ``fetch_tools`` / ``fetch_skills`` agent tools fall back to when the
        runtime ``AgentContext`` (serialized across the process boundary) does
        not carry the non-serializable tool/skill objects.
    """
    try:
        graph = await _build_graph()
    except Exception as exc:
        # Machine-readable marker for the CLI's ServerProcess, which surfaces
        # the message instead of a raw traceback tail on startup failure.
        print(f"{STARTUP_ERROR_MARKER}{exc}", file=sys.stderr, flush=True)
        logger.exception("Agent server graph build failed")
        raise

    global _server_runtime  # noqa: PLW0603
    _server_runtime = (graph, getattr(graph, "_agent_backend", None))
    return graph


async def _build_graph() -> Any:
    config = ServerConfig.from_env()
    working_dir = Path(config.working_dir).resolve()

    from msagent.cli.bootstrap.initializer import initializer

    graph, _cleanup = await initializer.create_graph(
        agent=config.agent,
        model=config.model,
        working_dir=working_dir,
    )
    logger.info(
        "Agent server graph ready: agent=%s model=%s working_dir=%s",
        config.agent,
        config.model,
        working_dir,
    )
    return graph
