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

"""Linux Bubblewrap sandbox wrapper.

Wraps tool execution with Linux Bubblewrap (bwrap) for filesystem,
network, and process isolation on Linux platforms.
"""

from __future__ import annotations

import logging
import platform
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from msagent.configs.sandbox import SandboxConfig

logger = logging.getLogger(__name__)

# Bubblewrap binary name
BWRAP_BIN = "bwrap"


class BubblewrapWrapper:
    """Wrap tool execution with Linux Bubblewrap (bwrap) sandbox.

    This wrapper builds a ``bwrap`` command line from the SandboxConfig,
    creating an isolated mount namespace with controlled filesystem access.

    Usage::

        wrapper = BubblewrapWrapper(config)
        wrapped_cmd = wrapper.wrap_command(["python", "script.py"], working_dir="/tmp")
        # -> ["bwrap", "--ro-bind", "/usr", "/usr", ..., "--bind", "/tmp", "/tmp", ...,
        #      "python", "script.py"]
    """

    def __init__(self, config: SandboxConfig) -> None:
        """Initialize the Bubblewrap wrapper.

        Args:
            config: Validated SandboxConfig with type=BUBBLEWRAP.
        """
        self.config = config

    @staticmethod
    def is_available() -> bool:
        """Check whether Bubblewrap (bwrap) is available on this system.

        Returns:
            True if running on Linux and bwrap is found in PATH.
        """
        if not platform.system().startswith("Linux"):
            return False
        return shutil.which(BWRAP_BIN) is not None

    @staticmethod
    def get_bwrap_path() -> str:
        """Resolve the bwrap binary path.

        Returns:
            Absolute path to bwrap, or "bwrap" as a PATH fallback.
        """
        found = shutil.which(BWRAP_BIN)
        return found or BWRAP_BIN

    def _build_bwrap_args(self, working_dir: str) -> list[str]:
        """Build the bwrap argument list from config.

        Args:
            working_dir: The working directory to grant write access to.

        Returns:
            List of bwrap arguments (excluding the command itself).
        """
        args: list[str] = [self.get_bwrap_path()]
        fs = self.config.filesystem

        # --- Read-only system mounts ---
        # Mount essential system directories as read-only.
        _SYSTEM_RO_DIRS = ["/usr", "/bin", "/lib", "/lib64", "/etc"]
        for d in _SYSTEM_RO_DIRS:
            if Path(d).exists():
                args.extend(["--ro-bind", d, d])

        # --- Read-only paths from config ---
        for path in fs.read:
            resolved = working_dir if path == "." else path
            p = Path(resolved)
            if p.exists():
                args.extend(["--ro-bind", str(p), str(p)])
            else:
                logger.debug("Read-only path does not exist, skipping: %s", resolved)

        # --- Writable paths from config ---
        for path in fs.write:
            resolved = working_dir if path == "." else path
            p = Path(resolved)
            p.mkdir(parents=True, exist_ok=True)
            args.extend(["--bind", str(p), str(p)])

        # Always bind the working directory as writable (required by tools).
        Path(working_dir).mkdir(parents=True, exist_ok=True)
        args.extend(["--bind", working_dir, working_dir])

        # --- Hidden paths ---
        # Mount an empty directory over hidden paths to make them invisible.
        for pattern in fs.hidden:
            p = Path(pattern) if pattern != "." else Path(working_dir)
            if p.exists():
                # Create a temporary empty directory to bind over the hidden path.
                empty_dir = tempfile.mkdtemp(prefix="msagent_bwrap_empty_")
                args.extend(["--bind", empty_dir, str(p)])

        # --- Temporary directories ---
        tmp_dir = tempfile.gettempdir()
        if Path(tmp_dir).exists():
            args.extend(["--bind", tmp_dir, tmp_dir])

        # --- /dev and /proc ---
        if Path("/dev").exists():
            args.extend(["--dev", "/dev"])
        if Path("/proc").exists():
            args.extend(["--proc", "/proc"])

        # --- Network isolation ---
        network = self.config.network
        remote_hosts = network.remote if network else []
        if not remote_hosts:
            # No remote hosts: unshare network namespace (block all outbound).
            args.append("--unshare-net")
        elif "*" in remote_hosts:
            # Wildcard: share network (allow all outbound) — default.
            pass
        else:
            # Specific hosts only – partial network filtering.
            # bwrap does not support domain-level filtering natively;
            # we fall back to sharing the network namespace but log a warning.
            logger.warning(
                "Bubblewrap does not support per-domain network filtering. Network is fully enabled. Allowed hosts: %s",
                remote_hosts,
            )

        # --- Security hardening ---
        args.extend(
            [
                "--die-with-parent",
                "--new-session",
                "--unshare-ipc",
                "--unshare-pid",
            ]
        )

        return args

    def wrap_command(self, command: list[str], working_dir: str) -> list[str]:
        """Wrap a command with Bubblewrap sandbox.

        Args:
            command: The command and arguments to wrap.
            working_dir: The working directory for the command.

        Returns:
            The wrapped command list ready for subprocess execution.
        """
        bwrap_args = self._build_bwrap_args(working_dir)

        logger.debug("Bubblewrap command: %s %s", " ".join(bwrap_args), " ".join(command))

        wrapped = [*bwrap_args, "--", *command]
        return wrapped
