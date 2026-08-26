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

"""Lifecycle management for the ``langgraph dev`` agent server subprocess.

Pattern ported from deepagents-code (MIT): generate a ``langgraph.json`` that
points at ``msagent.server.server_graph:make_graph``, spawn ``langgraph dev``
on a loopback ephemeral port, poll until the graph is served, and tear the
process group down on stop (SIGTERM -> SIGKILL, ``taskkill`` on Windows).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from msagent.server.config import ServerConfig

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_EPHEMERAL_PORT = 0

_GRAPH_REF = "msagent.server.server_graph:make_graph"

_HEALTH_POLL_INTERVAL = 0.1
_HEALTH_TIMEOUT = 60.0
_GRAPH_READY_TIMEOUT = 60.0
_SHUTDOWN_TIMEOUT = 3.0
_SIGKILL_TIMEOUT = 2.0

# Env keys stripped from the server subprocess so an untrusted project cannot
# shadow stdlib/third-party modules or alter startup behavior.
_SERVER_ENV_DENYLIST = frozenset(
    {
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "GIT_ASKPASS",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "PYTHONEXECUTABLE",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "SSH_ASKPASS",
    }
)


def _find_free_port(host: str) -> int:
    """Return a currently free TCP port on ``host``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def generate_langgraph_json(output_dir: Path) -> Path:
    """Write a ``langgraph.json`` serving the msagent agent graph.

    Also mounts the server-owned HTTP app (``/offload``, ``/catalog``) so the
    CLI reaches the server process for compression and the tool catalog.
    """
    config: dict[str, Any] = {
        "dependencies": ["."],
        "graphs": {"agent": _GRAPH_REF},
        "http": {"app": "msagent.server.offload_api:app"},
    }
    output_path = output_dir / "langgraph.json"
    output_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return output_path


def _default_package_project_root() -> Path | None:
    """Return the directory above the top-level ``msagent`` package.

    ``None`` when the package location cannot be determined (frozen/zipimport
    builds), letting the caller fall back to the installed distribution
    version instead of guessing a project root.
    """
    import msagent

    package_init = getattr(msagent, "__file__", None)
    if package_init is None:
        return None
    return Path(package_init).resolve().parent.parent


def _runtime_package_dependency() -> str:
    """Requirement spec for the server runtime pyproject.toml.

    Editable source checkouts use a local path dependency so the spawned
    server sees the working tree; installed wheels fall back to the installed
    distribution version.
    """
    from importlib.metadata import PackageNotFoundError, version

    root = _default_package_project_root()
    if root is not None and (root / "pyproject.toml").is_file():
        return f"mindstudio-agent @ {root.as_uri()}"
    try:
        return f"mindstudio-agent=={version('mindstudio-agent')}"
    except PackageNotFoundError:
        return "mindstudio-agent"


def _write_pyproject(work_dir: Path) -> None:
    """Write a minimal pyproject.toml so ``langgraph dev`` can install ``.``."""
    content = f"""[project]
name = "msagent-server-runtime"
version = "0.0.1"
requires-python = ">=3.11"
dependencies = [
    "{_runtime_package_dependency()}",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""
    (work_dir / "pyproject.toml").write_text(content, encoding="utf-8")


def _scaffold_workspace(work_dir: Path) -> None:
    """Create the server working directory with its langgraph.json and pyproject."""
    work_dir.mkdir(parents=True, exist_ok=True)
    _write_pyproject(work_dir)
    generate_langgraph_json(work_dir)


def _extract_startup_error_marker(output: str) -> str | None:
    """Extract the machine-readable startup error from the server log."""
    marker = "MSAGENT_STARTUP_ERROR:"
    for line in output.splitlines():
        if marker in line:
            return line.split(marker, 1)[1].strip()
    return None


def _preflight_validate_mcp_config(mcp_config_path: str | None) -> None:
    """Validate the MCP config before spawning the server.

    Catches malformed configs (bad JSON, missing command/url, unknown
    transport) in the CLI process so the user gets a clean error instead of an
    opaque server startup failure.
    """
    import json as _json

    if not mcp_config_path:
        return
    path = Path(mcp_config_path)
    if not path.exists():
        raise ValueError(f"MCP config not found: {mcp_config_path}")
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Malformed MCP config {mcp_config_path}: {exc}") from exc

    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"MCP config {mcp_config_path} must contain an 'mcpServers' object")
    for name, server in servers.items():
        if not isinstance(server, dict):
            raise ValueError(f"MCP server '{name}' must be an object")
        transport = server.get("transport", "stdio")
        if transport not in ("stdio", "sse", "http", "streamable_http", "websocket"):
            raise ValueError(
                f"MCP server '{name}' has unknown transport '{transport}' in {mcp_config_path}"
            )
        if transport == "stdio" and not server.get("command"):
            raise ValueError(f"MCP server '{name}' uses stdio but has no command in {mcp_config_path}")
        if transport != "stdio" and not server.get("url"):
            raise ValueError(f"MCP server '{name}' uses '{transport}' but has no url in {mcp_config_path}")


def _build_server_cmd(config_path: Path, *, host: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "langgraph_cli",
        "dev",
        "--host",
        host,
        "--port",
        str(port),
        "--no-browser",
        "--no-reload",
        # The msagent graph assembly (config loading, skill scanning, LLM
        # factory, create_deep_agent) performs blocking IO that langgraph dev's
        # blockbuster guard rejects on the event loop. This is an acceptable
        # trade-off for a local single-user loopback server; revisit (async
        # drivers / to_thread) only if the server is productized for remote or
        # multi-tenant deployment.
        "--allow-blocking",
        "--config",
        str(config_path),
    ]


def _build_server_env(server_config: ServerConfig) -> dict[str, str]:
    """Inherited env minus the denylist, plus the serialized ServerConfig."""
    env = {key: value for key, value in os.environ.items() if key not in _SERVER_ENV_DENYLIST}
    env.update(server_config.to_env_dict())
    return env


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    """Terminate the server process tree (SIGTERM then SIGKILL on POSIX)."""
    if process.poll() is not None:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                check=False,
                capture_output=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            pass
        return

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        process.terminate()

    deadline = time.monotonic() + _SHUTDOWN_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.05)
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class ServerProcess:
    """Manages a ``langgraph dev`` subprocess serving the msagent graph."""

    def __init__(
        self,
        *,
        server_config: ServerConfig,
        host: str = _DEFAULT_HOST,
        port: int = _EPHEMERAL_PORT,
        config_dir: Path | None = None,
        owns_config_dir: bool = True,
    ) -> None:
        self._server_config = server_config
        self.host = host
        self.port = port if port != _EPHEMERAL_PORT else _find_free_port(host)
        self._config_dir = config_dir
        self._owns_config_dir = owns_config_dir
        self._process: subprocess.Popen[Any] | None = None
        self._started = False
        self._log_path: Path | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    async def start(self) -> None:
        """Scaffold, spawn the server, and wait until it is healthy."""
        # Fail fast on a malformed MCP config before spawning any subprocess.
        _preflight_validate_mcp_config(self._server_config.mcp_config_path)

        owns_config_dir = self._config_dir is None
        config_dir = self._config_dir or Path(tempfile.mkdtemp(prefix="msagent_server_"))
        _scaffold_workspace(config_dir)

        env = _build_server_env(self._server_config)
        cmd = _build_server_cmd(config_dir / "langgraph.json", host=self.host, port=self.port)
        logger.info("Starting agent server: %s", " ".join(cmd))

        # Log to a file instead of a pipe so the subprocess can never block on
        # an unconsumed stdout buffer; the tail is surfaced on startup errors.
        log_path = config_dir / "server.log"
        log_file = open(log_path, "w", encoding="utf-8")
        try:
            self._process = subprocess.Popen(
                cmd,
                cwd=str(config_dir),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
        except Exception:
            log_file.close()
            raise
        self._config_dir = config_dir
        self._owns_config_dir = owns_config_dir
        self._started = True
        self._log_path = log_path

        try:
            await self.wait_until_healthy()
        except BaseException:
            # Includes asyncio.CancelledError: never leak the subprocess.
            self.stop()
            raise

    async def wait_until_healthy(self) -> None:
        """Poll the server /info endpoint until it responds."""
        deadline = time.monotonic() + _HEALTH_TIMEOUT
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if not self.running:
                    raise RuntimeError(self._early_exit_message())
                try:
                    response = await client.get(f"{self.url}/info")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(_HEALTH_POLL_INTERVAL)
        raise RuntimeError(self._early_exit_message())

    async def wait_for_graph_ready(
        self,
        graph_name: str = "agent",
        *,
        timeout: float = _GRAPH_READY_TIMEOUT,
    ) -> None:
        """Resolve the served graph so lazy startup failures surface early.

        Hits ``/assistants/{graph_name}/graph``, which triggers the server's
        lazy graph build and returns the graph schema once ``make_graph()``
        has succeeded.
        """
        from urllib.parse import quote

        if self._process is None:
            raise RuntimeError("Server process is not running")

        graph_url = f"{self.url}/assistants/{quote(graph_name, safe='')}/graph"
        deadline = time.monotonic() + timeout

        async with httpx.AsyncClient(timeout=10.0) as client:
            while time.monotonic() < deadline:
                if not self.running:
                    raise RuntimeError(self._early_exit_message())
                try:
                    response = await client.get(graph_url)
                    if response.status_code == 200:
                        return
                    if response.status_code >= 400 and response.status_code < 500:
                        raise RuntimeError(
                            f"Agent server graph '{graph_name}' failed to initialize "
                            f"(HTTP {response.status_code}): {response.text[:500]}"
                        )
                except httpx.HTTPError:
                    if not self.running:
                        raise RuntimeError(self._early_exit_message()) from None
                await asyncio.sleep(_HEALTH_POLL_INTERVAL)
        raise RuntimeError(
            f"Agent server graph '{graph_name}' did not initialize within {timeout}s\n"
            f"{self._early_exit_message()}"
        )

    def stop(self) -> None:
        """Terminate the server subprocess and clean up the temp workspace."""
        if self._process is not None:
            _terminate_process_group(self._process)
            self._process = None
        if self._config_dir is not None and self._owns_config_dir:
            import shutil

            shutil.rmtree(self._config_dir, ignore_errors=True)
            self._config_dir = None
        self._started = False

    def _early_exit_message(self) -> str:
        tail = ""
        log_path = getattr(self, "_log_path", None)
        if log_path is not None and Path(log_path).exists():
            try:
                tail = Path(log_path).read_text(encoding="utf-8", errors="replace")[-3000:]
            except OSError:
                pass
        marker_summary = _extract_startup_error_marker(tail)
        if marker_summary:
            return f"Agent server startup failed: {marker_summary}"
        return f"Agent server exited before becoming healthy:\n{tail}"
