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

"""Built-in web search tool."""

from __future__ import annotations

import asyncio
import os
from functools import lru_cache
from typing import Any

import httpx
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field, field_validator

_DEFAULT_TIMEOUT_SECONDS = 10.0
_BING_SEARCH_API_KEY_ENV = "BING_SEARCH_API_KEY"
_BING_SEARCH_ENDPOINT_ENV = "BING_SEARCH_ENDPOINT"
_BING_SEARCH_ENDPOINT = "https://api.bing.microsoft.com/v7.0/search"
_BING_SEARCH_RESULT_COUNT = 5
_NO_RETRY_SUFFIX = (
    "Model instruction: do not retry web_search again for this request. "
    "Use the current information or explain the limitation instead."
)


class WebSearchNoResultsError(Exception):
    """Raised when the search executes successfully but returns no usable results."""


class WebSearchInput(BaseModel):
    query: str = Field(description="Search keywords used to retrieve plain-text web search results")

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must not be empty")
        return normalized


@tool("web_search", args_schema=WebSearchInput)
async def web_search(
    *,
    query: str,
    runtime: Any = None,
) -> str:
    """Search the web and return plain-text results."""
    del runtime

    try:
        payload = WebSearchInput(query=query)
    except ValueError as exc:
        raise ToolException(str(exc)) from exc

    if _resolve_bing_search_api_key():
        try:
            return await _run_bing_search(payload.query)
        except (ToolException, WebSearchNoResultsError):
            pass

    try:
        return await _run_duckduckgo_search(payload.query)
    except WebSearchNoResultsError as exc:
        return _format_search_failure_message(query=payload.query, reason=str(exc))


def _resolve_bing_search_api_key() -> str:
    return str(os.environ.get(_BING_SEARCH_API_KEY_ENV, "") or "").strip()


def _resolve_bing_search_endpoint() -> str:
    endpoint = str(os.environ.get(_BING_SEARCH_ENDPOINT_ENV, "") or "").strip()
    return endpoint or _BING_SEARCH_ENDPOINT


async def _run_bing_search(query: str) -> str:
    api_key = _resolve_bing_search_api_key()
    if not api_key:
        raise ToolException(f"Bing search requires the '{_BING_SEARCH_API_KEY_ENV}' environment variable")

    try:
        result = await asyncio.wait_for(
            _fetch_bing_search_results(query=query, api_key=api_key),
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise ToolException(f"Bing search timed out after {_DEFAULT_TIMEOUT_SECONDS:.0f}s") from exc
    except ToolException:
        raise
    except Exception as exc:
        raise ToolException(f"Bing search failed: {exc}") from exc

    if not result:
        raise WebSearchNoResultsError("Bing search returned no results")
    return result


async def _fetch_bing_search_results(*, query: str, api_key: str) -> str:
    endpoint = _resolve_bing_search_endpoint()
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    params = {
        "q": query,
        "count": _BING_SEARCH_RESULT_COUNT,
        "textDecorations": "false",
        "textFormat": "Raw",
    }

    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(endpoint, headers=headers, params=params)
    except httpx.TimeoutException as exc:
        raise ToolException(f"Bing search timed out after {_DEFAULT_TIMEOUT_SECONDS:.0f}s") from exc
    except httpx.HTTPError as exc:
        raise ToolException(f"Bing search failed: {exc}") from exc

    if response.status_code != 200:
        raise ToolException(f"Bing search failed with HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise ToolException("Bing search returned invalid JSON") from exc

    web_pages = payload.get("webPages") if isinstance(payload, dict) else None
    values = web_pages.get("value") if isinstance(web_pages, dict) else None
    if not isinstance(values, list):
        raise WebSearchNoResultsError("Bing search returned no results")

    lines = []
    for item in values:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        url = str(item.get("url", "") or "").strip()
        snippet = str(item.get("snippet", "") or "").strip()
        if not name and not snippet:
            continue
        parts = [part for part in (name, url, snippet) if part]
        lines.append("\n".join(parts))

    return "\n\n".join(lines).strip()


@lru_cache(maxsize=1)
def _get_duckduckgo_search_tool() -> Any:
    try:
        from langchain_community.tools.ddg_search.tool import DuckDuckGoSearchRun
    except ImportError as exc:
        raise ToolException("web_search requires the 'langchain-community' and 'duckduckgo-search' packages") from exc
    return DuckDuckGoSearchRun()


async def _run_duckduckgo_search(query: str) -> str:
    try:
        result = await asyncio.wait_for(
            _get_duckduckgo_search_tool().ainvoke(query),
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise ToolException(f"DuckDuckGo search timed out after {_DEFAULT_TIMEOUT_SECONDS:.0f}s") from exc
    except ToolException:
        raise
    except Exception as exc:
        raise ToolException(f"DuckDuckGo search failed: {exc}") from exc

    content = result.strip() if isinstance(result, str) else str(result).strip()
    if not content:
        raise WebSearchNoResultsError("DuckDuckGo search returned no results")
    return content


def _format_search_failure_message(*, query: str, reason: str) -> str:
    return f"Unable to complete web search for query: {query} (reason={reason})\n{_NO_RETRY_SUFFIX}"
