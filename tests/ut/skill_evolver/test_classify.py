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

"""Tests for the JSON classify stage on a scripted fake LLM (no network)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from msagent.skill_evolver.classify import (
    BUNDLE_PLACEHOLDER,
    EMPTY_REPLY,
    ClassifyParseError,
    ClassifyResult,
    classify,
    parse_classify_reply,
    strip_code_fence,
    strip_think_blocks,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"
PROMPT_PATH = (
    REPO_ROOT / "resources" / "configs" / "default" / "skill-evolver" / "prompts" / "classify" / "prompt_v1.md"
)
LOGGER = "msagent.skill_evolver.classify"

TEMPLATE = "Classify the evidence.\n\n{evidence_bundle}\n\nReply with JSON only."
BUNDLE = (
    "### Episode E1 — error_recovery (weight 0.60, thread thread-t)\n"
    "Evidence: seq 4, 5, 8\n"
    "Excerpts:\n"
    '- seq 4 tool.start bash: {"cmd": "make"}'
)
VALID_SEQ = {4, 5, 8}


class _FakeLLM:
    """Records every payload and answers with the scripted replies in order.

    A ``str`` reply is wrapped into an ``AIMessage`` unless ``raw`` is set;
    any other object is returned as is.
    """

    def __init__(self, *replies: Any, raw: bool = False) -> None:
        self.replies = list(replies)
        self.raw = raw
        self.payloads: list[list[tuple[str, str]]] = []

    async def ainvoke(self, payload: list[tuple[str, str]]) -> Any:
        self.payloads.append(list(payload))
        assert self.replies, "fake LLM called more often than scripted"
        reply = self.replies.pop(0)
        if isinstance(reply, str) and not self.raw:
            return AIMessage(content=reply)
        return reply


# ------------------------------------------------------------------ builders


def _candidate(**overrides: Any) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "title": "Build before test",
        "rule": "Run make before invoking the test suite.",
        "evidence_refs": [4, 5],
        "future_applicability": "high",
        "target": {"action": "create", "existing_skill": None},
    }
    candidate.update(overrides)
    return candidate


def _reply(*candidates: dict[str, Any], verdict: str = "save") -> str:
    return json.dumps({"verdict": verdict, "candidates": list(candidates)})


# ---------------------------------------------------------------- happy path


@pytest.mark.asyncio
async def test_valid_json_single_call() -> None:
    llm = _FakeLLM(_reply(_candidate(evidence_refs=[8, 4, 4])))

    result = await classify(BUNDLE, VALID_SEQ, llm, TEMPLATE)

    assert llm.payloads == [[("human", TEMPLATE.replace(BUNDLE_PLACEHOLDER, BUNDLE))]]
    assert result.verdict == "save"
    (candidate,) = result.candidates
    assert candidate.title == "Build before test"
    assert candidate.rule == "Run make before invoking the test suite."
    assert candidate.evidence_refs == [4, 8]  # deduplicated and sorted
    assert candidate.future_applicability == "high"
    assert candidate.target.action == "create"
    assert candidate.target.existing_skill is None


@pytest.mark.asyncio
async def test_update_target_keeps_existing_skill() -> None:
    target = {"action": "update", "existing_skill": "profiling-bottleneck"}
    llm = _FakeLLM(_reply(_candidate(target=target)))

    result = await classify(BUNDLE, VALID_SEQ, llm, TEMPLATE)

    (candidate,) = result.candidates
    assert candidate.target.action == "update"
    assert candidate.target.existing_skill == "profiling-bottleneck"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrap",
    [
        "```json\n{body}\n```",
        "```\n{body}\n```",
        "<think>\nlet me think {about} it\n</think>\n{body}",
        "<THINK>x</THINK>\n```json\n{body}```",
    ],
)
async def test_fences_and_think_blocks_are_stripped(wrap: str) -> None:
    llm = _FakeLLM(wrap.replace("{body}", _reply(_candidate())))

    result = await classify(BUNDLE, VALID_SEQ, llm, TEMPLATE)

    assert len(llm.payloads) == 1
    assert result.verdict == "save"
    assert [c.evidence_refs for c in result.candidates] == [[4, 5]]


@pytest.mark.asyncio
async def test_nothing_verdict_without_candidates() -> None:
    llm = _FakeLLM('{"verdict": "nothing", "candidates": []}')

    result = await classify(BUNDLE, VALID_SEQ, llm, TEMPLATE)

    assert result == ClassifyResult(verdict="nothing", candidates=[])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(content=_reply(_candidate())),
        SimpleNamespace(text=lambda: "method, not property", content=_reply(_candidate())),
        AIMessage(content=[{"type": "text", "text": _reply(_candidate())}]),
        _reply(_candidate()),
    ],
    ids=["content-only", "text-method", "content-blocks", "plain-str"],
)
async def test_reply_shapes_without_text_property(response: Any) -> None:
    llm = _FakeLLM(response, raw=True)

    result = await classify(BUNDLE, VALID_SEQ, llm, TEMPLATE)

    assert result.verdict == "save"
    assert len(result.candidates) == 1


# --------------------------------------------------------------------- retry


@pytest.mark.asyncio
async def test_invalid_json_then_valid_retries_once_with_error_text() -> None:
    llm = _FakeLLM("not json at all", _reply(_candidate()))

    result = await classify(BUNDLE, VALID_SEQ, llm, TEMPLATE)

    assert result.verdict == "save"
    assert len(llm.payloads) == 2
    first_prompt = llm.payloads[0][0]
    assert llm.payloads[1][0] == first_prompt
    assert llm.payloads[1][1] == ("ai", "not json at all")
    role, correction = llm.payloads[1][2]
    assert role == "human"
    assert "could not be parsed" in correction
    assert "invalid JSON" in correction
    assert "Expecting value" in correction  # the json module's own error text


@pytest.mark.asyncio
async def test_invalid_twice_raises_after_exactly_two_calls() -> None:
    llm = _FakeLLM("garbage", "still garbage")

    with pytest.raises(ValueError, match="after one retry"):
        await classify(BUNDLE, VALID_SEQ, llm, TEMPLATE)

    assert len(llm.payloads) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_reply", "fragment"),
    [
        ("[]", "must be an object"),
        ('"save"', "must be an object"),
        (_reply(_candidate(evidence_refs=[True, 4])), "int_type"),
        (_reply(_candidate(evidence_refs=["7"])), "int_type"),
        (_reply(_candidate(evidence_refs=[4.0])), "int_type"),
        (_reply(_candidate(future_applicability="certain")), "literal_error"),
        (_reply(_candidate(target={"action": "update"})), "requires existing_skill"),
        (
            _reply(_candidate(target={"action": "reference", "existing_skill": " "})),
            "requires existing_skill",
        ),
        (_reply(_candidate(), verdict="maybe"), "literal_error"),
        ('{"verdict": "nothing"}', "candidates"),
    ],
    ids=[
        "list",
        "string",
        "bool-ref",
        "str-ref",
        "float-ref",
        "bad-applicability",
        "update-without-skill",
        "reference-blank-skill",
        "bad-verdict",
        "missing-candidates",
    ],
)
async def test_schema_violations_go_through_the_retry(bad_reply: str, fragment: str) -> None:
    llm = _FakeLLM(bad_reply, _reply(_candidate()))

    result = await classify(BUNDLE, VALID_SEQ, llm, TEMPLATE)

    assert result.verdict == "save"
    assert len(llm.payloads) == 2
    assert fragment in llm.payloads[1][2][1]


@pytest.mark.asyncio
async def test_blank_first_reply_is_replayed_as_placeholder_turn() -> None:
    llm = _FakeLLM("  \n", _reply(_candidate()))

    result = await classify(BUNDLE, VALID_SEQ, llm, TEMPLATE)

    assert result.verdict == "save"
    assert llm.payloads[1][1] == ("ai", EMPTY_REPLY)


# -------------------------------------------------------------- verification


@pytest.mark.asyncio
async def test_invented_seq_drops_candidate_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm = _FakeLLM(
        _reply(
            _candidate(title="Grounded", evidence_refs=[4]),
            _candidate(title="Fabricated", evidence_refs=[4, 41, 99]),
        )
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = await classify(BUNDLE, VALID_SEQ, llm, TEMPLATE)

    assert result.verdict == "save"
    assert [c.title for c in result.candidates] == ["Grounded"]
    assert "dropped candidate 'Fabricated': evidence seq not in bundle: [41, 99]" in caplog.text
    assert "dropped candidate 'Grounded'" not in caplog.text


@pytest.mark.asyncio
async def test_empty_evidence_refs_drops_candidate_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm = _FakeLLM(_reply(_candidate(title="Hollow", evidence_refs=[]), _candidate(title="Solid")))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = await classify(BUNDLE, VALID_SEQ, llm, TEMPLATE)

    assert [c.title for c in result.candidates] == ["Solid"]
    assert "dropped candidate 'Hollow': empty evidence_refs" in caplog.text


@pytest.mark.asyncio
async def test_all_candidates_dropped_forces_nothing(caplog: pytest.LogCaptureFixture) -> None:
    llm = _FakeLLM(_reply(_candidate(evidence_refs=[]), _candidate(evidence_refs=[123])))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = await classify(BUNDLE, VALID_SEQ, llm, TEMPLATE)

    assert result == ClassifyResult(verdict="nothing", candidates=[])
    assert caplog.text.count("dropped candidate") == 2


@pytest.mark.asyncio
async def test_nothing_verdict_with_grounded_candidate_passes_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm = _FakeLLM(_reply(_candidate(), verdict="nothing"))

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await classify(BUNDLE, VALID_SEQ, llm, TEMPLATE)

    assert result.verdict == "nothing"
    assert len(result.candidates) == 1
    assert "verdict 'nothing' returned with 1 verifiable candidates" in caplog.text


# -------------------------------------------------------------------- guards


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bundle", "valid_seq", "template", "fragment"),
    [
        ("", VALID_SEQ, TEMPLATE, "bundle is empty"),
        ("  \n", VALID_SEQ, TEMPLATE, "bundle is empty"),
        (BUNDLE, set(), TEMPLATE, "valid_seq is empty"),
        (BUNDLE, VALID_SEQ, "no placeholder here", "placeholder"),
    ],
    ids=["empty-bundle", "blank-bundle", "empty-valid-seq", "no-placeholder"],
)
async def test_guards_raise_before_calling_the_llm(
    bundle: str, valid_seq: set[int], template: str, fragment: str
) -> None:
    llm = _FakeLLM(_reply(_candidate()))

    with pytest.raises(ValueError, match=fragment):
        await classify(bundle, valid_seq, llm, template)

    assert llm.payloads == []


# ------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('  {"a": 1}  ', '{"a": 1}'),
        ('```json\n{"a": 1}```', '{"a": 1}'),
        ('```{"a": 1}```', '```{"a": 1}```'),  # no newline after the fence: left alone
    ],
)
def test_strip_code_fence(text: str, expected: str) -> None:
    assert strip_code_fence(text) == expected


def test_strip_think_blocks() -> None:
    text = "<think>\nplan\n</think>\n{}\n<THINK>more</THINK>"
    assert strip_think_blocks(text).strip() == "{}"


def test_parse_classify_reply_errors() -> None:
    with pytest.raises(ClassifyParseError, match="invalid JSON"):
        parse_classify_reply("{")
    with pytest.raises(ClassifyParseError, match="must be an object, got list"):
        parse_classify_reply("[1]")
    with pytest.raises(ClassifyParseError, match="schema violation"):
        parse_classify_reply('{"verdict": "save"}')


# -------------------------------------------------------------------- prompt


def test_packaged_classify_prompt_contract() -> None:
    text = PROMPT_PATH.read_text(encoding="utf-8")

    assert len(text.splitlines()) <= 120
    assert BUNDLE_PLACEHOLDER in text
    assert "{skill_library}" in text
    for key in (
        '"verdict"',
        '"candidates"',
        '"title"',
        '"rule"',
        '"evidence_refs"',
        '"future_applicability"',
        '"target"',
        '"action"',
        '"existing_skill"',
        '"save"',
        '"nothing"',
        '"create"',
        '"update"',
        '"reference"',
        '"high"',
        '"medium"',
        '"low"',
    ):
        assert key in text, key
    for render_marker in (
        "# Required SKILL.md structure",
        "## Inputs",
        "## Workflow",
        "## Outputs",
        "frontmatter",
        "Nothing to save.",
    ):
        assert render_marker not in text, render_marker


# ----------------------------------------------------------------- isolation

_ISOLATION_PROBE = """
import sys
import msagent.skill_evolver.bundle as bundle
import msagent.skill_evolver.classify as classify
assert bundle.__file__.startswith(sys.argv[1]), bundle.__file__
assert classify.__file__.startswith(sys.argv[1]), classify.__file__
banned = ("langchain", "langgraph", "httpx", "requests", "urllib3", "aiohttp",
          "urllib.request", "http.client")
leaked = sorted(m for m in sys.modules if m.startswith(banned))
assert not leaked, leaked
"""


def test_bundle_and_classify_import_no_langchain_or_network() -> None:
    # Same setup as test_features: the pytest process already has langchain
    # loaded, so the check runs in a fresh interpreter with src/ first on the path.
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_PROBE, str(SRC_DIR)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
