#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bootstrap and CLI helpers for msmodelslim skill scripts."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any


def ensure_msmodelslim() -> None:
    import msmodelslim  # noqa: F401 — trigger Ascend / package patches


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def emit_result(result: dict[str, Any]) -> int:
    print(json.dumps(result, ensure_ascii=False, default=_json_default))
    if result.get("ok") is False:
        return 1
    if result.get("valid") is False:
        return 1
    return 0


def parse_optional_json(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def parse_int_list(value: str | None) -> list[int] | None:
    if value is None or value == "":
        return None
    stripped = value.strip()
    if stripped.startswith("["):
        parsed = json.loads(stripped)
        if not isinstance(parsed, list):
            raise ValueError("device_indices must be a JSON array")
        return [int(x) for x in parsed]
    return [int(part.strip()) for part in stripped.split(",") if part.strip()]
