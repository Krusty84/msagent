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

"""Flags shared by /skill-mine and /direct-skill-generation.

``--policy <name>``, ``--demo``, ``--no-demo`` and ``--dry-run`` override the
config for one run only; the file is never changed. Hand-rolled on purpose:
argparse reports errors with ``sys.exit``, and ``SystemExit`` is a
``BaseException`` that no guard in the CLI catches, so a mistyped flag would
end the user's session. Every violation is a :class:`CliArgsError`. Stdlib
only (plus the policy names from :mod:`msagent.skill_evolver.config`).
"""

from __future__ import annotations

from dataclasses import dataclass

from msagent.skill_evolver.config import POLICIES

POLICY_FLAG = "--policy"
DEMO_FLAG = "--demo"
NO_DEMO_FLAG = "--no-demo"
DRY_RUN_FLAG = "--dry-run"
POLICY_USAGE = f"[{POLICY_FLAG} {'|'.join(POLICIES)}] [{DEMO_FLAG}|{NO_DEMO_FLAG}]"


class CliArgsError(ValueError):
    """The command line is not valid."""


@dataclass(slots=True)
class SharedFlags:
    """Accumulator of the shared flags; None means "not given"."""

    policy: str | None = None
    demo: bool | None = None
    dry_run: bool = False


def take_shared_flag(args: list[str], index: int, acc: SharedFlags) -> int | None:
    """Consume the shared flag at ``args[index]`` into ``acc``.

    Returns the index after the consumed tokens, or None when the token is
    not a shared flag (the caller handles it).
    """
    token = args[index]
    if token == POLICY_FLAG:
        if index + 1 >= len(args):
            raise CliArgsError(f"{POLICY_FLAG} requires a value")
        if acc.policy is not None:
            raise CliArgsError(f"{POLICY_FLAG} given twice")
        value = args[index + 1]
        if value not in POLICIES:
            raise CliArgsError(f"{POLICY_FLAG}: expected one of {', '.join(POLICIES)}, got '{value}'")
        acc.policy = value
        return index + 2
    if token in (DEMO_FLAG, NO_DEMO_FLAG):
        wanted = token == DEMO_FLAG
        if acc.demo is not None:
            if acc.demo == wanted:
                raise CliArgsError(f"{token} given twice")
            raise CliArgsError(f"{DEMO_FLAG} and {NO_DEMO_FLAG} cannot be combined")
        acc.demo = wanted
        return index + 1
    if token == DRY_RUN_FLAG:
        if acc.dry_run:
            raise CliArgsError(f"{DRY_RUN_FLAG} given twice")
        acc.dry_run = True
        return index + 1
    return None
