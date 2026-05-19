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

import os
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field, field_validator

_DUCKDUCKGO_SEARCH_URL = "https://html.duckduckgo.com/html/"
_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_TAVILY_CRAWL_URL = "https://api.tavily.com/crawl"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_TAVILY_CRAWL_TIMEOUT_SECONDS = 150.0
_DEFAULT_RESULT_LIMIT = 5
_MAX_RESULT_LIMIT = 10
_DEFAULT_FETCH_MAX_CHARS = 8000
_MAX_FETCH_CHARS = 20000
_USER_AGENT = "msagent/0.1 web-search"
_TAVILY_API_KEY_ENV = "TAVILY_API_KEY"


class WebSearchInput(BaseModel):
    query: str = Field(description="Search query")
    allowed_domains: list[str] = Field(
        default_factory=list,
        description="Only include results from these domains",
    )
    blocked_domains: list[str] = Field(
        default_factory=list,
        description="Exclude results from these domains",
    )
    limit: int = Field(
        default=_DEFAULT_RESULT_LIMIT,
        ge=1,
        le=_MAX_RESULT_LIMIT,
        description="Maximum number of results to return",
    )

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must not be empty")
        return normalized

    @field_validator("allowed_domains", "blocked_domains")
    @classmethod
    def _normalize_domains(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            domain = value.strip().lower()
            if not domain:
                continue
            if "://" in domain:
                parsed = urlparse(domain)
                domain = (parsed.hostname or "").strip().lower()
            domain = domain.lstrip(".")
            if domain.startswith("www."):
                domain = domain[4:]
            if domain and domain not in seen:
                seen.add(domain)
                normalized.append(domain)
        return normalized


class WebFetchInput(BaseModel):
    url: str = Field(description="HTTP or HTTPS URL, or bare domain, to crawl")
    extract_mode: Literal["auto", "text", "markdown"] = Field(
        default="markdown",
        description="Extraction format: auto and markdown return Markdown, text returns plain text",
    )
    max_chars: int = Field(
        default=_DEFAULT_FETCH_MAX_CHARS,
        ge=1000,
        le=_MAX_FETCH_CHARS,
        description="Maximum number of crawled page text characters to return",
    )
    query: str | None = Field(
        default=None,
        description="Optional crawl instructions used by Tavily to find relevant pages from the URL.",
    )

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        normalized = value.strip()
        if "://" not in normalized:
            normalized = f"https://{normalized}"
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an HTTP or HTTPS URL, or a bare domain")
        return normalized

    @field_validator("query")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_title = False
        self._current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr_map = dict(attrs)
        href = attr_map.get("href") or ""
        class_name = attr_map.get("class") or ""
        if "result__a" not in class_name or not href:
            return
        self._current = {"title": "", "url": href.strip()}
        self._in_title = True

    def handle_data(self, data: str) -> None:
        if self._in_title and self._current is not None:
            self._current["title"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._in_title or self._current is None:
            return
        title = _clean_text(self._current.get("title", ""))
        url = _normalize_result_url(self._current.get("url", ""))
        if title and url:
            self.results.append({"title": title, "url": url})
        self._current = None
        self._in_title = False


@tool("web_search", args_schema=WebSearchInput)
async def web_search(
    *,
    query: str,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    limit: int = _DEFAULT_RESULT_LIMIT,
    runtime: Any = None,
) -> str:
    """Search the web and return compact results with source URLs."""
    del runtime

    try:
        payload = WebSearchInput(
            query=query,
            allowed_domains=list(allowed_domains or []),
            blocked_domains=list(blocked_domains or []),
            limit=limit,
        )
    except ValueError as exc:
        raise ToolException(str(exc)) from exc

    allowed_domain_set = set(payload.allowed_domains)
    blocked_domain_set = set(payload.blocked_domains)

    results, provider = await _search_results_with_provider(
        query=payload.query,
        allowed_domains=allowed_domain_set,
        blocked_domains=blocked_domain_set,
        limit=payload.limit,
    )

    if not results:
        filters = []
        if payload.allowed_domains:
            filters.append(f"allowed={','.join(payload.allowed_domains)}")
        if payload.blocked_domains:
            filters.append(f"blocked={','.join(payload.blocked_domains)}")
        suffix = f" ({'; '.join(filters)})" if filters else ""
        return f"No web results found for query: {payload.query}{suffix}"

    lines = [f"Web search results for: {payload.query}", f"Provider: {provider}"]
    for index, result in enumerate(results[: payload.limit], start=1):
        lines.append(f"{index}. {result['title']}\n   URL: {result['url']}")
    return "\n".join(lines)


@tool("web_fetch", args_schema=WebFetchInput)
async def web_fetch(
    *,
    url: str,
    extract_mode: Literal["auto", "text", "markdown"] = "markdown",
    max_chars: int = _DEFAULT_FETCH_MAX_CHARS,
    query: str | None = None,
    runtime: Any = None,
) -> str:
    """Fetch a web page URL and return cleaned, length-limited page content."""
    del runtime

    try:
        payload = WebFetchInput(
            url=url,
            extract_mode=extract_mode,
            max_chars=max_chars,
            query=query,
        )
    except ValueError as exc:
        raise ToolException(str(exc)) from exc

    title, content, truncated = await _fetch_page_text(
        payload.url,
        max_chars=payload.max_chars,
        extract_mode=payload.extract_mode,
        query=payload.query,
    )

    lines = [f"Web page content for: {title or payload.url}", f"URL: {payload.url}"]
    lines.append(f"Content: (truncated to {payload.max_chars} chars)" if truncated else "Content:")
    lines.append(content or "No readable page text found.")
    return "\n".join(lines)


async def _search_results_with_provider(
    *,
    query: str,
    allowed_domains: set[str],
    blocked_domains: set[str],
    limit: int,
) -> tuple[list[dict[str, str]], str]:
    tavily_api_key = os.getenv(_TAVILY_API_KEY_ENV, "").strip()
    if tavily_api_key:
        try:
            results = await _search_with_tavily(
                query=query,
                api_key=tavily_api_key,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                limit=limit,
            )
            return results, "Tavily"
        except ToolException:
            pass

    html = await _fetch_duckduckgo_html(query)
    results = _extract_results(html)
    filtered = _filter_results(
        results,
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
    )
    return filtered, "DuckDuckGo HTML fallback"


async def _fetch_page_text(
    url: str,
    max_chars: int,
    extract_mode: Literal["auto", "text", "markdown"] = "markdown",
    query: str | None = None,
) -> tuple[str, str, bool]:
    tavily_api_key = os.getenv(_TAVILY_API_KEY_ENV, "").strip()
    if not tavily_api_key:
        raise ToolException(f"{_TAVILY_API_KEY_ENV} is required for web_fetch")

    text = await _crawl_with_tavily(
        url=url,
        api_key=tavily_api_key,
        extract_mode=extract_mode,
        query=query,
    )
    truncated = len(text) > max_chars
    return "", _truncate_text(text, max_chars), truncated


async def _crawl_with_tavily(
    *,
    url: str,
    api_key: str,
    extract_mode: Literal["auto", "text", "markdown"],
    query: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "url": url,
        "instructions": query or "",
        "chunks_per_source": 3,
        "max_depth": 1,
        "max_breadth": 20,
        "limit": 50,
        "select_paths": None,
        "select_domains": None,
        "exclude_paths": None,
        "exclude_domains": None,
        "allow_external": True,
        "include_images": False,
        "extract_depth": "basic",
        "format": "text" if extract_mode == "text" else "markdown",
        "include_favicon": False,
        "timeout": int(_TAVILY_CRAWL_TIMEOUT_SECONDS),
        "include_usage": False,
    }

    try:
        async with httpx.AsyncClient(
            timeout=_TAVILY_CRAWL_TIMEOUT_SECONDS + 10,
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": _USER_AGENT,
            },
        ) as client:
            response = await client.post(_TAVILY_CRAWL_URL, json=payload)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolException(f"Tavily web crawl request failed: {exc}") from exc

    try:
        response_payload = response.json()
    except ValueError as exc:
        raise ToolException("Tavily web crawl returned invalid JSON") from exc

    results = response_payload.get("results") or []
    if not results:
        failed = response_payload.get("failed_results") or []
        if failed:
            error = failed[0].get("error") if isinstance(failed[0], dict) else None
            raise ToolException(f"Tavily web crawl returned no content: {error or 'unknown error'}")
        return ""

    pages: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        raw_content = _clean_multiline_text(str(item.get("raw_content") or ""))
        if not raw_content:
            continue
        page_url = _clean_text(str(item.get("url") or ""))
        pages.append(f"Source: {page_url}\n\n{raw_content}" if page_url else raw_content)
    return _clean_multiline_text("\n\n---\n\n".join(pages))


async def _search_with_tavily(
    *,
    query: str,
    api_key: str,
    allowed_domains: set[str],
    blocked_domains: set[str],
    limit: int,
) -> list[dict[str, str]]:
    payload: dict[str, Any] = {
        "api_key": api_key,
        "query": query,
        "max_results": limit,
        "search_depth": "advanced",
        "include_answer": False,
        "include_raw_content": False,
    }
    if allowed_domains:
        payload["include_domains"] = sorted(allowed_domains)
    if blocked_domains:
        payload["exclude_domains"] = sorted(blocked_domains)

    try:
        async with httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            response = await client.post(_TAVILY_SEARCH_URL, json=payload)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolException(f"Tavily web search request failed: {exc}") from exc

    try:
        response_payload = response.json()
    except ValueError as exc:
        raise ToolException("Tavily web search returned invalid JSON") from exc

    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in response_payload.get("results", []) or []:
        url = str(item.get("url") or "").strip()
        title = _clean_text(str(item.get("title") or url))
        if not url or not title or url in seen_urls:
            continue
        seen_urls.add(url)
        results.append({"title": title, "url": url})
    return results


async def _fetch_duckduckgo_html(query: str) -> str:
    try:
        async with httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            response = await client.get(_DUCKDUCKGO_SEARCH_URL, params={"q": query})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolException(f"DuckDuckGo web search request failed: {exc}") from exc
    return response.text


def _extract_results(html: str) -> list[dict[str, str]]:
    parser = _DuckDuckGoHTMLParser()
    parser.feed(html)
    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for result in parser.results:
        url = result["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        results.append(result)
    return results


def _filter_results(
    results: list[dict[str, str]],
    *,
    allowed_domains: set[str],
    blocked_domains: set[str],
) -> list[dict[str, str]]:
    filtered: list[dict[str, str]] = []
    for result in results:
        hostname = _normalize_hostname(result["url"])
        if not hostname:
            continue
        if allowed_domains and not _domain_matches(hostname, allowed_domains):
            continue
        if blocked_domains and _domain_matches(hostname, blocked_domains):
            continue
        filtered.append(result)
    return filtered


def _domain_matches(hostname: str, domains: set[str]) -> bool:
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains)


def _normalize_hostname(url: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").strip().lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname


def _normalize_result_url(url: str) -> str:
    normalized = unescape(url.strip())
    if not normalized:
        return ""
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    if normalized.startswith("/"):
        normalized = urljoin(_DUCKDUCKGO_SEARCH_URL, normalized)

    parsed = urlparse(normalized)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path == "/l/":
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            normalized = unquote(target)
    return normalized


def _clean_text(value: str) -> str:
    normalized = re.sub(r"\s+", " ", unescape(value or "")).strip()
    return normalized


def _clean_multiline_text(value: str) -> str:
    text = unescape(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _truncate_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."
