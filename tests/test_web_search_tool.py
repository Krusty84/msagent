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
    _extract_page_text,
    _extract_relevant_snippets,
    _extract_results,
    _fetch_duckduckgo_html,
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


def test_extract_results_parses_and_deduplicates_html() -> None:
    html = """
    <a class="result__a" href="https://example.com/one"> First Result </a>
    <a class="result__a" href="https://example.com/one">Duplicate Result</a>
    <a class="result__a" href="https://docs.python.org/3/">Python Docs</a>
    <a href="/html/?q=deepagents">DuckDuckGo Internal</a>
    """

    results = _extract_results(html)

    assert results == [
        {"title": "First Result", "url": "https://example.com/one"},
        {"title": "Python Docs", "url": "https://docs.python.org/3/"},
    ]


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

    assert filtered == [
        {"title": "GitHub", "url": "https://github.com/langchain-ai/deepagents"}
    ]


def test_normalize_direct_url_accepts_urls_and_domains() -> None:
    assert (
        _normalize_direct_url("https://example.com/docs") == "https://example.com/docs"
    )
    assert (
        _normalize_direct_url("www.example.com/docs") == "https://www.example.com/docs"
    )
    assert (
        _normalize_direct_url("docs.example.com/path")
        == "https://docs.example.com/path"
    )
    assert _normalize_direct_url("vllm ascend docs") is None


@pytest.mark.asyncio
async def test_fetch_duckduckgo_html_translates_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        503, request=httpx.Request("GET", "https://html.duckduckgo.com/html/")
    )
    transport = httpx.MockTransport(lambda request: response)
    original_async_client = web_search_module.httpx.AsyncClient

    def _client(*args, **kwargs):
        return original_async_client(transport=transport)

    monkeypatch.setattr(web_search_module.httpx, "AsyncClient", _client)

    with pytest.raises(ToolException, match="DuckDuckGo web search request failed"):
        await _fetch_duckduckgo_html("deepagents")


@pytest.mark.asyncio
async def test_fetch_url_text_fetches_and_extracts_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        200,
        text="<html><head><title>Docs</title><script>ignore()</script></head>"
        "<body><h1>Docs</h1><p>Useful content.</p></body></html>",
        request=httpx.Request("GET", "https://example.com/docs"),
    )
    transport = httpx.MockTransport(lambda request: response)
    original_async_client = web_search_module.httpx.AsyncClient

    def _client(*args, **kwargs):
        return original_async_client(transport=transport)

    monkeypatch.setattr(web_search_module.httpx, "AsyncClient", _client)

    title, content, truncated = await _fetch_url_text("https://example.com/docs")

    assert title == "Docs"
    assert "Useful content." in content
    assert "ignore()" not in content
    assert truncated is False


@pytest.mark.asyncio
async def test_fetch_url_text_reports_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        404,
        request=httpx.Request("GET", "https://example.com/missing"),
    )
    transport = httpx.MockTransport(lambda request: response)
    original_async_client = web_search_module.httpx.AsyncClient

    def _client(*args, **kwargs):
        return original_async_client(transport=transport)

    monkeypatch.setattr(web_search_module.httpx, "AsyncClient", _client)

    with pytest.raises(ToolException, match="HTTP 404"):
        await _fetch_url_text("https://example.com/missing")


@pytest.mark.asyncio
async def test_fetch_url_text_reports_empty_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        200,
        text="   ",
        request=httpx.Request("GET", "https://example.com/empty"),
    )
    transport = httpx.MockTransport(lambda request: response)
    original_async_client = web_search_module.httpx.AsyncClient

    def _client(*args, **kwargs):
        return original_async_client(transport=transport)

    monkeypatch.setattr(web_search_module.httpx, "AsyncClient", _client)

    with pytest.raises(ToolException, match="empty response body"):
        await _fetch_url_text("https://example.com/empty")


@pytest.mark.asyncio
async def test_fetch_url_text_reports_unreadable_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        200,
        text="<html><head><title>Only Script</title></head><body><script>ignore()</script></body></html>",
        request=httpx.Request("GET", "https://example.com/unreadable"),
    )
    transport = httpx.MockTransport(lambda request: response)
    original_async_client = web_search_module.httpx.AsyncClient

    def _client(*args, **kwargs):
        return original_async_client(transport=transport)

    monkeypatch.setattr(web_search_module.httpx, "AsyncClient", _client)

    with pytest.raises(ToolException, match="no readable text content"):
        await _fetch_url_text("https://example.com/unreadable")


def test_extract_page_text_ignores_script_and_style() -> None:
    title, text = _extract_page_text(
        "<html><head><title>Example</title><style>.x{}</style></head>"
        "<body><h1>Hello</h1><script>alert(1)</script><p>World</p></body></html>"
    )

    assert title == "Example"
    assert "Hello" in text
    assert "World" in text
    assert "alert" not in text
    assert ".x" not in text


@pytest.mark.asyncio
async def test_search_result_urls_uses_duckduckgo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_search_module,
        "_fetch_duckduckgo_html",
        AsyncMock(
            return_value=(
                '<a class="result__a" href="https://github.com/langchain-ai/deepagents">deepagents</a>'
            )
        ),
    )

    results, failure = await _search_result_urls(
        query="deepagents",
        allowed_domains=set(),
        blocked_domains=set(),
        limit=5,
    )

    assert failure is not None
    assert failure.provider == "DuckDuckGo HTML"
    assert failure.attempts == 1
    assert results == [
        {"title": "deepagents", "url": "https://github.com/langchain-ai/deepagents"}
    ]


@pytest.mark.asyncio
async def test_web_search_formats_url_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        web_search_module,
        "_search_result_urls",
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
    assert "Provider: DuckDuckGo HTML" in result
    assert "1. deepagents" in result
    assert "URL: https://github.com/langchain-ai/deepagents" in result


@pytest.mark.asyncio
async def test_web_fetch_formats_page_content(monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.mark.asyncio
async def test_web_fetch_reports_timeout_with_no_retry_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_search_module,
        "_fetch_url_text",
        AsyncMock(side_effect=ToolException("URL fetch request timed out after 30s")),
    )

    result = await web_fetch.coroutine(url="https://example.com/docs")

    assert (
        "Unable to fetch usable web page content from: https://example.com/docs"
        in result
    )
    assert "reason=URL fetch request timed out after 30s" in result
    assert "do not retry web_search or web_fetch again for this request" in result


@pytest.mark.asyncio
async def test_web_fetch_reports_query_no_match_with_no_retry_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_search_module,
        "_fetch_url_text",
        AsyncMock(return_value=("Docs", "Alpha paragraph.\n\nBeta paragraph.", False)),
    )

    result = await web_fetch.coroutine(
        url="https://example.com/docs",
        query="useful keyword",
    )

    assert (
        "Unable to fetch usable web page content from: https://example.com/docs"
        in result
    )
    assert "reason=query matched no relevant snippets on the page" in result
    assert "query=useful keyword" in result
    assert "do not retry web_search or web_fetch again for this request" in result


def test_extract_relevant_snippets_filters_by_query_terms() -> None:
    content = "Alpha paragraph.\n\nBeta useful paragraph.\n\nGamma paragraph."

    result = _extract_relevant_snippets(content, "useful", max_chars=1000)

    assert "Beta useful paragraph." in result
    assert "Alpha paragraph." not in result


@pytest.mark.asyncio
async def test_web_search_returns_no_results_message_when_filtered_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    assert "No web results found for query: deepagents" in result
    assert "reason=no results matched allowed domains: python.org" in result
    assert "attempts=3" in result
    assert "allowed=python.org" in result
    assert "do not retry web_search or web_fetch again for this request" in result


@pytest.mark.asyncio
async def test_web_search_reports_timeout_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    assert "reason=search timed out after 60s" in result
    assert "attempts=3" in result
    assert "do not retry web_search or web_fetch again for this request" in result


@pytest.mark.asyncio
async def test_search_result_urls_stops_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_search_module,
        "_try_fetch_duckduckgo_html",
        AsyncMock(return_value=(None, "DuckDuckGo web search request failed: 503")),
    )

    results, failure = await _search_result_urls(
        query="deepagents",
        allowed_domains=set(),
        blocked_domains=set(),
        limit=5,
    )

    assert results == []
    assert failure is not None
    assert failure.attempts == 3
    assert failure.reason == "DuckDuckGo web search request failed: 503"
