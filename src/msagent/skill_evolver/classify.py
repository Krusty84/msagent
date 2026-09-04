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

"""LLM classification of an evidence bundle into knowledge candidates.

The model receives the bundle built by :mod:`msagent.skill_evolver.bundle`
and answers with one JSON object (see the ``classify`` prompt). Everything
that makes the answer trustworthy happens here, in code: the reply is parsed
strictly (one corrective retry, then failure), and every candidate must cite
only seqs that were in the bundle — the model cannot reference what it has
not seen. Candidates failing that check are dropped with a warning, never
repaired.

The LLM is any object with ``async ainvoke(payload)`` that accepts a list of
``(role, text)`` pairs and returns an object exposing ``.text`` or
``.content`` (langchain chat models do, without being imported here).
Stdlib + pydantic only; this module never writes files.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, StrictInt, ValidationError, model_validator

logger = logging.getLogger(__name__)

# Placeholder of the prompt template that receives the bundle text.
BUNDLE_PLACEHOLDER = "{evidence_bundle}"
# Longest error text quoted back to the model in the corrective retry.
ERROR_TEXT_LIMIT = 2000
# Assistant turn replayed in the retry when the first reply was blank: some
# providers reject an empty assistant message.
EMPTY_REPLY = "(empty reply)"

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CORRECTION = (
    "Your previous reply could not be parsed as the required JSON object.\n"
    "Error: {error}\n\n"
    "Reply again with only the JSON object described in the instructions: "
    "no markdown fences, no prose, no comments, nothing before or after it."
)


class ClassifyParseError(ValueError):
    """The reply is not the JSON object the prompt requires."""


# ------------------------------------------------------------------- models


class CandidateTarget(BaseModel):
    """Where the knowledge belongs: a new skill or an existing one."""

    action: Literal["create", "update", "reference"]
    existing_skill: str | None = None

    @model_validator(mode="after")
    def require_existing_skill(self) -> CandidateTarget:
        """``update`` and ``reference`` point at a named library entry."""
        if self.action != "create" and not (self.existing_skill or "").strip():
            raise ValueError(f"target.action {self.action!r} requires existing_skill")
        return self


class Candidate(BaseModel):
    """One durable rule the model distilled from the bundle."""

    title: str
    rule: str
    # StrictInt: lax validation would turn true / "7" / 4.0 into ints that
    # could pass the evidence check by accident.
    evidence_refs: list[StrictInt]
    future_applicability: Literal["high", "medium", "low"]
    target: CandidateTarget


class ClassifyResult(BaseModel):
    """The model's verdict; ``candidates`` is empty when it is ``nothing``."""

    verdict: Literal["save", "nothing"]
    candidates: list[Candidate]


# ------------------------------------------------------------------ parsing


def strip_think_blocks(text: str) -> str:
    """Remove ``<think>…</think>`` reasoning blocks some models emit."""
    return _THINK_BLOCK.sub("", text)


def strip_code_fence(text: str) -> str:
    """Unwrap the whole text if the model fenced it despite instructions."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            return stripped[first_newline + 1 : -3].strip()
    return stripped


def parse_classify_reply(raw: str) -> ClassifyResult:
    """Parse one model reply strictly; raise ClassifyParseError otherwise."""
    text = strip_code_fence(strip_think_blocks(raw))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClassifyParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        kind = type(data).__name__
        raise ClassifyParseError(f"top-level JSON value must be an object, got {kind}")
    try:
        return ClassifyResult.model_validate(data)
    except ValidationError as exc:
        raise ClassifyParseError(f"schema violation: {exc}") from exc


def reply_text(response: Any) -> str:
    """Text of an ``ainvoke`` result: ``.text``, else ``.content``, else str."""
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(response, "content", None)
    if content is not None:
        return content if isinstance(content, str) else str(content)
    return str(response)


# ------------------------------------------------------------- verification


def _verify(result: ClassifyResult, valid_seq: set[int]) -> ClassifyResult:
    """Drop candidates whose evidence is empty or not in the bundle."""
    kept: list[Candidate] = []
    for candidate in result.candidates:
        refs = sorted(set(candidate.evidence_refs))
        if not refs:
            logger.warning(
                "classify: dropped candidate %r: empty evidence_refs",
                candidate.title,
            )
            continue
        unknown = [seq for seq in refs if seq not in valid_seq]
        if unknown:
            logger.warning(
                "classify: dropped candidate %r: evidence seq not in bundle: %s",
                candidate.title,
                unknown,
            )
            continue
        kept.append(candidate.model_copy(update={"evidence_refs": refs}))
    verdict = result.verdict if kept else "nothing"
    if verdict == "nothing" and kept:
        logger.info(
            "classify: verdict 'nothing' returned with %d verifiable candidates",
            len(kept),
        )
    return ClassifyResult(verdict=verdict, candidates=kept)


# --------------------------------------------------------------- public API


async def classify(
    bundle: str,
    valid_seq: set[int],
    llm: Any,
    template: str,
) -> ClassifyResult:
    """Ask the LLM to classify ``bundle``; keep only verifiable candidates.

    ``valid_seq`` is the set :func:`bundle.build_evidence_bundle` returned for
    this text. A candidate whose ``evidence_refs`` is empty, or cites any seq
    outside ``valid_seq``, is dropped with a warning; when nothing survives
    the verdict is ``nothing``.

    Raises ``ValueError`` when the bundle is blank, when ``valid_seq`` is
    empty for a non-blank bundle, when the template has no
    ``{evidence_bundle}`` placeholder, and when the reply is still not the
    required JSON after one corrective retry.
    """
    if not bundle.strip():
        raise ValueError("classify: the evidence bundle is empty")
    if not valid_seq:
        raise ValueError("classify: valid_seq is empty for a non-empty bundle")
    if BUNDLE_PLACEHOLDER not in template:
        raise ValueError(f"classify: template has no {BUNDLE_PLACEHOLDER} placeholder")

    # str.replace, never str.format: braces inside the bundle must stay inert.
    payload = [("human", template.replace(BUNDLE_PLACEHOLDER, bundle))]
    raw = reply_text(await llm.ainvoke(payload))
    try:
        result = parse_classify_reply(raw)
    except ClassifyParseError as first:
        logger.warning("classify: unparseable reply, retrying once: %s", first)
        payload = [
            *payload,
            ("ai", strip_think_blocks(raw).strip() or EMPTY_REPLY),
            ("human", _CORRECTION.format(error=str(first)[:ERROR_TEXT_LIMIT])),
        ]
        raw = reply_text(await llm.ainvoke(payload))
        try:
            result = parse_classify_reply(raw)
        except ClassifyParseError as second:
            message = f"classify: reply is not valid JSON after one retry: {second}"
            raise ValueError(message) from second
    return _verify(result, valid_seq)
