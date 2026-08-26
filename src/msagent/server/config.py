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

"""Server configuration shared between the CLI client and the agent server.

The client serializes a ``ServerConfig`` into ``MSAGENT_SERVER_*`` environment
variables before spawning the ``langgraph dev`` subprocess; the server's
``make_graph()`` deserializes it with ``ServerConfig.from_env()``. Keeping a
single schema on both sides prevents configuration drift across the process
boundary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_PREFIX = "MSAGENT_SERVER_"


def _env_name(attr: str) -> str:
    return f"{ENV_PREFIX}{attr.upper()}"


@dataclass
class ServerConfig:
    """Serializable configuration for the agent server process."""

    agent: str | None = None
    model: str | None = None
    working_dir: str = field(default_factory=lambda: str(Path.cwd().resolve()))
    approval_mode: str = "active"
    recursion_limit: int | None = None
    checkpointer_path: str | None = None
    mcp_config_path: str | None = None
    audit_enabled: bool = True
    debug: bool = False

    def to_env_dict(self) -> dict[str, str]:
        """Return this config as ``MSAGENT_SERVER_*`` env-var pairs."""
        return {
            _env_name("agent"): self.agent or "",
            _env_name("model"): self.model or "",
            _env_name("working_dir"): self.working_dir,
            _env_name("approval_mode"): self.approval_mode,
            _env_name("recursion_limit"): str(self.recursion_limit) if self.recursion_limit is not None else "",
            _env_name("checkpointer_path"): self.checkpointer_path or "",
            _env_name("mcp_config_path"): self.mcp_config_path or "",
            _env_name("audit_enabled"): "1" if self.audit_enabled else "0",
            _env_name("debug"): "1" if self.debug else "0",
        }

    def to_env(self) -> None:
        """Export this config into ``os.environ`` under the server prefix."""
        os.environ.update(self.to_env_dict())

    @classmethod
    def from_env(cls) -> "ServerConfig":
        """Read a config previously written by :meth:`to_env`."""
        return cls(
            agent=_env_str(_env_name("agent")) or None,
            model=_env_str(_env_name("model")) or None,
            working_dir=_env_str(_env_name("working_dir")) or str(Path.cwd().resolve()),
            approval_mode=_env_str(_env_name("approval_mode")) or "active",
            recursion_limit=_env_int(_env_name("recursion_limit")),
            checkpointer_path=_env_str(_env_name("checkpointer_path")) or None,
            mcp_config_path=_env_str(_env_name("mcp_config_path")) or None,
            audit_enabled=_env_bool(_env_name("audit_enabled"), default=True),
            debug=_env_bool(_env_name("debug"), default=False),
        )

    @classmethod
    def from_cli_args(cls, **kwargs: Any) -> "ServerConfig":
        """Build a config from CLI-derived keyword arguments."""
        return cls(**{key: value for key, value in kwargs.items() if value is not None})


def _env_str(name: str) -> str:
    return os.environ.get(name, "").strip()


def _env_int(name: str) -> int | None:
    raw = _env_str(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _env_bool(name: str, *, default: bool) -> bool:
    raw = _env_str(name)
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
