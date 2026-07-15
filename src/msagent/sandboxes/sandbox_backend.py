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

"""Sandbox backend wrapper applying OS-level isolation to command execution.

Wraps a deepagents backend (e.g. ``LocalShellBackend``) and intercepts
``execute`` / ``aexecute`` calls to prepend OS sandbox tooling
(``sandbox-exec`` on macOS, ``bwrap`` on Linux) before the command
reaches the system shell.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from deepagents.backends.protocol import ExecuteResponse

from msagent.sandboxes.bubblewrap import BubblewrapWrapper
from msagent.sandboxes.seatbelt import SeatbeltWrapper

if TYPE_CHECKING:
    from msagent.configs.sandbox import SandboxConfig

logger = logging.getLogger(__name__)

# Environment variable names that should NEVER be inherited into a sandboxed
# process (API keys, tokens, credentials, etc.).
_SENSITIVE_ENV_VAR_PATTERNS: tuple[str, ...] = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "PRIVATE_KEY",
    "ACCESS_KEY",
)

# Environment variables that are safe to pass through to sandboxed processes.
_SAFE_ENV_VARS: frozenset[str] = frozenset(
    {
        "HOME",
        "USER",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TERM",
        "SHELL",
        "PWD",
        "OLDPWD",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "NODE_PATH",
        "JAVA_HOME",
        "GOPATH",
        "CARGO_HOME",
        "RUSTUP_HOME",
    }
)


def _filter_sandbox_env(original_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build a filtered environment dict for sandboxed execution.

    Only passes through safe environment variables and strips any that
    match sensitive patterns (API keys, tokens, credentials, etc.).

    Args:
        original_env: The original environment dict. If ``None``, uses
            ``os.environ``.

    Returns:
        A filtered dict safe for sandboxed subprocess execution.
    """
    source = original_env if original_env is not None else os.environ
    filtered: dict[str, str] = {}

    for key, value in source.items():
        upper_key = key.upper()

        # Explicitly allow known-safe variables.
        if upper_key in _SAFE_ENV_VARS:
            filtered[key] = value
            continue

        # Block anything matching a sensitive pattern.
        if any(pattern in upper_key for pattern in _SENSITIVE_ENV_VAR_PATTERNS):
            logger.debug("Stripping sensitive env var from sandbox: %s", key)
            continue

        # Pass through everything else (application-specific vars).
        filtered[key] = value

    return filtered


class SandboxedShellBackend:
    """Wraps a backend with OS-level sandbox enforcement.

    Delegates all backend operations (read, write, glob, grep, ls, edit,
    upload, download) directly to the *inner* backend.  Only ``execute``
    and ``aexecute`` are intercepted so that the shell command is wrapped
    with the appropriate OS sandbox tool before it reaches the system.

    Usage::

        inner = LocalShellBackend(root_dir="/workspace")
        sandboxed = SandboxedShellBackend(
            inner=inner,
            sandbox_config=config,
            working_dir="/workspace",
        )
        result = sandboxed.execute("python script.py")
        # Command actually run: bwrap ... -- python script.py
    """

    def __init__(
        self,
        inner: Any,
        sandbox_config: SandboxConfig,
        working_dir: str,
    ) -> None:
        """Initialize the sandbox backend wrapper.

        Args:
            inner: The inner backend to delegate to (e.g. ``LocalShellBackend``).
            sandbox_config: Validated sandbox configuration.
            working_dir: The working directory for sandbox execution.
        """
        self._inner = inner
        self._sandbox_config = sandbox_config
        self._working_dir = working_dir

        # Create the appropriate OS-level wrapper.
        from msagent.configs.sandbox import SandboxType

        if sandbox_config.type == SandboxType.SEATBELT:
            self._os_wrapper = SeatbeltWrapper(sandbox_config)
        elif sandbox_config.type == SandboxType.BUBBLEWRAP:
            self._os_wrapper = BubblewrapWrapper(sandbox_config)
        else:
            self._os_wrapper = None
            logger.warning(
                "Unknown sandbox type '%s' – execute calls will NOT be sandboxed.",
                sandbox_config.type,
            )

        # Override the inner backend's env to use filtered environment.
        self._apply_filtered_env()

    def _apply_filtered_env(self) -> None:
        """Replace the inner backend's environment with a filtered version."""
        if hasattr(self._inner, "_env"):
            filtered = _filter_sandbox_env(self._inner._env)
            self._inner._env = filtered
            logger.debug("Applied filtered sandbox environment (%d vars)", len(filtered))

    # ------------------------------------------------------------------
    # Command wrapping
    # ------------------------------------------------------------------

    def _wrap_command_str(self, command: str) -> str:
        """Wrap a shell command string with sandbox tool invocation.

        Converts the string command into a list, wraps it with the OS
        sandbox wrapper, and joins back into a shell-safe string.

        Args:
            command: The original shell command string.

        Returns:
            A sandbox-wrapped shell command string, or the original
            command unchanged if no OS wrapper is available.
        """
        if self._os_wrapper is None:
            return command

        # The OS wrappers work with argument lists.
        # Split the command string into a list for wrapping.
        # We use a simple split — the original string is already
        # executed via shell=True, so this preserves semantics.
        cmd_parts: list[str] = [command]

        try:
            wrapped_parts = self._os_wrapper.wrap_command(
                cmd_parts,
                working_dir=self._working_dir,
            )
            # Re-join into a shell command string.
            # Use shlex.join for safe quoting where available.
            import shlex

            wrapped_str = " ".join(shlex.quote(part) for part in wrapped_parts)
            logger.debug("Sandbox-wrapped command: %s", wrapped_str)
            return wrapped_str
        except Exception:
            logger.exception("Failed to wrap command with sandbox – running without isolation.")
            return command

    # ------------------------------------------------------------------
    # Intercepted execution methods
    # ------------------------------------------------------------------

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a command inside the OS sandbox.

        Args:
            command: Shell command string.
            timeout: Maximum execution time in seconds.

        Returns:
            ``ExecuteResponse`` with combined stdout/stderr and exit code.
        """
        wrapped_command = self._wrap_command_str(command)
        return self._inner.execute(wrapped_command, timeout=timeout)

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Async variant of :meth:`execute`."""
        wrapped_command = self._wrap_command_str(command)
        return await self._inner.aexecute(wrapped_command, timeout=timeout)

    # ------------------------------------------------------------------
    # Passthrough delegation for ALL other backend operations
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Delegate unknown attributes to the inner backend.

        This handles all non-execute operations transparently:
        read, write, edit, glob, grep, ls, upload_files, download_files,
        and their async variants.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)
