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

"""Server-side checkpointer factory referenced by the generated langgraph.json.

The database path is delivered through the ``MSAGENT_SERVER_CHECKPOINTER_PATH``
environment variable (set by the CLI before server startup), so the same
``.msagent/config.checkpoints.db`` file is shared between the agent server
(writer) and the CLI ``/threads`` reader (see ``msagent.cli.handlers.threads``).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

CHECKPOINTER_PATH_ENV = "MSAGENT_SERVER_CHECKPOINTER_PATH"


@asynccontextmanager
async def create_checkpointer() -> AsyncIterator[object]:
    """Yield an :class:`AsyncSqliteSaver` bound to the shared DB file."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    db_path = os.environ.get(CHECKPOINTER_PATH_ENV, "").strip()
    if not db_path:
        raise RuntimeError(
            f"{CHECKPOINTER_PATH_ENV} not set. The CLI must set this environment "
            "variable before starting the agent server."
        )
    # WAL allows the CLI to read the same file concurrently from another process.
    async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
        yield saver
