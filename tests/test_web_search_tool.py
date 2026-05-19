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

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from langchain_core.tools import ToolException

import msagent.tools.web_search as web_search_module
from msagent.tools.web_search import (
    WebFetchInput,
    WebSearchInput,
    _extract_relevant_snippets,
    _fetch_exa_search_results,
    _fetch_url_text,
    _filter_results,
    _normalize_direct_url,
    _search_result_urls,
    web_fetch,
    web_search,
)


def test_web_search_input_normalizes_domains() -> None:
    payload = WebSearchInput(
        query="  langchain deepagents  ",
        allowed_domains=["https://www.github.com", "github.com", "docs.python.org"],
        blocked_domains=["www.example.com", "example.com"],
    )

    assert payload.query == "langchain deepagents"
    assert payload.allowed_domains == ["github.com", "docs.python.org"]
    assert payload.blocked_domains == ["example.com"]


def test_web_fetch_input_normalizes_bare_domain_url() -> None:
    payload = WebFetchInput(url="docs.example.com/path", query="  install  ")

    assert payload.url == "https://docs.example.com/path"
    assert payload.query == "install"


def test_filter_results_honors_allowed_and_blocked_domains() -> None:
    results = [
        {"title": "GitHub", "url": "https://github.com/langchain-ai/deepagents"},
        {"title": "Python", "url": "https://docs.python.org/3/"},
        {"title": "Blocked", "url": "https://sub.example.com/page"},
    ]

    filtered = _filter_results(
        results,
        allowed_domains={"github.com", "example.com"},
        blocked_domains={"example.com"},
    )

    assert filtered == [{"title": "GitHub", "url": "https://github.com/langchain-ai/deepagents"}]


def test_normalize_direct_url_accepts_urls_and_domains() -> None:
    assert _normalize_direct_url("https://example.com/docs") == "https://example.com/docs"
    assert _normalize_direct_url("www.example.com/docs") == "https://www.example.com/docs"
    assert _normalize_direct_url("docs.example.com/path") == "https://docs.example.com/path"
    assert _normalize_direct_url("vllm ascend docs") is None


@pytest.mark.asyncio
async def test_fetch_exa_search_results_translates_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        503,
        json={"message": "upstream unavailable"},
        request=httpx.Request("POST", "https://api.exa.ai/search"),
    )
    transport = httpx.MockTransport(lambda request: response)
    original_async_client = web_search_module.httpx.AsyncClient

    def _client(*args, **kwargs):
        return original_async_client(transport=transport)

    monkeypatch.setattr(web_search_module.httpx, "AsyncClient", _client)

    with pytest.raises(ToolException, match="Exa web search request failed: HTTP 503"):
        await _fetch_exa_search_results(api_key="exa-key", query="deepagents", limit=5)


@pytest.mark.asyncio
async def test_fetch_url_text_fetches_exa_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        200,
        json={
            "results": [
                {
                    "title": "Docs",
                    "text": "Useful content.\n\nOther content.",
                }
            ]
        },
        request=httpx.Request("POST", "https://api.exa.ai/contents"),
    )
    transport = httpx.MockTransport(lambda request: response)
    original_async_client = web_search_module.httpx.AsyncClient

    def _client(*args, **kwargs):
        return original_async_client(transport=transport)

    monkeypatch.setattr(web_search_module.httpx, "AsyncClient", _client)

    title, content, truncated = await _fetch_url_text(
        "https://example.com/docs",
        api_key="exa-key",
    )

    assert title == "Docs"
    assert "Useful content." in content
    assert truncated is False


@pytest.mark.asyncio
async def test_search_result_urls_uses_exa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_search_module,
        "_fetch_exa_search_results",
        AsyncMock(
            return_value=[
                {"title": "deepagents", "url": "https://github.com/langchain-ai/deepagents"}
            ]
        ),
    )

    results, failure = await _search_result_urls(
        api_key="exa-key",
        query="deepagents",
        allowed_domains=set(),
        blocked_domains=set(),
        limit=5,
    )

    assert failure is not None
    assert failure.provider == "Exa"
    assert failure.attempts == 1
    assert results == [{"title": "deepagents", "url": "https://github.com/langchain-ai/deepagents"}]


@pytest.mark.asyncio
async def test_web_search_formats_url_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    monkeypatch.setattr(
        web_search_module,
        "_run_search_with_deadline",
        AsyncMock(
            return_value=(
                [
                    {
                        "title": "deepagents",
                        "url": "https://github.com/langchain-ai/deepagents",
                    }
                ],
                web_search_module.SearchFailure(reason="search succeeded", attempts=1),
            )
        ),
    )

    result = await web_search.coroutine(query="deepagents")

    assert "Web search URL results for: deepagents" in result
    assert "Provider: Exa" in result
    assert "1. deepagents" in result
    assert "URL: https://github.com/langchain-ai/deepagents" in result


@pytest.mark.asyncio
async def test_web_fetch_formats_page_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    monkeypatch.setattr(
        web_search_module,
        "_fetch_url_text",
        AsyncMock(return_value=("Docs", "Useful content.\n\nOther content.", False)),
    )

    result = await web_fetch.coroutine(
        url="https://example.com/docs",
        query="Useful",
    )

    assert "Web page content for: Docs" in result
    assert "URL: https://example.com/docs" in result
    assert "Query: Useful" in result
    assert "Useful content." in result


def test_extract_relevant_snippets_filters_by_query_terms() -> None:
    content = "Alpha paragraph.\n\nBeta useful paragraph.\n\nGamma paragraph."

    result = _extract_relevant_snippets(content, "useful", max_chars=1000)

    assert "Beta useful paragraph." in result
    assert "Alpha paragraph." not in result


@pytest.mark.asyncio
async def test_web_search_returns_no_results_message_when_filtered_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    monkeypatch.setattr(
        web_search_module,
        "_run_search_with_deadline",
        AsyncMock(
            return_value=(
                [],
                web_search_module.SearchFailure(
                    reason="no results matched allowed domains: python.org",
                    attempts=3,
                ),
            )
        ),
    )

    result = await web_search.coroutine(
        query="deepagents",
        allowed_domains=["python.org"],
    )

    assert (
        result
        == "No web results found for query: deepagents "
        "(reason=no results matched allowed domains: python.org; attempts=3; allowed=python.org)"
    )


@pytest.mark.asyncio
async def test_web_search_reports_timeout_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    monkeypatch.setattr(
        web_search_module,
        "_run_search_with_deadline",
        AsyncMock(
            return_value=(
                [],
                web_search_module.SearchFailure(
                    reason="search timed out after 60s",
                    attempts=3,
                ),
            )
        ),
    )

    result = await web_search.coroutine(query="deepagents")

    assert result == "No web results found for query: deepagents (reason=search timed out after 60s; attempts=3)"


@pytest.mark.asyncio
async def test_search_result_urls_stops_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        web_search_module,
        "_try_fetch_exa_search_results",
        AsyncMock(return_value=(None, "Exa web search request failed: HTTP 503 (upstream unavailable)")),
    )

    results, failure = await _search_result_urls(
        api_key="exa-key",
        query="deepagents",
        allowed_domains=set(),
        blocked_domains=set(),
        limit=5,
    )

    assert results == []
    assert failure is not None
    assert failure.attempts == 3
    assert failure.reason == "Exa web search request failed: HTTP 503 (upstream unavailable)"


@pytest.mark.asyncio
async def test_web_search_requires_exa_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)

    with pytest.raises(ToolException, match="EXA_API_KEY is required"):
        await web_search.coroutine(query="deepagents")


@pytest.mark.asyncio
async def test_web_fetch_requires_exa_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)

    with pytest.raises(ToolException, match="EXA_API_KEY is required"):
        await web_fetch.coroutine(url="https://example.com/docs")
