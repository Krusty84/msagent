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

"""Tests for skill_evolver.report: decision report location, name, atomic write, evidence sibling."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import msagent.cli.handlers  # noqa: F401
from msagent.cli.bootstrap.initializer import initializer
from msagent.skill_evolver import report as module
from msagent.skill_evolver.report import (
    REPORT_VERSION,
    decisions_dir,
    is_synthetic,
    write_report,
)
from msagent.skill_evolver.writer import batch_dir_name

THREAD = "thread/with spaces:and*stars"
NAME_RE = re.compile(r"^(?P<batch>[A-Za-z0-9][A-Za-z0-9._-]*)-(?P<utc>\d{8}T\d{6}\d{6}Z)(-\d+)?\.json$")
PAYLOAD = {
    "command": "skill-mine",
    "thread_id": THREAD,
    "gate": {"score": 0.0, "passes": False, "reason": "no_episodes"},
    "plans": {"proposals": 0},
    "proposals": [],
    "stop_message": "Nothing to save: no episodes detected",
    "llm": {"calls_used": 0},
    "failed": None,
}


def test_decisions_dir_is_private_under_the_project_state(tmp_path: Path) -> None:
    state = initializer.get_project_paths(tmp_path / "work").root

    path = decisions_dir(state)

    assert path == state / "skill-evolver" / "decisions"
    assert path.is_dir()
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o700
        assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert decisions_dir(state) == path  # idempotent


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory permissions")
def test_decisions_dir_is_best_effort_when_the_state_dir_is_read_only(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o500)
    try:
        path = decisions_dir(state)
    finally:
        state.chmod(0o700)

    assert path == state / "skill-evolver" / "decisions"
    assert not path.parent.exists()  # the failure is left to write_report, logged per thread


@pytest.mark.parametrize(
    ("agent", "working_dir", "expected"),
    [
        ("SyntheticDemo", "/home/u/proj", True),
        ("Profiler", "/synthetic/demo", True),
        ("Profiler", "/tmp/my-Synthetic-fixtures/proj", True),
        ("Profiler", "/home/u/proj", False),
        ("", "", False),
    ],
)
def test_is_synthetic_reads_agent_and_working_dir(agent: str, working_dir: str, expected: bool) -> None:
    assert is_synthetic(agent, working_dir) is expected


def test_report_written_with_zero_proposals_name_content_and_mode(tmp_path: Path) -> None:
    directory = decisions_dir(tmp_path / "state")

    path = write_report(directory, thread_id=THREAD, payload=PAYLOAD)

    assert path.parent == directory
    match = NAME_RE.match(path.name)
    assert match, path.name
    assert match.group("batch") == batch_dir_name(THREAD)
    assert datetime.strptime(match.group("utc"), "%Y%m%dT%H%M%S%fZ").year >= 2026
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o600

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["report_version"] == REPORT_VERSION == 1
    assert document["evidence_text_file"] is None
    assert document["gate"]["passes"] is False
    assert document["plans"]["proposals"] == 0
    assert document["stop_message"].startswith("Nothing to save")
    assert document["llm"]["calls_used"] == 0
    assert document["thread_id"] == THREAD
    assert list(document) == sorted(document)  # sort_keys
    assert path.read_text(encoding="utf-8").endswith("}\n")
    assert sorted(p.name for p in directory.iterdir()) == [path.name]
    assert PAYLOAD.get("report_version") is None  # the caller's mapping is not mutated


def test_report_saves_evidence_only_when_given(tmp_path: Path) -> None:
    directory = decisions_dir(tmp_path / "state")

    without = write_report(directory, thread_id="t1", payload=PAYLOAD)
    with_text = write_report(directory, thread_id="t2", payload=PAYLOAD, evidence_text="# Round 1\n\n### Episode E1\n")

    sibling = with_text.with_name(with_text.stem + ".evidence.md")
    assert [p.name for p in directory.glob("*.evidence.md")] == [sibling.name]
    assert json.loads(without.read_text(encoding="utf-8"))["evidence_text_file"] is None
    assert sibling.is_file()
    assert sibling.read_text(encoding="utf-8") == "# Round 1\n\n### Episode E1\n"
    assert "<think>" not in sibling.read_text(encoding="utf-8")
    assert json.loads(with_text.read_text(encoding="utf-8"))["evidence_text_file"] == sibling.name
    if sys.platform != "win32":
        assert (sibling.stat().st_mode & 0o777) == 0o600


def test_report_name_collision_gets_suffix_and_write_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = decisions_dir(tmp_path / "state")

    class _FrozenClock:
        @staticmethod
        def now(tz: object) -> datetime:
            return datetime(2026, 9, 8, 12, 30, 45, 123456, tzinfo=tz)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "datetime", _FrozenClock)
    stem = f"{batch_dir_name('thread-a')}-20260908T123045123456Z"

    first = write_report(directory, thread_id="thread-a", payload=PAYLOAD)
    second = write_report(directory, thread_id="thread-a", payload=PAYLOAD, evidence_text="e")
    third = write_report(directory, thread_id="thread-a", payload=PAYLOAD)

    assert first.name == f"{stem}.json"
    assert second.name == f"{stem}-2.json"
    assert third.name == f"{stem}-3.json"
    assert (directory / f"{stem}-2.evidence.md").is_file()
    assert json.loads(second.read_text(encoding="utf-8"))["evidence_text_file"] == f"{stem}-2.evidence.md"
    assert not list(directory.glob("*.tmp")) and not list(directory.glob(".*"))


def test_write_failure_leaves_no_temp_file_and_reserved_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = decisions_dir(tmp_path / "state")

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(module.os, "fsync", boom)

    with pytest.raises(OSError, match="disk full"):
        write_report(directory, thread_id="t", payload=PAYLOAD)

    names = sorted(p.name for p in directory.iterdir())
    assert len(names) == 1 and names[0].endswith(".json")  # the reserved, empty placeholder
    assert (directory / names[0]).read_bytes() == b""
    assert not list(directory.glob(".*.tmp"))


def test_non_json_values_are_stringified_not_lost(tmp_path: Path) -> None:
    directory = decisions_dir(tmp_path / "state")

    path = write_report(directory, thread_id="t", payload={**PAYLOAD, "output_root": Path("/x/skills")})

    assert json.loads(path.read_text(encoding="utf-8"))["output_root"] == "/x/skills"


def test_module_is_standalone_of_pipeline_and_langchain() -> None:
    code = (
        "import sys; import msagent.skill_evolver.report; "
        "print(sorted(m for m in sys.modules if m.startswith('langchain') or m in ("
        "'msagent.skill_evolver.direct_skill_generation', 'msagent.skill_evolver.mining', "
        "'msagent.skill_evolver.pipeline', 'msagent.cli.bootstrap.initializer')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parents[3],
    )

    assert result.stdout.strip() == "[]"
