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

"""The recorder callback must receive tool events through langchain's manager."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from langchain_core.callbacks.manager import CallbackManager
from langchain_core.messages import ToolMessage

from msagent.trajectory_recorder.callback import TrajectoryCallbackHandler
from msagent.trajectory_recorder.config import TrajectoryRecorderConfig
from msagent.trajectory_recorder.reader import load_trajectory
from msagent.trajectory_recorder.recorder import TrajectoryRecorder


def test_agent_events_are_not_ignored() -> None:
    # langchain_core dispatches on_tool_start/on_tool_end/on_tool_error under
    # the ``ignore_agent`` gate; opting out silently drops every tool event.
    assert TrajectoryCallbackHandler.ignore_agent is False


def test_tool_events_reach_recorder_through_callback_manager(tmp_path: Path) -> None:
    path = tmp_path / "Profiler_t1.jsonl"
    recorder = TrajectoryRecorder(
        path=path,
        config=TrajectoryRecorderConfig(),
        base_fields={"thread_id": "t1", "agent": "Profiler"},
    )
    handler = TrajectoryCallbackHandler(
        recorder=recorder,
        capture=recorder.config.capture,
        turn_run_id="run-1",
    )
    metadata = {"checkpoint_ns": "tools:abc"}
    manager = CallbackManager(handlers=[handler], metadata=metadata)

    recorder.emit("turn.start", {"run_id": "run-1", "user_message": "hi"})
    run = manager.on_tool_start(
        {"name": "bash"},
        "ls",
        inputs={"cmd": "ls"},
        run_id=uuid4(),
    )
    run.on_tool_end(ToolMessage(content="ok", tool_call_id="tc1"))
    failing = manager.on_tool_start(
        {"name": "grep"},
        "x",
        inputs={"pattern": "x"},
        run_id=uuid4(),
    )
    failing.on_tool_error(RuntimeError("boom"))
    recorder.emit("turn.end", {"run_id": "run-1", "status": "completed"})

    lines = path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    assert [event["event"] for event in events] == [
        "turn.start",
        "tool.start",
        "tool.result",
        "tool.start",
        "tool.error",
        "turn.end",
    ]
    assert events[1]["input"] == {"cmd": "ls"}
    assert events[1]["graph"]["checkpoint_ns"] == "tools:abc"
    assert events[2]["name"] == "bash"
    assert events[2]["message"]["data"]["content"] == "ok"
    assert events[4]["error_type"] == "RuntimeError"

    (turn,) = load_trajectory(path).turns
    assert turn.status == "completed"
    assert [(call.name, call.status, call.args) for call in turn.tool_calls] == [
        ("bash", "ok", {"cmd": "ls"}),
        ("grep", "error", {"pattern": "x"}),
    ]
    assert turn.tool_calls[0].output_text == "ok"
    assert turn.tool_calls[1].error == "boom"
