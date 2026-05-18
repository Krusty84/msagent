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

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain_core.tools import ToolException

import msagent.tools.web_search as web_search_module
from msagent.tools.web_search import (
    WebSearchInput,
    _extract_results,
    _extract_readable_text,
    _fetch_duckduckgo_html,
    _fetch_page_text,
    _filter_results,
    _normalize_result_url,
    _search_results_with_provider,
    _search_with_tavily,
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


def test_extract_results_parses_and_deduplicates_html() -> None:
    html = """
    <a class="result__a" href="//example.com/one"> First Result </a>
    <a class="result__a" href="https://example.com/one">Duplicate Result</a>
    <a class="result__a" href="https://docs.python.org/3/">Python Docs</a>
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

    assert filtered == [{"title": "GitHub", "url": "https://github.com/langchain-ai/deepagents"}]


def test_normalize_result_url_decodes_duckduckgo_redirect() -> None:
    url = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Flangchain-ai%2Fdeepagents"

    assert _normalize_result_url(url) == "https://github.com/langchain-ai/deepagents"


def test_extract_readable_text_skips_scripts_and_styles() -> None:
    html = """
    <html>
      <head><title> Example Page </title><style>.hidden { display: none; }</style></head>
      <body>
        <script>alert("ignore")</script>
        <h1>Heading</h1>
        <p> First paragraph. </p>
        <p>Second paragraph.</p>
      </body>
    </html>
    """

    title, text = _extract_readable_text(html)

    assert title == "Example Page"
    assert "Heading" in text
    assert "First paragraph." in text
    assert "Second paragraph." in text
    assert "alert" not in text
    assert "display" not in text


def test_extract_readable_text_supports_selector_and_query_filters() -> None:
    html = """
    <html>
      <head><title>Docs</title></head>
      <body>
        <div class="nav">Navigation Qwen3.5</div>
        <article id="main">
          <p>Qwen3.5 recommended tensor parallel size is 4.</p>
          <p>Kimi deployment notes.</p>
        </article>
      </body>
    </html>
    """

    title, text = _extract_readable_text(html, selector="p", query="Qwen3.5 tensor")

    assert title == "Docs"
    assert text == "Qwen3.5 recommended tensor parallel size is 4."
    assert "Navigation" not in text
    assert "Kimi" not in text


def test_extract_readable_text_selector_can_match_ancestor_blocks() -> None:
    html = """
    <html>
      <body>
        <div class="nav">Navigation Qwen3.5</div>
        <article id="main">
          <p>Qwen3.5 recommended tensor parallel size is 4.</p>
          <p>Kimi deployment notes.</p>
        </article>
      </body>
    </html>
    """

    _, text = _extract_readable_text(html, selector="#main", query="Qwen3.5 tensor")

    assert text == "Qwen3.5 recommended tensor parallel size is 4."
    assert "Navigation" not in text
    assert "Kimi" not in text


def test_extract_readable_text_supports_markdown_mode() -> None:
    html = """
    <html><body>
      <h1>Title</h1>
      <p>Read <a href="https://example.com/docs">docs</a>.</p>
      <li>First item</li>
    </body></html>
    """

    _, text = _extract_readable_text(html, extract_mode="markdown")

    assert "# Title" in text
    assert "[docs](https://example.com/docs)" in text
    assert "- First item" in text


def test_extract_readable_text_auto_mode_returns_markdown() -> None:
    html = "<html><body><h1>Title</h1><p>Read <a href=\"https://example.com/docs\">docs</a>.</p></body></html>"

    _, text = _extract_readable_text(html)

    assert "# Title" in text
    assert "[docs](https://example.com/docs)" in text


@pytest.mark.asyncio
async def test_fetch_duckduckgo_html_translates_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx.Response(503, request=httpx.Request("GET", "https://html.duckduckgo.com/html/"))
    transport = httpx.MockTransport(lambda request: response)
    original_async_client = web_search_module.httpx.AsyncClient

    def _client(*args, **kwargs):
        return original_async_client(transport=transport)

    monkeypatch.setattr(web_search_module.httpx, "AsyncClient", _client)

    with pytest.raises(ToolException, match="DuckDuckGo web search request failed"):
        await _fetch_duckduckgo_html("deepagents")


@pytest.mark.asyncio
async def test_fetch_page_text_extracts_and_truncates_html(monkeypatch: pytest.MonkeyPatch) -> None:
    html = "<html><head><title>Docs</title></head><body><h1>Intro</h1><p>abcdefg</p></body></html>"
    response = httpx.Response(
        200,
        text=html,
        headers={"content-type": "text/html; charset=utf-8"},
        request=httpx.Request("GET", "https://example.com/docs"),
    )
    transport = httpx.MockTransport(lambda request: response)
    original_async_client = web_search_module.httpx.AsyncClient

    def _client(*args, **kwargs):
        return original_async_client(transport=transport)

    monkeypatch.setattr(web_search_module.httpx, "AsyncClient", _client)

    title, content, truncated = await _fetch_page_text("https://example.com/docs", 5, extract_mode="text")

    assert title == "Docs"
    assert content == "In..."
    assert truncated is True


@pytest.mark.asyncio
async def test_search_with_tavily_formats_and_deduplicates_results(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_payload = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        captured_payload = json.loads(request.content.decode("utf-8"))
        return response

    response = httpx.Response(
        200,
        json={
            "results": [
                {
                    "title": "deepagents",
                    "url": "https://github.com/langchain-ai/deepagents",
                },
                {"title": "duplicate", "url": "https://github.com/langchain-ai/deepagents"},
                {"title": "", "url": "https://docs.python.org/3/"},
            ]
        },
        request=httpx.Request("POST", "https://api.tavily.com/search"),
    )
    transport = httpx.MockTransport(_handler)
    original_async_client = web_search_module.httpx.AsyncClient

    def _client(*args, **kwargs):
        return original_async_client(transport=transport)

    monkeypatch.setattr(web_search_module.httpx, "AsyncClient", _client)

    results = await _search_with_tavily(
        query="deepagents",
        api_key="test-key",
        allowed_domains=set(),
        blocked_domains=set(),
        limit=5,
    )

    assert results == [
        {"title": "deepagents", "url": "https://github.com/langchain-ai/deepagents"},
        {"title": "https://docs.python.org/3/", "url": "https://docs.python.org/3/"},
    ]
    assert captured_payload["include_answer"] is False
    assert captured_payload["include_raw_content"] is False


@pytest.mark.asyncio
async def test_search_results_with_provider_uses_tavily_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(
        web_search_module,
        "_search_with_tavily",
        AsyncMock(return_value=[{"title": "deepagents", "url": "https://github.com/langchain-ai/deepagents"}]),
    )
    monkeypatch.setattr(
        web_search_module,
        "_fetch_duckduckgo_html",
        AsyncMock(side_effect=AssertionError("DuckDuckGo fallback should not be used")),
    )

    results, provider = await _search_results_with_provider(
        query="deepagents",
        allowed_domains=set(),
        blocked_domains=set(),
        limit=5,
    )

    assert provider == "Tavily"
    assert results == [{"title": "deepagents", "url": "https://github.com/langchain-ai/deepagents"}]


@pytest.mark.asyncio
async def test_search_results_with_provider_falls_back_without_tavily_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(
        web_search_module,
        "_fetch_duckduckgo_html",
        AsyncMock(
            return_value='''<a class="result__a" href="https://github.com/langchain-ai/deepagents">deepagents</a>'''
        ),
    )

    results, provider = await _search_results_with_provider(
        query="deepagents",
        allowed_domains=set(),
        blocked_domains=set(),
        limit=5,
    )

    assert provider == "DuckDuckGo HTML fallback"
    assert results == [{"title": "deepagents", "url": "https://github.com/langchain-ai/deepagents"}]


@pytest.mark.asyncio
async def test_search_results_with_provider_falls_back_when_tavily_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(
        web_search_module,
        "_search_with_tavily",
        AsyncMock(side_effect=ToolException("boom")),
    )
    monkeypatch.setattr(
        web_search_module,
        "_fetch_duckduckgo_html",
        AsyncMock(return_value='''<a class="result__a" href="https://docs.python.org/3/">Python Docs</a>'''),
    )

    results, provider = await _search_results_with_provider(
        query="deepagents",
        allowed_domains=set(),
        blocked_domains=set(),
        limit=5,
    )

    assert provider == "DuckDuckGo HTML fallback"
    assert results == [{"title": "Python Docs", "url": "https://docs.python.org/3/"}]


@pytest.mark.asyncio
async def test_web_search_formats_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        web_search_module,
        "_search_results_with_provider",
        AsyncMock(
            return_value=(
                [{"title": "deepagents", "url": "https://github.com/langchain-ai/deepagents"}],
                "Tavily",
            )
        ),
    )

    result = await web_search.coroutine(query="deepagents")

    assert "Web search results for: deepagents" in result
    assert "Provider: Tavily" in result
    assert "1. deepagents" in result
    assert "URL: https://github.com/langchain-ai/deepagents" in result


@pytest.mark.asyncio
async def test_web_fetch_formats_page_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        web_search_module,
        "_fetch_page_text",
        AsyncMock(return_value=("Docs", "Useful content.", False)),
    )

    result = await web_fetch.coroutine(
        url="https://example.com/docs",
        extract_mode="markdown",
        selector="article",
        query="Useful",
    )

    assert "Web page content for: Docs" in result
    assert "URL: https://example.com/docs" in result
    assert "Useful content." in result


@pytest.mark.asyncio
async def test_web_fetch_uses_runtime_extractor_when_prompt_is_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _extractor(**kwargs):
        captured.update(kwargs)
        return "## Extracted\n\n- Recommended tensor parallel size is 4."

    monkeypatch.setattr(
        web_search_module,
        "_fetch_page_text",
        AsyncMock(return_value=("Docs", "# Useful\n\nFull Markdown page.", False)),
    )

    result = await web_fetch.coroutine(
        url="https://example.com/docs",
        extract_prompt="Extract Qwen3.5 recommended configuration.",
        runtime=SimpleNamespace(context=SimpleNamespace(web_fetch_extractor=_extractor)),
    )

    assert "## Extracted" in result
    assert "Recommended tensor parallel size is 4." in result
    assert captured["prompt"] == "Extract Qwen3.5 recommended configuration."
    assert captured["markdown"] == "# Useful\n\nFull Markdown page."
    assert captured["url"] == "https://example.com/docs"


@pytest.mark.asyncio
async def test_web_fetch_auto_extracts_without_explicit_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _extractor(**kwargs):
        captured.update(kwargs)
        return "## Extracted automatically"

    monkeypatch.setattr(
        web_search_module,
        "_fetch_page_text",
        AsyncMock(return_value=("Docs", "# Useful", False)),
    )

    result = await web_fetch.coroutine(
        url="https://example.com/docs",
        query="Qwen3.5 config",
        runtime=SimpleNamespace(context=SimpleNamespace(web_fetch_extractor=_extractor)),
    )

    assert "## Extracted automatically" in result
    assert "Qwen3.5 config" in str(captured["prompt"])


@pytest.mark.asyncio
async def test_web_fetch_falls_back_to_markdown_without_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        web_search_module,
        "_fetch_page_text",
        AsyncMock(return_value=("Docs", "# Useful", False)),
    )

    result = await web_fetch.coroutine(
        url="https://example.com/docs",
        extract_prompt="Extract useful content.",
    )

    assert "# Useful" in result


@pytest.mark.asyncio
async def test_web_search_returns_no_results_message_when_filtered_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        web_search_module,
        "_search_results_with_provider",
        AsyncMock(return_value=([], "DuckDuckGo HTML fallback")),
    )

    result = await web_search.coroutine(
        query="deepagents",
        allowed_domains=["python.org"],
    )

    assert result == "No web results found for query: deepagents (allowed=python.org)"
