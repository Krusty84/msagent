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

"""Tests for skill_evolver.budget: call counting, the call bound, the context budget."""

from __future__ import annotations

import pytest

import msagent.cli.handlers  # noqa: F401
from msagent.skill_evolver.budget import (
    CHARS_PER_TOKEN,
    CLASSIFY_CALLS,
    EXPAND_CALLS,
    MIN_BUNDLE_CHARS,
    PLAN_CALLS,
    REPLY_RESERVE_TOKENS,
    USABLE_RATIO,
    ContextBudget,
    CountingLlm,
    LlmBudgetExhausted,
    llm_call_bound,
    thread_call_ceiling,
)


class _Boom:
    async def ainvoke(self, payload: object) -> object:
        raise RuntimeError("transport down")


@pytest.mark.asyncio
async def test_counting_llm_counts_every_call_and_stops_before_exceeding(fake_llm_cls) -> None:
    inner = fake_llm_cls("one", "two", "three")
    llm = CountingLlm(inner, limit=2)

    first = await llm.ainvoke([("human", "a")])
    second = await llm.ainvoke([("human", "b")])

    assert (first.content, second.content) == ("one", "two")
    assert llm.calls_used == 2
    assert llm.exhausted is True
    with pytest.raises(LlmBudgetExhausted) as info:
        await llm.ainvoke([("human", "c")])
    assert (info.value.used, info.value.limit) == (2, 2)
    assert str(info.value) == "LLM call budget exhausted (2/2 calls per thread)"
    assert len(inner.payloads) == 2
    assert inner.replies == ["three"]


@pytest.mark.asyncio
async def test_counting_llm_counts_a_failed_transport_call(fake_llm_cls) -> None:
    llm = CountingLlm(_Boom(), limit=3)

    with pytest.raises(RuntimeError, match="transport down"):
        await llm.ainvoke([("human", "a")])

    assert llm.calls_used == 1
    assert llm.exhausted is False
    assert CountingLlm(fake_llm_cls(), limit=0).exhausted is True


def test_call_constants_and_thread_ceiling() -> None:
    assert (CLASSIFY_CALLS, EXPAND_CALLS, PLAN_CALLS) == (2, 2, 6)
    assert (CHARS_PER_TOKEN, USABLE_RATIO, REPLY_RESERVE_TOKENS, MIN_BUNDLE_CHARS) == (4, 0.6, 2048, 500)
    assert thread_call_ceiling(3, expand=True) == 22
    assert thread_call_ceiling(3, expand=False) == 20
    assert thread_call_ceiling(1, expand=True) == 10


def test_llm_call_bound_formula() -> None:
    assert llm_call_bound(0, 3) == 0
    assert llm_call_bound(1, 1) == 10
    assert llm_call_bound(1, 2) == 16
    assert llm_call_bound(5, 3) == 80
    assert llm_call_bound(1, 3) == 16
    assert llm_call_bound(1, 3, max_llm_calls=16) == 16
    assert llm_call_bound(1, 3, expand=False) == 16
    assert llm_call_bound(1, 3, max_llm_calls=100) == 22
    assert llm_call_bound(1, 3, max_llm_calls=100, expand=False) == 20
    assert llm_call_bound(2, 1, max_llm_calls=4) == 8


def test_context_budget_arithmetic() -> None:
    unknown = ContextBudget.for_window(None)
    assert unknown.available_chars is None
    assert unknown.fits(10**9)
    assert unknown.room_for(10**6) is None
    assert unknown.describe() == "context window unknown; not enforced"
    assert ContextBudget.for_window(0) == unknown

    tiny = ContextBudget.for_window(1000)
    assert tiny.available_chars == 0  # 1000*4*0.6 - 2048*4 < 0, clamped
    assert tiny.room_for(10) == 0
    assert tiny.fits(0) and not tiny.fits(1)

    big = ContextBudget.for_window(128_000)
    assert big.available_chars == 307_200 - 8_192 == 299_008
    assert big.room_for(1000) == 298_008
    assert big.fits(299_008) and not big.fits(299_009)
    assert big.room_for(10**7) == 0
    assert big.describe() == "context window 128000 tokens -> 299008 chars for the prompt"
