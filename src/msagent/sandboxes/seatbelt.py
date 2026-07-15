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

"""macOS Seatbelt sandbox wrapper.

Wraps tool execution with macOS Seatbelt (sandbox-exec) for filesystem,
network, and process isolation on Darwin platforms.
"""

from __future__ import annotations

import logging
import platform
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from msagent.configs.sandbox import SandboxConfig

logger = logging.getLogger(__name__)

# macOS sandbox-exec binary path
SANDBOX_EXEC_PATH = "/usr/bin/sandbox-exec"

# Default Seatbelt profile template with parameterized rules.
# Variables are substituted at runtime:
#   ${READONLY_PATHS}    – paths allowed for reading
#   ${WRITABLE_PATHS}    – paths allowed for writing
#   ${HIDDEN_PATHS}      – paths/patterns to hide
#   ${NETWORK_ALLOW}     – "(allow network*)" or empty
#   ${ALLOWED_REMOTE_HOSTS} – host allow rules or empty
_SEATBELT_PROFILE_TEMPLATE = """(version 1)
(allow default)
(deny file-write*)

; --- Read-only paths ---
${READONLY_PATHS}

; --- Writable paths ---
${WRITABLE_PATHS}

; --- Hidden paths ---
${HIDDEN_PATHS}

; --- Network access ---
${NETWORK_ALLOW}
${ALLOWED_REMOTE_HOSTS}

; --- Process execution ---
(allow process-exec)
(allow process-fork)
(allow sysctl-read)
(allow signal)

; --- Basic operations ---
(allow file-read*)
(allow file-ioctl)
(allow file-mmap)
"""


class SeatbeltWrapper:
    """Wrap tool execution with macOS Seatbelt (sandbox-exec) sandbox.

    This wrapper generates a temporary Seatbelt profile from the
    SandboxConfig and prepends ``sandbox-exec -f <profile>`` to the
    command so that the child process runs inside a macOS sandbox.

    Usage::

        wrapper = SeatbeltWrapper(config)
        wrapped_cmd = wrapper.wrap_command(["python", "script.py"], working_dir="/tmp")
        # -> ["sandbox-exec", "-f", "/tmp/seatbelt_xxx.sb", "--", "python", "script.py"]
    """

    def __init__(self, config: SandboxConfig) -> None:
        """Initialize the Seatbelt wrapper.

        Args:
            config: Validated SandboxConfig with type=SEATBELT.
        """
        self.config = config

    @staticmethod
    def is_available() -> bool:
        """Check whether Seatbelt (sandbox-exec) is available on this system.

        Returns:
            True if running on macOS and sandbox-exec is found.
        """
        if platform.system() != "Darwin":
            return False
        return Path(SANDBOX_EXEC_PATH).exists()

    @staticmethod
    def get_seatbelt_path() -> str:
        """Resolve the sandbox-exec binary path.

        Returns:
            Absolute path to sandbox-exec, or "sandbox-exec" if not found
            at the canonical location (falls back to PATH lookup).
        """
        if Path(SANDBOX_EXEC_PATH).exists():
            return SANDBOX_EXEC_PATH
        found = shutil.which("sandbox-exec")
        return found or "sandbox-exec"

    def _build_profile(self, working_dir: str) -> str:
        """Build a Seatbelt SBPL profile string from config.

        Args:
            working_dir: The working directory to grant write access to.

        Returns:
            A complete Seatbelt profile string (SBPL format).
        """
        fs = self.config.filesystem

        # Read-only paths
        readonly_entries: list[str] = []
        for path in fs.read:
            if path == ".":
                path = working_dir
            readonly_entries.append(f'(allow file-read* (subpath "{path}"))')
        readonly_block = "\n".join(readonly_entries)

        # Writable paths
        writable_entries: list[str] = []
        for path in fs.write:
            if path == ".":
                path = working_dir
            writable_entries.append(f'(allow file-read* file-write* (subpath "{path}"))')
        # Always grant write access to working_dir and temp
        writable_entries.append(f'(allow file-read* file-write* (subpath "{working_dir}"))')
        writable_block = "\n".join(writable_entries)

        # Hidden paths (deny read)
        hidden_entries: list[str] = []
        for pattern in fs.hidden:
            hidden_entries.append(f'(deny file-read* (subpath "{pattern}"))')
        hidden_block = "\n".join(hidden_entries)

        # Network
        network = self.config.network
        remote_hosts = network.remote if network else []
        if remote_hosts and "*" not in remote_hosts:
            network_allow = ""
            host_rules: list[str] = []
            for host in remote_hosts:
                host_rules.append(f'(allow network-outbound (remote host "{host}"))')
            allowed_remote_block = "\n".join(host_rules)
        elif remote_hosts:
            # Wildcard: allow all outbound
            network_allow = "(allow network*)"
            allowed_remote_block = ""
        else:
            # No remote hosts configured: deny all outbound network
            network_allow = "(deny network*)"
            allowed_remote_block = ""

        profile = _SEATBELT_PROFILE_TEMPLATE.replace("${READONLY_PATHS}", readonly_block)
        profile = profile.replace("${WRITABLE_PATHS}", writable_block)
        profile = profile.replace("${HIDDEN_PATHS}", hidden_block)
        profile = profile.replace("${NETWORK_ALLOW}", network_allow)
        profile = profile.replace("${ALLOWED_REMOTE_HOSTS}", allowed_remote_block)

        return profile

    def wrap_command(self, command: list[str], working_dir: str) -> list[str]:
        """Wrap a command with sandbox-exec.

        Generates a temporary Seatbelt profile file and prepends
        ``sandbox-exec -f <profile>`` to the command.

        Args:
            command: The command and arguments to wrap.
            working_dir: The working directory for the command.

        Returns:
            The wrapped command list.
        """
        import tempfile

        profile_content = self._build_profile(working_dir)

        # Write profile to a temporary file that sandbox-exec can read.
        # The file must exist for the lifetime of the wrapped process;
        # we use delete=False and rely on the caller (or OS) to clean up.
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix="msagent_seatbelt_",
            suffix=".sb",
            delete=False,
        ) as f:
            f.write(profile_content)
            profile_path = f.name

        logger.debug("Seatbelt profile written to %s", profile_path)
        logger.debug("Profile content:\n%s", profile_content)

        seatbelt_path = self.get_seatbelt_path()
        wrapped = [seatbelt_path, "-f", profile_path, "--", *command]
        return wrapped
