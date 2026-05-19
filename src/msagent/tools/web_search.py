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

"""Built-in web search tool backed by Exa."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field, field_validator

_EXA_API_BASE_URL = "https://api.exa.ai"
_EXA_SEARCH_ENDPOINT = f"{_EXA_API_BASE_URL}/search"
_EXA_CONTENTS_ENDPOINT = f"{_EXA_API_BASE_URL}/contents"
_EXA_API_KEY_ENV = "EXA_API_KEY"
_EXA_PROVIDER_NAME = "Exa"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_SEARCH_DEADLINE_SECONDS = 60.0
_SEARCH_MAX_ATTEMPTS = 3
_DEFAULT_RESULT_LIMIT = 5
_MAX_RESULT_LIMIT = 10
_DEFAULT_URL_FETCH_MAX_CHARS = 8000
_USER_AGENT = "msagent/0.1 web-search"


@dataclass(frozen=True, slots=True)
class SearchFailure:
    """Explain why web search returned no usable results."""

    reason: str
    attempts: int = 0
    provider: str = _EXA_PROVIDER_NAME


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

    api_key = _require_exa_api_key()
    allowed_domain_set = set(payload.allowed_domains)
    blocked_domain_set = set(payload.blocked_domains)

    results, failure = await _run_search_with_deadline(
        api_key=api_key,
        query=payload.query,
        allowed_domains=allowed_domain_set,
        blocked_domains=blocked_domain_set,
        limit=payload.limit,
    )

    if not results:
        return _format_no_results_message(
            query=payload.query,
            failure=failure or SearchFailure(reason="unknown search failure"),
            allowed_domains=payload.allowed_domains,
            blocked_domains=payload.blocked_domains,
        )

    provider = failure.provider if failure is not None else _EXA_PROVIDER_NAME
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

    api_key = _require_exa_api_key()
    title, content, truncated = await _fetch_url_text(
        payload.url,
        api_key=api_key,
        max_chars=payload.max_chars,
    )
    if payload.query:
        content = _extract_relevant_snippets(content, payload.query, max_chars=payload.max_chars)
        truncated = False

    lines = [f"Web page content for: {title or payload.url}", f"URL: {payload.url}"]
    if payload.query:
        lines.append(f"Query: {payload.query}")
    lines.append(f"Content: (truncated to {payload.max_chars} chars)" if truncated else "Content:")
    lines.append(content or "No readable page text found.")
    return "\n".join(lines)


def _require_exa_api_key() -> str:
    api_key = str(os.environ.get(_EXA_API_KEY_ENV, "") or "").strip()
    if not api_key:
        raise ToolException(
            "EXA_API_KEY is required for built-in web_search/web_fetch when using the Exa backend."
        )
    return api_key


async def _run_search_with_deadline(
    *,
    api_key: str,
    query: str,
    allowed_domains: set[str],
    blocked_domains: set[str],
    limit: int,
) -> tuple[list[dict[str, str]], SearchFailure | None]:
    try:
        return await asyncio.wait_for(
            _search_result_urls(
                api_key=api_key,
                query=query,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                limit=limit,
            ),
            timeout=_SEARCH_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError:
        return [], SearchFailure(
            reason=f"search timed out after {_SEARCH_DEADLINE_SECONDS:.0f}s",
            attempts=_SEARCH_MAX_ATTEMPTS,
        )


async def _search_result_urls(
    *,
    api_key: str,
    query: str,
    allowed_domains: set[str],
    blocked_domains: set[str],
    limit: int,
) -> tuple[list[dict[str, str]], SearchFailure | None]:
    last_reason = "search returned no usable results"
    attempts = 0

    for attempts in range(1, _SEARCH_MAX_ATTEMPTS + 1):
        raw_results, fetch_error = await _try_fetch_exa_search_results(
            api_key=api_key,
            query=query,
            limit=limit,
        )
        if fetch_error is not None:
            last_reason = fetch_error
            continue
        if not raw_results:
            last_reason = "search provider returned no result entries"
            continue

        filtered = _filter_results(
            raw_results,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )
        if filtered:
            return filtered[:limit], SearchFailure(reason="search succeeded", attempts=attempts)

        last_reason = _build_filtered_out_reason(
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )

    return [], SearchFailure(reason=last_reason, attempts=attempts)


async def _try_fetch_exa_search_results(
    *,
    api_key: str,
    query: str,
    limit: int,
) -> tuple[list[dict[str, str]] | None, str | None]:
    try:
        results = await _fetch_exa_search_results(
            api_key=api_key,
            query=query,
            limit=limit,
        )
    except ToolException as exc:
        return None, str(exc)
    return results, None


async def _fetch_exa_search_results(
    *,
    api_key: str,
    query: str,
    limit: int,
) -> list[dict[str, str]]:
    payload = {
        "query": query,
        "numResults": limit,
        "type": "auto",
    }
    response_data = await _post_exa_json(
        api_key=api_key,
        url=_EXA_SEARCH_ENDPOINT,
        payload=payload,
        error_context="Exa web search request",
    )
    results = response_data.get("results", [])
    if not isinstance(results, list):
        return []

    normalized_results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "") or "").strip()
        title = str(item.get("title", "") or item.get("id", "") or url).strip()
        if not url or not _is_http_url(url):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        normalized_results.append(
            {
                "title": _clean_text(title) or url,
                "url": url,
            }
        )
    return normalized_results


async def _fetch_url_text(
    url: str,
    *,
    api_key: str,
    max_chars: int = _DEFAULT_URL_FETCH_MAX_CHARS,
) -> tuple[str, str, bool]:
    payload = {
        "urls": [url],
        "text": True,
        "highlights": False,
        "summary": False,
    }
    response_data = await _post_exa_json(
        api_key=api_key,
        url=_EXA_CONTENTS_ENDPOINT,
        payload=payload,
        error_context="Exa contents request",
    )
    results = response_data.get("results", [])
    if not isinstance(results, list) or not results:
        raise ToolException(f"Exa contents request returned no content for URL: {url}")

    item = results[0]
    if not isinstance(item, dict):
        raise ToolException(f"Exa contents request returned malformed content for URL: {url}")

    title = _clean_text(str(item.get("title", "") or ""))
    text = _clean_multiline_text(str(item.get("text", "") or ""))
    if not text:
        raise ToolException(f"Exa contents request returned empty text for URL: {url}")

    truncated = len(text) > max_chars
    return title, _truncate_text(text, max_chars), truncated


async def _post_exa_json(
    *,
    api_key: str,
    url: str,
    payload: dict[str, Any],
    error_context: str,
) -> dict[str, Any]:
    headers = {
        "x-api-key": api_key,
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers=headers,
        ) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise ToolException(f"{error_context} timed out: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        detail = _extract_error_detail(exc.response)
        raise ToolException(f"{error_context} failed: HTTP {exc.response.status_code} ({detail})") from exc
    except httpx.HTTPError as exc:
        raise ToolException(f"{error_context} failed: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise ToolException(f"{error_context} returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise ToolException(f"{error_context} returned unexpected response payload")
    return data


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text or "no response body"

    if isinstance(payload, dict):
        for key in ("error", "message", "detail"):
            value = payload.get(key)
            if value:
                return str(value)
    return str(payload)


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


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _clean_text(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value or "").strip()
    return normalized


def _clean_multiline_text(value: str) -> str:
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
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


def _build_filtered_out_reason(*, allowed_domains: set[str], blocked_domains: set[str]) -> str:
    filters: list[str] = []
    if allowed_domains:
        filters.append(f"no results matched allowed domains: {', '.join(sorted(allowed_domains))}")
    if blocked_domains:
        filters.append(f"results were excluded by blocked domains: {', '.join(sorted(blocked_domains))}")
    if filters:
        return "; ".join(filters)
    return "search provider returned results, but none were usable"


def _format_no_results_message(
    *,
    query: str,
    failure: SearchFailure,
    allowed_domains: list[str],
    blocked_domains: list[str],
) -> str:
    filters: list[str] = []
    if allowed_domains:
        filters.append(f"allowed={','.join(allowed_domains)}")
    if blocked_domains:
        filters.append(f"blocked={','.join(blocked_domains)}")

    details = [f"reason={failure.reason}"]
    if failure.attempts:
        details.append(f"attempts={failure.attempts}")
    details.extend(filters)
    return f"No web results found for query: {query} ({'; '.join(details)})"


async def _amain() -> None:
    target_url = "https://docs.vllm.ai/projects/vllm-ascend-cn/zh-cn/latest/tutorials/models/"
    search_query = "vllm ascend qwen3.5 35b a3b recommended config"
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
