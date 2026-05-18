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
import inspect
import re
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
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
_WEB_FETCH_DEBUG_DIR_ENV = "WEB_FETCH_DEBUG_DIR"
_DEFAULT_FETCH_EXTRACT_PROMPT = (
    "Extract the most relevant factual content from this fetched Markdown page. "
    "Keep only information that helps answer the user's web research request. "
    "Preserve exact commands, parameters, versions, URLs, tables, code blocks, and source-specific facts. "
    "Remove navigation, ads, boilerplate, comments, unrelated links, and duplicated content. "
    "Return concise Markdown."
)


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
    extract_mode: Literal["auto", "text", "markdown"] = Field(
        default="auto",
        description="Extraction format: auto returns Markdown for HTML pages, text returns plain text, markdown preserves simple headings and links",
    )
    max_chars: int = Field(
        default=_DEFAULT_FETCH_MAX_CHARS,
        ge=1000,
        le=_MAX_FETCH_CHARS,
        description="Maximum number of extracted page text characters to return",
    )
    selector: str | None = Field(
        default=None,
        description="Optional simple selector to limit extraction to matching blocks. Supports tag, .class, #id, or comma-separated values.",
    )
    query: str | None = Field(
        default=None,
        description="Optional keyword query; only matching extracted paragraphs/blocks are returned.",
    )
    extract_prompt: str | None = Field(
        default=None,
        description="Optional prompt used by a lightweight extractor/subagent. If omitted, web_fetch auto-extracts concise relevant Markdown.",
    )

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        normalized = value.strip()
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an absolute HTTP or HTTPS URL")
        return normalized

    @field_validator("selector", "query", "extract_prompt")
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


class _ReadableHTMLParser(HTMLParser):
    _BLOCK_TAGS = {"article", "section", "main", "div", "p", "li", "td", "th", "tr", "blockquote", "pre"} | {
        f"h{level}" for level in range(1, 7)
    }

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.blocks: list[dict[str, Any]] = []
        self._skip_depth = 0
        self._in_title = False
        self._block_stack: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        if tag_name in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        elif tag_name == "title":
            self._in_title = True
        elif tag_name in self._BLOCK_TAGS:
            ancestors = [
                {"tag": block.get("tag"), "attrs": block.get("attrs") or {}}
                for block in self._block_stack
            ]
            self._block_stack.append({"tag": tag_name, "attrs": dict(attrs), "ancestors": ancestors, "parts": []})
        elif tag_name == "br" and self._block_stack:
            self._block_stack[-1]["parts"].append("\n")
        elif tag_name == "a" and self._block_stack:
            href = dict(attrs).get("href") or ""
            self._block_stack[-1]["parts"].append({"link_href": href, "text": ""})

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.lower()
        if tag_name in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag_name == "title":
            self._in_title = False
        elif tag_name == "a" and self._block_stack:
            parts = self._block_stack[-1]["parts"]
            if parts and isinstance(parts[-1], dict) and "link_href" in parts[-1]:
                link = parts.pop()
                text = _clean_text(str(link.get("text") or ""))
                href = str(link.get("link_href") or "").strip()
                parts.append(f"[{text}]({href})" if text and href else text)
        elif tag_name in self._BLOCK_TAGS and self._block_stack:
            block = self._block_stack.pop()
            if block["tag"] != tag_name:
                return
            text = _clean_multiline_text(" ".join(str(part) for part in block["parts"]))
            if text:
                self.blocks.append(
                    {
                        "tag": tag_name,
                        "attrs": block["attrs"],
                        "ancestors": block.get("ancestors") or [],
                        "text": text,
                    }
                )

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = _clean_text(data)
        if not text:
            return
        if self._in_title:
            self.title = _clean_text(f"{self.title} {text}")
        elif self._block_stack:
            parts = self._block_stack[-1]["parts"]
            if parts and isinstance(parts[-1], dict) and "link_href" in parts[-1]:
                parts[-1]["text"] = f"{parts[-1].get('text', '')} {text}".strip()
            else:
                parts.append(text)
        else:
            self.blocks.append({"tag": "text", "attrs": {}, "text": text})


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
    extract_mode: Literal["auto", "text", "markdown"] = "auto",
    max_chars: int = _DEFAULT_FETCH_MAX_CHARS,
    selector: str | None = None,
    query: str | None = None,
    extract_prompt: str | None = None,
    runtime: Any = None,
) -> str:
    """Fetch a web page URL and return cleaned, length-limited Markdown content."""

    try:
        payload = WebFetchInput(
            url=url,
            extract_mode=extract_mode,
            max_chars=max_chars,
            selector=selector,
            query=query,
            extract_prompt=extract_prompt,
        )
    except ValueError as exc:
        raise ToolException(str(exc)) from exc

    title, content, truncated = await _fetch_page_text(
        payload.url,
        max_chars=payload.max_chars,
        extract_mode="markdown",
        selector=payload.selector,
        query=payload.query,
    )
    extraction_prompt = _resolve_extract_prompt(payload.extract_prompt, payload.query)
    extracted = await _extract_with_subagent(
        markdown=content,
        prompt=extraction_prompt,
        url=payload.url,
        title=title,
        max_chars=payload.max_chars,
        runtime=runtime,
    )
    if extracted is not None:
        content = extracted
        truncated = len(content) > payload.max_chars
        content = _truncate_text(content, payload.max_chars)

    lines = [f"Web page content for: {title or payload.url}", f"URL: {payload.url}"]
    lines.append(f"Content: (truncated to {payload.max_chars} chars)" if truncated else "Content:")
    lines.append(content or "No readable page text found.")
    return "\n".join(lines)


def _resolve_extract_prompt(extract_prompt: str | None, query: str | None) -> str:
    if extract_prompt:
        return extract_prompt
    if query:
        return (
            "Extract only the Markdown passages that are directly relevant to this query. "
            "Preserve exact facts, commands, parameters, versions, URLs, and tables. "
            "Remove unrelated boilerplate. Query: "
            f"{query}"
        )
    return _DEFAULT_FETCH_EXTRACT_PROMPT


async def _extract_with_subagent(
    *,
    markdown: str,
    prompt: str,
    url: str,
    title: str,
    max_chars: int,
    runtime: Any,
) -> str | None:
    extractor = _resolve_web_fetch_extractor(runtime)
    if extractor is None:
        return None

    try:
        result = extractor(
            prompt=prompt,
            markdown=markdown,
            url=url,
            title=title,
            max_chars=max_chars,
        )
    except TypeError:
        result = extractor(prompt, markdown)

    if inspect.isawaitable(result):
        result = await result
    return _clean_multiline_text(_message_content_to_text(result))


def _resolve_web_fetch_extractor(runtime: Any) -> Any:
    context = getattr(runtime, "context", None)
    if isinstance(context, dict):
        extractor = context.get("web_fetch_extractor")
    else:
        extractor = getattr(context, "web_fetch_extractor", None)
    if extractor is None:
        extractor = getattr(runtime, "web_fetch_extractor", None)
    return extractor if callable(extractor) else None


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content or "")


def _write_web_fetch_debug_files(*, url: str, html: str, markdown: str) -> None:
    output_dir = os.getenv(_WEB_FETCH_DEBUG_DIR_ENV, "").strip()
    if not output_dir:
        return

    try:
        path = Path(output_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        slug = _debug_filename_slug(url)
        (path / f"{timestamp}_{slug}.html").write_text(html, encoding="utf-8", errors="replace")
        (path / f"{timestamp}_{slug}.md").write_text(markdown, encoding="utf-8", errors="replace")
    except OSError:
        return


def _debug_filename_slug(url: str) -> str:
    parsed = urlparse(url)
    raw = f"{parsed.netloc}{parsed.path}".strip("/") or "web_fetch"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-")
    return (slug or "web_fetch")[:80]


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
    extract_mode: Literal["auto", "text", "markdown"] = "auto",
    selector: str | None = None,
    query: str | None = None,
) -> tuple[str, str, bool]:
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
        if query:
            text = _filter_plain_text(text, query)
    else:
        title, text = _extract_readable_text(
            response.text,
            extract_mode=extract_mode,
            selector=selector,
            query=query,
        )
        _write_web_fetch_debug_files(url=url, html=response.text, markdown=text)

    truncated = len(text) > max_chars
    truncated_text = _truncate_text(text, max_chars)
    return title, truncated_text, truncated


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


def _extract_readable_text(
    html: str,
    extract_mode: Literal["auto", "text", "markdown"] = "auto",
    selector: str | None = None,
    query: str | None = None,
) -> tuple[str, str]:
    parser = _ReadableHTMLParser()
    parser.feed(html)
    blocks = _filter_blocks(parser.blocks, selector=selector, query=query)
    return parser.title, _render_blocks(blocks, markdown=extract_mode != "text")


def _filter_blocks(
    blocks: list[dict[str, Any]],
    *,
    selector: str | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    filtered = [block for block in blocks if _selector_matches(block, selector)] if selector else list(blocks)
    if query:
        filtered = [block for block in filtered if _query_matches(str(block.get("text") or ""), query)]
    return filtered


def _selector_matches(block: dict[str, Any], selector: str | None) -> bool:
    if not selector:
        return True
    candidates = [{"tag": block.get("tag"), "attrs": block.get("attrs") or {}}]
    candidates.extend(block.get("ancestors") or [])

    for raw_part in selector.split(","):
        part = raw_part.strip().lower()
        if not part:
            continue
        for candidate in candidates:
            tag = str(candidate.get("tag") or "").lower()
            attrs = candidate.get("attrs") or {}
            class_names = set(str(attrs.get("class") or "").lower().split())
            element_id = str(attrs.get("id") or "").lower()
            if part.startswith(".") and part[1:] in class_names:
                return True
            if part.startswith("#") and part[1:] == element_id:
                return True
            if part == tag:
                return True
    return False


def _query_matches(text: str, query: str) -> bool:
    terms = [_clean_text(term).lower() for term in re.split(r"[\s,;]+", query) if _clean_text(term)]
    haystack = text.lower()
    return bool(terms) and all(term in haystack for term in terms)


def _filter_plain_text(text: str, query: str) -> str:
    return _clean_multiline_text(
        "\n\n".join(block for block in re.split(r"\n\s*\n", text) if block.strip() and _query_matches(block, query))
    )


def _render_blocks(blocks: list[dict[str, Any]], *, markdown: bool) -> str:
    lines: list[str] = []
    for block in blocks:
        tag = str(block.get("tag") or "")
        text = _clean_multiline_text(str(block.get("text") or ""))
        if not text:
            continue
        if not markdown:
            lines.append(re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text))
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(tag[1])
            lines.append(f"{'#' * level} {text}")
        elif tag == "li":
            lines.append(f"- {text}")
        else:
            lines.append(text)
    return _clean_multiline_text("\n\n".join(lines))


def _truncate_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."
