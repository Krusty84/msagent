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

"""Tests for skill_evolver.cli_args: the flags shared by both commands."""

from __future__ import annotations

import pytest

import msagent.cli.handlers  # noqa: F401
from msagent.skill_evolver.cli_args import POLICY_USAGE, CliArgsError, SharedFlags, take_shared_flag


def _parse(args: list[str]) -> tuple[SharedFlags, list[str]]:
    """Drive take_shared_flag the way a command parser does; unknown tokens are collected."""
    acc = SharedFlags()
    rest: list[str] = []
    index = 0
    while index < len(args):
        consumed = take_shared_flag(args, index, acc)
        if consumed is None:
            rest.append(args[index])
            index += 1
            continue
        index = consumed
    return acc, rest


def test_defaults_and_consumption() -> None:
    acc, rest = _parse([])
    assert acc == SharedFlags(policy=None, demo=None, dry_run=False)
    assert rest == []

    acc, rest = _parse(["--threads", "2", "--policy", "reusable_workflow", "last", "--demo", "--dry-run"])
    assert acc == SharedFlags(policy="reusable_workflow", demo=True, dry_run=True)
    assert rest == ["--threads", "2", "last"]

    acc, _ = _parse(["--no-demo", "--policy", "strict_knowledge"])
    assert acc == SharedFlags(policy="strict_knowledge", demo=False, dry_run=False)


def test_take_shared_flag_returns_the_next_index() -> None:
    acc = SharedFlags()

    assert take_shared_flag(["--policy", "strict_knowledge", "x"], 0, acc) == 2
    assert take_shared_flag(["--policy", "strict_knowledge", "--demo"], 2, acc) == 3
    assert take_shared_flag(["--dry-run"], 0, acc) == 1
    assert take_shared_flag(["--frob"], 0, acc) is None
    assert acc == SharedFlags(policy="strict_knowledge", demo=True, dry_run=True)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--policy"], "--policy requires a value"),
        (["--policy", "x"], "--policy: expected one of strict_knowledge, reusable_workflow, got 'x'"),
        (["--policy", "--demo"], "--policy: expected one of strict_knowledge, reusable_workflow, got '--demo'"),
        (["--policy", "strict_knowledge", "--policy", "reusable_workflow"], "--policy given twice"),
        (["--policy", "strict_knowledge", "--policy"], "--policy requires a value"),
        (["--demo", "--demo"], "--demo given twice"),
        (["--no-demo", "--no-demo"], "--no-demo given twice"),
        (["--demo", "--no-demo"], "--demo and --no-demo cannot be combined"),
        (["--no-demo", "--demo"], "--demo and --no-demo cannot be combined"),
        (["--dry-run", "--dry-run"], "--dry-run given twice"),
    ],
)
def test_exact_error_messages(args: list[str], message: str) -> None:
    with pytest.raises(CliArgsError) as info:
        _parse(args)

    assert str(info.value) == message
    assert isinstance(info.value, ValueError)


def test_policy_usage_lists_the_policies() -> None:
    assert POLICY_USAGE == "[--policy strict_knowledge|reusable_workflow] [--demo|--no-demo]"
