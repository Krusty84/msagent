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
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field, field_validator

_BING_SEARCH_URL = "https://www.bing.com/search"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_RESULT_LIMIT = 5
_MAX_RESULT_LIMIT = 10
_DEFAULT_URL_FETCH_MAX_CHARS = 8000
_USER_AGENT = "msagent/0.1 web-search"
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class WebSearchInput(BaseModel):
    query: str = Field(description="Search keywords used to find matching web page URLs")
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
    url: str = Field(description="HTTP or HTTPS URL, or bare domain URL, to fetch")
    query: str | None = Field(
        default=None,
        description="Optional keywords used to extract relevant snippets from the fetched page content",
    )
    max_chars: int = Field(
        default=_DEFAULT_URL_FETCH_MAX_CHARS,
        ge=1000,
        le=20000,
        description="Maximum fetched page text characters to return",
    )

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        normalized = _normalize_direct_url(value)
        if normalized is None:
            raise ValueError("url must be an HTTP or HTTPS URL, or a bare domain URL")
        return normalized

    @field_validator("query")
    @classmethod
    def _normalize_optional_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class _BingHTMLParser(HTMLParser):
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
        if not _is_search_result_url(href):
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
        url = self._current.get("url", "").strip()
        if title and url:
            self.results.append({"title": title, "url": url})
        self._current = None
        self._in_title = False


class _ReadableHTMLParser(HTMLParser):
    _SKIPPED_TAGS = {"script", "style", "noscript", "svg", "canvas"}
    _BLOCK_TAGS = {
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.text_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if normalized in self._SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if normalized == "title":
            self._in_title = True
        if normalized in self._BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        self.text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in self._SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if normalized == "title":
            self._in_title = False
        if normalized in self._BLOCK_TAGS:
            self.text_parts.append("\n")


@tool("web_search", args_schema=WebSearchInput)
async def web_search(
    *,
    query: str,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    limit: int = _DEFAULT_RESULT_LIMIT,
    runtime: Any = None,
) -> str:
    """Search the web for URLs matching a keyword query."""
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

    results, provider = await _search_result_urls(
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

    lines = [f"Web search URL results for: {payload.query}", f"Provider: {provider}"]
    for index, result in enumerate(results[: payload.limit], start=1):
        lines.append(f"{index}. {result['title']}\n   URL: {result['url']}")
    return "\n".join(lines)


@tool("web_fetch", args_schema=WebFetchInput)
async def web_fetch(
    *,
    url: str,
    query: str | None = None,
    max_chars: int = _DEFAULT_URL_FETCH_MAX_CHARS,
    runtime: Any = None,
) -> str:
    """Fetch a URL and return page content, optionally narrowed to query-matching snippets."""
    del runtime

    try:
        payload = WebFetchInput(
            url=url,
            query=query,
            max_chars=max_chars,
        )
    except ValueError as exc:
        raise ToolException(str(exc)) from exc

    title, content, truncated = await _fetch_url_text(payload.url, max_chars=payload.max_chars)
    if payload.query:
        content = _extract_relevant_snippets(content, payload.query, max_chars=payload.max_chars)
        truncated = False

    lines = [f"Web page content for: {title or payload.url}", f"URL: {payload.url}"]
    if payload.query:
        lines.append(f"Query: {payload.query}")
    lines.append(f"Content: (truncated to {payload.max_chars} chars)" if truncated else "Content:")
    lines.append(content or "No readable page text found.")
    return "\n".join(lines)


async def _search_result_urls(
    *,
    query: str,
    allowed_domains: set[str],
    blocked_domains: set[str],
    limit: int,
) -> tuple[list[dict[str, str]], str]:
    html = await _fetch_bing_html(query)
    results = _extract_results(html)
    filtered = _filter_results(
        results,
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
    )
    return filtered, "Bing HTML"


async def _fetch_bing_html(query: str) -> str:
    try:
        async with httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers=_BROWSER_HEADERS,
        ) as client:
            response = await client.get(_BING_SEARCH_URL, params={"q": query})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolException(f"Bing web search request failed: {exc}") from exc
    return response.text


async def _fetch_url_text(url: str, *, max_chars: int = _DEFAULT_URL_FETCH_MAX_CHARS) -> tuple[str, str, bool]:
    try:
        async with httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers=_BROWSER_HEADERS,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolException(f"URL fetch request failed: {exc}") from exc

    title, text = _extract_page_text(response.text)
    truncated = len(text) > max_chars
    return title, _truncate_text(text, max_chars), truncated


def _extract_results(html: str) -> list[dict[str, str]]:
    parser = _BingHTMLParser()
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


def _is_search_result_url(url: str) -> bool:
    parsed = urlparse(unescape(url.strip()))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    hostname = (parsed.hostname or "").lower()
    blocked_hosts = {
        "bing.com",
        "www.bing.com",
        "go.microsoft.com",
        "login.live.com",
        "account.microsoft.com",
    }
    return hostname not in blocked_hosts and not hostname.endswith(".bing.com")


def _extract_page_text(html: str) -> tuple[str, str]:
    parser = _ReadableHTMLParser()
    parser.feed(html)
    title = _clean_text(parser.title)
    text = _clean_multiline_text(" ".join(parser.text_parts))
    return title, text


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


def _normalize_direct_url(value: str) -> str | None:
    candidate = value.strip()
    if not candidate or any(ch.isspace() for ch in candidate):
        return None

    if "://" in candidate:
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return candidate
        return None

    if candidate.startswith("www."):
        candidate = f"https://{candidate}"
    elif _looks_like_bare_domain_url(candidate):
        candidate = f"https://{candidate}"
    else:
        return None

    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return candidate
    return None


def _looks_like_bare_domain_url(value: str) -> bool:
    parsed = urlparse(f"https://{value}")
    hostname = parsed.hostname or ""
    if "." not in hostname:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", hostname))


def _normalize_result_url(url: str) -> str:
    return unescape(url.strip())


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


def _extract_relevant_snippets(content: str, query: str, *, max_chars: int) -> str:
    terms = [term.lower() for term in re.findall(r"[\w.-]+", query) if len(term) >= 2]
    if not terms:
        return _truncate_text(content, max_chars)

    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", content) if paragraph.strip()]
    matched = [
        paragraph
        for paragraph in paragraphs
        if any(term in paragraph.lower() for term in terms)
    ]
    if not matched:
        return _truncate_text(content, max_chars)
    return _truncate_text("\n\n---\n\n".join(matched), max_chars)


async def _amain() -> None:
    target_url = " https://docs.vllm.ai/projects/vllm-ascend-cn/zh-cn/latest/tutorials/models/"
    search_query = "vllm-ascend中qwen3.5-35B-A3B的推荐配置参数"
    fetch_query = "example"
    allowed_domains: list[str] = []
    blocked_domains: list[str] = []
    limit = _DEFAULT_RESULT_LIMIT

    search_result = await web_search.coroutine(
        query=search_query,
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
        limit=limit,
    )
    print("=== Search URL result ===")
    print(search_result)

    try:
        fetch_result = await web_fetch.coroutine(
            url=target_url,
            query=fetch_query,
        )
    except ToolException as exc:
        fetch_result = f"web_fetch failed: {exc}"
    print("\n=== Fetch URL content result ===")
    print(fetch_result)


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
