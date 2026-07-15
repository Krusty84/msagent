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

"""Sandbox backends with OS-specific implementations.

Provides OS-level sandbox wrappers (Seatbelt for macOS, Bubblewrap for Linux)
along with availability checks and a factory function for creating wrappers
from SandboxConfig instances.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import SandboxBackendProtocol

from msagent.sandboxes.bubblewrap import BubblewrapWrapper
from msagent.sandboxes.sandbox_backend import SandboxedShellBackend, _filter_sandbox_env
from msagent.sandboxes.seatbelt import SeatbeltWrapper

if TYPE_CHECKING:
    from msagent.configs.sandbox import SandboxConfig, SandboxType

logger = logging.getLogger(__name__)

# Re-export deepagents backend types
BackendProtocol = SandboxBackendProtocol
SandboxBackend = SandboxBackendProtocol

# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def create_sandbox_wrapper(config: SandboxConfig) -> SeatbeltWrapper | BubblewrapWrapper | None:
    """Create an OS-level sandbox wrapper from a SandboxConfig.

    Args:
        config: A validated SandboxConfig instance.

    Returns:
        - ``SeatbeltWrapper`` if ``config.type == SEATBELT``.
        - ``BubblewrapWrapper`` if ``config.type == BUBBLEWRAP``.
        - ``None`` if the sandbox type is not recognized.

    Raises:
        RuntimeError: If the requested sandbox is not available on the current
            platform (call :func:`check_sandbox_available` first to avoid this).
    """
    from msagent.configs.sandbox import SandboxType

    if config.type == SandboxType.SEATBELT:
        if not check_sandbox_available(config.type):
            raise RuntimeError(
                f"Seatbelt sandbox is not available on this system "
                f"(platform: {sys.platform}). "
                f"Install sandbox-exec or use a Linux host for Bubblewrap."
            )
        return SeatbeltWrapper(config)

    if config.type == SandboxType.BUBBLEWRAP:
        if not check_sandbox_available(config.type):
            raise RuntimeError(
                f"Bubblewrap sandbox is not available on this system "
                f"(platform: {sys.platform}). "
                f"Install bwrap or use a macOS host for Seatbelt."
            )
        return BubblewrapWrapper(config)

    logger.warning("Unknown sandbox type '%s' – no sandbox wrapper created.", config.type)
    return None


# ---------------------------------------------------------------------------
# Availability checks
# ---------------------------------------------------------------------------


def check_sandbox_available(sandbox_type: SandboxType) -> bool:
    """Check whether the given sandbox type is available on the current host.

    Args:
        sandbox_type: A ``SandboxType`` enum value.

    Returns:
        ``True`` if the sandbox binary is present and the platform matches.
    """
    from msagent.configs.sandbox import SandboxType

    if sandbox_type == SandboxType.SEATBELT:
        return SeatbeltWrapper.is_available()
    if sandbox_type == SandboxType.BUBBLEWRAP:
        return BubblewrapWrapper.is_available()
    return False


def validate_sandbox_config(config: SandboxConfig) -> None:
    """Validate that a sandbox config is usable on the current host.

    Checks both OS compatibility (config.os vs current platform) and binary
    availability (the sandbox tool is installed).

    Args:
        config: A SandboxConfig to validate.

    Raises:
        RuntimeError: If the OS does not match.
        ValueError: If the sandbox binary is not found.
    """
    # First check OS compatibility (uses the config's own validator).
    config.validate_current_os()

    # Then check binary availability.
    if not check_sandbox_available(config.type):
        raise ValueError(
            f"Sandbox type '{config.type.value}' is configured but the "
            f"required binary is not available on this system "
            f"(platform: {sys.platform}). "
            f"Install the sandbox tool or change the configuration."
        )


def get_available_sandbox_types() -> list[str]:
    """Return the list of sandbox types available on the current host.

    Returns:
        List of sandbox type string values that can be used.
    """
    from msagent.configs.sandbox import SandboxType

    available: list[str] = []
    for st in SandboxType:
        if check_sandbox_available(st):
            available.append(st.value)
    return available


# ---------------------------------------------------------------------------
# Configuration loading helpers
# ---------------------------------------------------------------------------


def warn_unavailable_sandboxes(sandboxes: dict[str, SandboxConfig]) -> None:
    """Log warnings for sandbox configs that cannot be used on this host.

    Call this after loading sandbox configurations to alert the user that
    their sandbox settings will not take effect.

    Args:
        sandboxes: Dictionary mapping sandbox names to SandboxConfig instances.
    """
    for name, sandbox in sandboxes.items():
        try:
            validate_sandbox_config(sandbox)
        except (RuntimeError, ValueError) as exc:
            logger.warning(
                "⚠️ Sandbox '%s' (type=%s, os=%s) is not usable on this system: %s. "
                "Tools matching this sandbox profile will run **without** OS-level isolation.",
                name,
                sandbox.type.value,
                sandbox.os.value,
                exc,
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    # Re-exports from deepagents
    "LocalShellBackend",
    "SandboxBackendProtocol",
    "BackendProtocol",
    "SandboxBackend",
    # OS-level sandbox wrappers
    "SeatbeltWrapper",
    "BubblewrapWrapper",
    # Backend wrapper for applying sandbox at execution time
    "SandboxedShellBackend",
    "_filter_sandbox_env",
    # Factory
    "create_sandbox_wrapper",
    # Availability / validation
    "check_sandbox_available",
    "validate_sandbox_config",
    "get_available_sandbox_types",
    "warn_unavailable_sandboxes",
]
