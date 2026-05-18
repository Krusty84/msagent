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

import json
import os
import re
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
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
_DEFAULT_FETCH_MAX_CHARS = 8000
_MAX_FETCH_CHARS = 20000
_USER_AGENT = "msagent/0.1 web-search"
_TAVILY_API_KEY_ENV = "TAVILY_API_KEY"
_WEB_SEARCH_DEBUG_LOG_ENV = "WEB_SEARCH_DEBUG_LOG"


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
    url: str = Field(description="HTTP or HTTPS URL to fetch")
    max_chars: int = Field(
        default=_DEFAULT_FETCH_MAX_CHARS,
        ge=1000,
        le=_MAX_FETCH_CHARS,
        description="Maximum number of extracted page text characters to return",
    )

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        normalized = value.strip()
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an absolute HTTP or HTTPS URL")
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
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag_name = tag.lower()
        if tag_name in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        elif tag_name == "title":
            self._in_title = True
        elif tag_name in {"p", "br", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.lower()
        if tag_name in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag_name == "title":
            self._in_title = False
        elif tag_name in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = _clean_text(data)
        if not text:
            return
        if self._in_title:
            self.title = _clean_text(f"{self.title} {text}")
        else:
            self.text_parts.append(text)


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

    debug_event: dict[str, Any] = {
        "tool": "web_search",
        "query": query,
        "allowed_domains": allowed_domains,
        "blocked_domains": blocked_domains,
        "limit": limit,
    }
    try:
        payload = WebSearchInput(
            query=query,
            allowed_domains=list(allowed_domains or []),
            blocked_domains=list(blocked_domains or []),
            limit=limit,
        )
    except ValueError as exc:
        debug_event["error"] = str(exc)
        _write_web_search_debug_event(debug_event)
        raise ToolException(str(exc)) from exc

    allowed_domain_set = set(payload.allowed_domains)
    blocked_domain_set = set(payload.blocked_domains)

    try:
        results, provider = await _search_results_with_provider(
            query=payload.query,
            allowed_domains=allowed_domain_set,
            blocked_domains=blocked_domain_set,
            limit=payload.limit,
        )
        debug_event["provider"] = provider
        debug_event["result_count"] = len(results)
        debug_event["results"] = results[: payload.limit]

        if not results:
            filters = []
            if payload.allowed_domains:
                filters.append(f"allowed={','.join(payload.allowed_domains)}")
            if payload.blocked_domains:
                filters.append(f"blocked={','.join(payload.blocked_domains)}")
            suffix = f" ({'; '.join(filters)})" if filters else ""
            output = f"No web results found for query: {payload.query}{suffix}"
        else:
            lines = [f"Web search results for: {payload.query}", f"Provider: {provider}"]
            for index, result in enumerate(results[: payload.limit], start=1):
                lines.append(f"{index}. {result['title']}\n   URL: {result['url']}")
            output = "\n".join(lines)

        debug_event["output"] = output
        return output
    except Exception as exc:
        debug_event["error"] = repr(exc)
        raise
    finally:
        _write_web_search_debug_event(debug_event)


@tool("web_fetch", args_schema=WebFetchInput)
async def web_fetch(
    *,
    url: str,
    max_chars: int = _DEFAULT_FETCH_MAX_CHARS,
    runtime: Any = None,
) -> str:
    """Fetch a web page URL and return cleaned, length-limited page text."""
    del runtime

    debug_event: dict[str, Any] = {
        "tool": "web_fetch",
        "url": url,
        "max_chars": max_chars,
    }
    try:
        payload = WebFetchInput(url=url, max_chars=max_chars)
    except ValueError as exc:
        debug_event["error"] = str(exc)
        _write_web_search_debug_event(debug_event)
        raise ToolException(str(exc)) from exc

    try:
        title, content, original_chars, truncated = await _fetch_page_text(payload.url, payload.max_chars)
        debug_event["title"] = title
        debug_event["content_chars"] = len(content)
        debug_event["original_chars"] = original_chars
        debug_event["truncated"] = truncated
        debug_event["content"] = content

        lines = [f"Web page content for: {title or payload.url}", f"URL: {payload.url}"]
        if truncated:
            lines.append(f"Content: (truncated to {payload.max_chars} chars)")
        else:
            lines.append("Content:")
        lines.append(content or "No readable page text found.")
        output = "\n".join(lines)
        debug_event["output"] = output
        return output
    except Exception as exc:
        debug_event["error"] = repr(exc)
        raise
    finally:
        _write_web_search_debug_event(debug_event)


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


async def _fetch_page_text(url: str, max_chars: int) -> tuple[str, str, int, bool]:
    try:
        async with httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolException(f"Web fetch request failed: {exc}") from exc

    content_type = response.headers.get("content-type", "")
    if content_type and "text/html" not in content_type and "text/plain" not in content_type:
        raise ToolException(f"Web fetch only supports text/html or text/plain content, got: {content_type}")

    if "text/plain" in content_type:
        title = ""
        text = _clean_multiline_text(response.text)
    else:
        title, text = _extract_readable_text(response.text)

    original_chars = len(text)
    truncated_text = _truncate_text(text, max_chars)
    return title, truncated_text, original_chars, len(truncated_text) < original_chars


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


def _extract_readable_text(html: str) -> tuple[str, str]:
    parser = _ReadableHTMLParser()
    parser.feed(html)
    text = _clean_multiline_text(" ".join(parser.text_parts))
    return parser.title, text


def _truncate_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."


def _write_web_search_debug_event(event: dict[str, Any]) -> None:
    log_path = os.getenv(_WEB_SEARCH_DEBUG_LOG_ENV, "").strip()
    if not log_path:
        return

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return
