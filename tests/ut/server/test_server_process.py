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

"""Unit tests for the agent server process lifecycle helpers."""

from __future__ import annotations

import json
import pytest
import os
from pathlib import Path

from msagent.server.config import ServerConfig
from msagent.server.process import (
    _build_server_cmd,
    _build_server_env,
    _find_free_port,
    _runtime_package_dependency,
    _write_pyproject,
    generate_langgraph_json,
)


def test_generate_langgraph_json(tmp_path: Path) -> None:
    output = generate_langgraph_json(tmp_path)
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["dependencies"] == ["."]
    assert data["graphs"]["agent"] == "msagent.server.server_graph:make_graph"


def test_write_pyproject(tmp_path: Path) -> None:
    _write_pyproject(tmp_path)
    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert "msagent-server-runtime" in pyproject
    assert "mindstudio-agent" in pyproject
    assert "requires-python = \">=3.11\"" in pyproject


def test_runtime_package_dependency_resolves() -> None:
    spec = _runtime_package_dependency()
    assert spec.startswith("mindstudio-agent")
    # Either a local path (editable checkout) or a pinned version.
    assert "@" in spec or "==" in spec


def test_build_server_cmd_shape(tmp_path: Path) -> None:
    cmd = _build_server_cmd(tmp_path / "langgraph.json", host="127.0.0.1", port=4321)
    assert cmd[0] == os.sys.executable
    assert cmd[2] == "langgraph_cli"
    assert "--host" in cmd and "127.0.0.1" in cmd
    assert "--port" in cmd and "4321" in cmd
    assert "--allow-blocking" in cmd
    assert "--no-browser" in cmd and "--no-reload" in cmd


def test_build_server_env_strips_denylist_and_injects_config() -> None:
    os.environ["PYTHONPATH"] = "/evil"
    os.environ["TAVILY_API_KEY"] = "secret"
    cfg = ServerConfig(agent="Profiler", working_dir="/tmp/x")
    env = _build_server_env(cfg)
    assert "PYTHONPATH" not in env
    assert env["TAVILY_API_KEY"] == "secret"
    assert env["MSAGENT_SERVER_AGENT"] == "Profiler"
    assert env["MSAGENT_SERVER_WORKING_DIR"] == "/tmp/x"


def test_find_free_port() -> None:
    port = _find_free_port("127.0.0.1")
    assert isinstance(port, int)
    assert 0 < port < 65536


def test_preflight_validate_mcp_config_accepts_valid(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "msprof-mcp": {"command": "msprof-mcp", "transport": "stdio", "enabled": True},
                    "remote": {"url": "http://x", "transport": "sse", "enabled": True},
                }
            }
        ),
        encoding="utf-8",
    )
    from msagent.server.process import _preflight_validate_mcp_config

    _preflight_validate_mcp_config(str(config))  # must not raise


def test_preflight_validate_mcp_config_rejects_bad_entries(tmp_path: Path) -> None:
    from msagent.server.process import _preflight_validate_mcp_config

    # missing file
    with pytest.raises(ValueError, match="not found"):
        _preflight_validate_mcp_config(str(tmp_path / "nope.json"))

    # malformed json
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match="Malformed"):
        _preflight_validate_mcp_config(str(bad_json))

    # stdio without command
    no_cmd = tmp_path / "no_cmd.json"
    no_cmd.write_text(json.dumps({"mcpServers": {"x": {"transport": "stdio", "enabled": True}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="no command"):
        _preflight_validate_mcp_config(str(no_cmd))

    # unknown transport
    bad_transport = tmp_path / "bad_transport.json"
    bad_transport.write_text(
        json.dumps({"mcpServers": {"x": {"transport": "carrier-pigeon", "enabled": True}}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown transport"):
        _preflight_validate_mcp_config(str(bad_transport))


def test_extract_startup_error_marker() -> None:
    from msagent.server.process import _extract_startup_error_marker

    output = "line1\nMSAGENT_STARTUP_ERROR: Agent 'Foo' not found\nline3"
    assert _extract_startup_error_marker(output) == "Agent 'Foo' not found"
    assert _extract_startup_error_marker("no marker here") is None
