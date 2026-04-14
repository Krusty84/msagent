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

"""CLI bootstrap for starting the LangGraph web server."""

from __future__ import annotations

from pathlib import Path

from msagent.cli.theme import console
from msagent.web.launcher import launch_langgraph_dev_server


async def handle_web_command(args) -> int:
    """Handle the `msagent web` command."""
    try:
        return await launch_langgraph_dev_server(
            host=str(args.host),
            port=int(args.port),
            ui_port=int(args.ui_port),
            start_ui=not bool(args.no_ui),
            open_browser_on_start=not bool(args.no_open),
            working_dir=Path(args.working_dir),
            agent=args.agent,
            model=args.model,
        )
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        console.print_error(f"Error starting web server: {exc}")
        console.print("")
        return 1
