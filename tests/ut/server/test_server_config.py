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

"""Unit tests for the agent server config schema."""

from __future__ import annotations

import os
from pathlib import Path

from msagent.server.config import ServerConfig


def test_env_round_trip() -> None:
    cfg = ServerConfig(
        agent="Profiler",
        model="default",
        working_dir="/tmp/project",
        approval_mode="passive",
        recursion_limit=42,
        checkpointer_path="/tmp/project/.msagent/config.checkpoints.db",
        mcp_config_path="/tmp/project/.msagent/config.mcp.json",
        audit_enabled=False,
        debug=True,
    )

    env = cfg.to_env_dict()
    assert env["MSAGENT_SERVER_AGENT"] == "Profiler"
    assert env["MSAGENT_SERVER_MODEL"] == "default"
    assert env["MSAGENT_SERVER_WORKING_DIR"] == "/tmp/project"
    assert env["MSAGENT_SERVER_APPROVAL_MODE"] == "passive"
    assert env["MSAGENT_SERVER_RECURSION_LIMIT"] == "42"
    assert env["MSAGENT_SERVER_CHECKPOINTER_PATH"] == "/tmp/project/.msagent/config.checkpoints.db"
    assert env["MSAGENT_SERVER_MCP_CONFIG_PATH"] == "/tmp/project/.msagent/config.mcp.json"
    assert env["MSAGENT_SERVER_AUDIT_ENABLED"] == "0"
    assert env["MSAGENT_SERVER_DEBUG"] == "1"

    saved: dict[str, str | None] = {}
    for key, value in env.items():
        saved[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        restored = ServerConfig.from_env()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert restored.agent == "Profiler"
    assert restored.model == "default"
    assert restored.working_dir == "/tmp/project"
    assert restored.approval_mode == "passive"
    assert restored.recursion_limit == 42
    assert restored.checkpointer_path == "/tmp/project/.msagent/config.checkpoints.db"
    assert restored.mcp_config_path == "/tmp/project/.msagent/config.mcp.json"
    assert restored.audit_enabled is False
    assert restored.debug is True


def test_from_env_defaults() -> None:
    saved = {key: os.environ.get(key) for key in os.environ if key.startswith("MSAGENT_SERVER_")}
    for key in saved:
        os.environ.pop(key, None)
    try:
        cfg = ServerConfig.from_env()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert cfg.agent is None
    assert cfg.model is None
    assert cfg.approval_mode == "active"
    assert cfg.recursion_limit is None
    assert cfg.audit_enabled is True
    assert cfg.debug is False
    assert Path(cfg.working_dir).is_absolute()


def test_to_env_updates_os_environ() -> None:
    cfg = ServerConfig(agent="Minos", working_dir="/tmp/x")
    cfg.to_env()
    try:
        assert os.environ["MSAGENT_SERVER_AGENT"] == "Minos"
    finally:
        for key in cfg.to_env_dict():
            os.environ.pop(key, None)
