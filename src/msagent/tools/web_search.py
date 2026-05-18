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
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field, field_validator

_DUCKDUCKGO_SEARCH_URL = "https://html.duckduckgo.com/html/"
_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_RESULT_LIMIT = 5
_MAX_RESULT_LIMIT = 10
_USER_AGENT = "msagent/0.1 web-search"
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
_TAVILY_API_KEY_ENV = "TAVILY_API_KEY"
_MAX_FETCHED_CONTENT_CHARS = 4000
_URL_PATTERN = re.compile(r"https?://[^\s|<>)\]}\"']+")


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


class _ReadableHTMLParser(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "nav", "footer"}

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.text_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if normalized == "title":
            self._in_title = True
            return
        if normalized in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth == 0:
            cleaned = _clean_text(data)
            if cleaned:
                self.text_parts.append(cleaned)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized == "title":
            self._in_title = False
            return
        if normalized in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def readable_text(self) -> str:
        return _clean_text(" ".join(self.text_parts))


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

    urls = _extract_urls(payload.query)
    if urls:
        return await _fetch_url_contents(urls[: payload.limit])

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


async def _fetch_url_content(url: str) -> str:
    normalized_url = _normalize_url_query(url)
    try:
        async with httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers=_url_fetch_headers(normalized_url),
        ) as client:
            response = await client.get(normalized_url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolException(f"URL fetch request failed: {exc}") from exc

    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        text = _clean_text(response.text)
        return _format_fetched_url_content(
            url=str(response.url),
            title="",
            content=text,
            status_code=response.status_code,
            content_type=content_type,
        )

    parser = _ReadableHTMLParser()
    parser.feed(response.text)
    return _format_fetched_url_content(
        url=str(response.url),
        title=_clean_text(parser.title),
        content=parser.readable_text(),
        status_code=response.status_code,
        content_type=content_type,
    )


async def _fetch_url_contents(urls: list[str]) -> str:
    lines = ["Fetched URL contents by priority:"]
    for index, url in enumerate(urls, start=1):
        try:
            content = await _fetch_url_content(url)
        except ToolException as exc:
            content = f"Fetch failed for: {url}\nError: {exc}"
        lines.append(f"\nPriority {index}\n{content}")
    return "\n".join(lines)


def _format_fetched_url_content(
    *,
    url: str,
    title: str,
    content: str,
    status_code: int,
    content_type: str,
) -> str:
    trimmed = content[:_MAX_FETCHED_CONTENT_CHARS]
    if len(content) > _MAX_FETCHED_CONTENT_CHARS:
        trimmed = f"{trimmed}..."

    lines = [
        f"Fetched URL content for: {url}",
        f"Status: {status_code}",
        f"Content-Type: {content_type or 'unknown'}",
    ]
    if title:
        lines.append(f"Title: {title}")
    lines.append(f"Content:\n{trimmed or 'No readable text content found.'}")
    return "\n".join(lines)


def _url_fetch_headers(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    return {
        "User-Agent": _BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": origin,
    }


def _is_url_query(value: str) -> bool:
    normalized = value.strip()
    if " " in normalized or "\\" in normalized:
        return False
    parsed = urlparse(normalized if "://" in normalized else f"https://{normalized}")
    return bool(parsed.scheme in {"http", "https"} and parsed.netloc and "." in parsed.netloc)


def _extract_urls(value: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in _URL_PATTERN.finditer(value):
        url = match.group(0).rstrip(".,;，。；")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    if not urls and _is_url_query(value):
        urls.append(_normalize_url_query(value))
    return urls


def _normalize_url_query(value: str) -> str:
    normalized = value.strip()
    if "://" not in normalized:
        normalized = f"https://{normalized}"
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolException(f"Invalid URL: {value}")
    return normalized


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


