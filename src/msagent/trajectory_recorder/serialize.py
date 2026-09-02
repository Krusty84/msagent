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

"""JSON-safe serialization helpers for trajectory events.

Messages are serialized with ``langchain_core.messages.message_to_dict`` so the
result is complete (content blocks, tool_calls, usage_metadata,
response_metadata, additional_kwargs) and reversible via ``messages_from_dict``.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Pattern

logger = logging.getLogger(__name__)

_MAX_DEPTH = 24


def json_safe(value: Any, _depth: int = 0) -> Any:
    """Convert an arbitrary value into JSON-serializable primitives."""
    if _depth > _MAX_DEPTH:
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return json_safe(value.value, _depth + 1)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(key): json_safe(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item, _depth + 1) for item in value]
    return str(value)


def serialize_message(message: Any) -> dict[str, Any]:
    """Serialize one langchain message with full fidelity."""
    try:
        from langchain_core.messages import message_to_dict

        return json_safe(message_to_dict(message))
    except Exception:
        logger.debug("Falling back to generic message serialization", exc_info=True)
        return {
            "type": str(getattr(message, "type", type(message).__name__)),
            "data": {
                "id": getattr(message, "id", None),
                "content": json_safe(getattr(message, "content", repr(message))),
            },
        }


def serialize_messages(messages: Iterable[Any]) -> list[dict[str, Any]]:
    return [serialize_message(message) for message in messages]


def compile_redaction_patterns(patterns: Iterable[str]) -> list[Pattern[str]]:
    """Compile redaction regexes, skipping invalid ones with a warning."""
    compiled: list[Pattern[str]] = []
    for raw in patterns:
        try:
            compiled.append(re.compile(raw))
        except re.error:
            logger.warning("Skipping invalid trajectory redaction pattern: %r", raw)
    return compiled


def sanitize(
    value: Any,
    *,
    max_field_chars: int,
    patterns: list[Pattern[str]],
    replacement: str,
    _depth: int = 0,
) -> Any:
    """Apply redaction and optional per-field truncation to a JSON-safe tree."""
    if _depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        result = value
        for pattern in patterns:
            result = pattern.sub(replacement, result)
        if 0 < max_field_chars < len(result):
            result = f"{result[:max_field_chars]}... [truncated, original length: {len(result)}]"
        return result
    if isinstance(value, dict):
        return {
            key: sanitize(
                item,
                max_field_chars=max_field_chars,
                patterns=patterns,
                replacement=replacement,
                _depth=_depth + 1,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            sanitize(
                item,
                max_field_chars=max_field_chars,
                patterns=patterns,
                replacement=replacement,
                _depth=_depth + 1,
            )
            for item in value
        ]
    return value