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

"""Parse and validate MSAGENT_IO v1 blocks from task delegation payloads."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_NAME = "msagent.subagent_io"
PROTOCOL_VERSION = "1"

_MSAGENT_IO_FENCE = re.compile(
    r"```msagent-io\s+v1\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


@dataclass(slots=True)
class ProtocolParseResult:
    """Outcome of parsing one MSAGENT_IO block from free text."""

    parsed: bool
    valid: bool
    payload: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    input_data: dict[str, Any] | None = None
    output_data: dict[str, Any] | None = None
    error_data: dict[str, Any] | None = None
    io_status: str | None = None


def extract_msagent_io_payload(text: str | None) -> dict[str, Any] | None:
    """Return the JSON object inside the first ``msagent-io v1`` fence, if any."""
    if not text or not text.strip():
        return None

    match = _MSAGENT_IO_FENCE.search(text)
    if not match:
        return None

    try:
        payload = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return None

    return payload if isinstance(payload, dict) else None


def parse_delegation_input(
    text: str | None,
    *,
    expected_subagent_type: str,
) -> ProtocolParseResult:
    """Parse and validate the input block from a task ``description``."""
    payload = extract_msagent_io_payload(text)
    if payload is None:
        return ProtocolParseResult(parsed=False, valid=False, errors=["missing_msagent_io_block"])

    errors = _validate_envelope(payload, expected_subagent_type=expected_subagent_type)
    input_data = payload.get("input")
    if not isinstance(input_data, dict):
        errors.append("missing_input_object")
    elif not input_data:
        errors.append("empty_input_object")
    else:
        errors.extend(_validate_delegation_input(expected_subagent_type, input_data))

    return ProtocolParseResult(
        parsed=True,
        valid=not errors,
        payload=payload,
        errors=errors,
        input_data=input_data if isinstance(input_data, dict) else None,
    )


def parse_completion_output(
    text: str | None,
    *,
    expected_subagent_type: str,
) -> ProtocolParseResult:
    """Parse and validate the output block from a task ``ToolMessage``."""
    payload = extract_msagent_io_payload(text)
    if payload is None:
        return ProtocolParseResult(parsed=False, valid=False, errors=["missing_msagent_io_block"])

    errors = _validate_envelope(payload, expected_subagent_type=expected_subagent_type)
    io_status = _coerce_str(payload.get("status"))
    if io_status not in {"ok", "failed"}:
        errors.append("invalid_status")

    output_data = payload.get("output")
    error_data = payload.get("error")
    if io_status == "ok":
        if not isinstance(output_data, dict):
            errors.append("missing_output_object")
    elif io_status == "failed":
        if not isinstance(error_data, dict):
            errors.append("missing_error_object")

    return ProtocolParseResult(
        parsed=True,
        valid=not errors,
        payload=payload,
        errors=errors,
        output_data=output_data if isinstance(output_data, dict) else None,
        error_data=error_data if isinstance(error_data, dict) else None,
        io_status=io_status,
    )


def _validate_envelope(payload: dict[str, Any], *, expected_subagent_type: str) -> list[str]:
    errors: list[str] = []
    if payload.get("protocol") != PROTOCOL_NAME:
        errors.append("invalid_protocol")
    if str(payload.get("version") or "") != PROTOCOL_VERSION:
        errors.append("invalid_version")

    block_type = _coerce_str(payload.get("subagent_type"))
    if not block_type:
        errors.append("missing_subagent_type")
    elif block_type != expected_subagent_type:
        errors.append("subagent_type_mismatch")

    return errors


def _validate_delegation_input(subagent_type: str, input_data: dict[str, Any]) -> list[str]:
    if subagent_type == "quant-tuning-evaluation-generator":
        return _validate_evaluation_generator_input(input_data)
    return []


def _validate_evaluation_generator_input(input_data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    deprecated = {"target_datasets", "accuracy_targets", "accuracy_tolerance"} & input_data.keys()
    if deprecated:
        errors.append("deprecated_fields:" + ",".join(sorted(deprecated)))

    datasets = input_data.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        errors.append("missing_datasets")
        return errors

    for index, item in enumerate(datasets):
        if not isinstance(item, dict):
            errors.append(f"datasets[{index}]_invalid")
            continue
        if not _coerce_str(item.get("name")):
            errors.append(f"datasets[{index}]_missing_name")
        if item.get("target") is None:
            errors.append(f"datasets[{index}]_missing_target")
        if not _coerce_str(item.get("config_name")):
            errors.append(f"datasets[{index}]_missing_config_name")

    return errors


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
