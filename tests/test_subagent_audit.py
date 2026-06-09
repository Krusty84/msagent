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

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from msagent.audit.events import AuditEvent, AuditEventType, format_audit_timestamp
from msagent.audit.protocol import parse_completion_output, parse_delegation_input
from langchain_core.messages import AIMessage, HumanMessage

from msagent.audit.user_interaction import build_user_response_fields, extract_last_agent_prompt
from msagent.audit.read import AuditReader, iter_json_values
from msagent.audit.tracker import SubagentAuditTracker
from msagent.audit.writer import AuditWriter, build_audit_filename, resolve_audit_log_enabled
from msagent.configs import AuditLogConfig
from msagent.core.constants import CONFIG_AUDIT_DIR

MSAGENT_IO_INPUT = """\
Generate practice YAML.

```msagent-io v1
{
  "protocol": "msagent.subagent_io",
  "version": "1",
  "subagent_type": "quant-tuning-practice-generator",
  "input": {
    "model_type": "qwen3",
    "round": 1
  }
}
```"""

MSAGENT_IO_OUTPUT = """\
Practice YAML ready.

```msagent-io v1
{
  "protocol": "msagent.subagent_io",
  "version": "1",
  "subagent_type": "quant-tuning-practice-generator",
  "status": "ok",
  "output": {
    "practice_path": "/tmp/practice_round_1.yaml"
  }
}
```"""


def test_subagent_audit_records_merged_delegation_event(tmp_path: Path) -> None:
    writer = AuditWriter(
        working_dir=tmp_path,
        thread_id="thread-1",
        agent_name="Auto-tuning",
        enabled=True,
    )
    tracker = SubagentAuditTracker(writer)
    tracker.begin_run("run-1")

    tracker.observe(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {
                        "subagent_type": "quant-tuning-practice-generator",
                        "description": MSAGENT_IO_INPUT,
                    },
                    "id": "call-task-1",
                    "type": "tool_call",
                }
            ],
        ),
        namespace=(),
    )
    tracker.observe(
        ToolMessage(
            content=MSAGENT_IO_OUTPUT,
            tool_call_id="call-task-1",
            name="task",
        ),
        namespace=(),
    )
    tracker.observe(
        AIMessage(content="ignored", tool_calls=[{"name": "run_command", "args": {}, "id": "x"}]),
        namespace=("subagent",),
    )

    reader = AuditReader(working_dir=tmp_path, thread_id="thread-1")
    events = list(reader.iter_events())
    assert len(events) == 1
    event = events[0]
    assert event["event"] == AuditEventType.SUBAGENT_DELEGATION
    assert event["run_id"] == "run-1"
    assert event["agent_name"] == "Auto-tuning"
    assert event["subagent_type"] == "quant-tuning-practice-generator"
    assert event["status"] == "ok"
    assert event["start_time"] == format_audit_timestamp()
    assert event["end_time"] == format_audit_timestamp()
    assert list(event.keys())[0] == "agent_name"
    assert "timestamp" not in event
    assert "protocol_version" not in event
    assert event["input_valid"] is True
    assert event["output_valid"] is True
    assert event["input"] == {"model_type": "qwen3", "round": 1}
    assert event["output"] == {"practice_path": "/tmp/practice_round_1.yaml"}
    assert "task_description_raw" not in event
    assert "result_raw" not in event
    assert "thread_id" not in event

    summary = reader.list_delegations()
    assert len(summary) == 1
    assert summary[0]["status"] == "ok"
    assert summary[0]["start_time"] == format_audit_timestamp()
    assert summary[0]["input"]["round"] == 1
    assert summary[0]["output"]["practice_path"] == "/tmp/practice_round_1.yaml"


def test_subagent_audit_keeps_raw_text_when_protocol_missing(tmp_path: Path) -> None:
    writer = AuditWriter(
        working_dir=tmp_path,
        thread_id="thread-raw",
        agent_name="Auto-tuning",
        enabled=True,
    )
    tracker = SubagentAuditTracker(writer)
    tracker.begin_run("run-raw")

    tracker.observe(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {
                        "subagent_type": "quant-tuning-practice-generator",
                        "description": "Generate practice YAML for round 1",
                    },
                    "id": "call-task-raw",
                }
            ],
        ),
        namespace=(),
    )
    tracker.observe(
        ToolMessage(
            content="Generated practice_round_1.yaml",
            tool_call_id="call-task-raw",
            name="task",
        ),
        namespace=(),
    )

    reader = AuditReader(working_dir=tmp_path, thread_id="thread-raw")
    events = list(reader.iter_events())
    assert len(events) == 1
    event = events[0]
    assert event["input_valid"] is False
    assert event["output_valid"] is False
    assert "Generate practice YAML" in event["task_description_raw"]
    assert "practice_round_1.yaml" in event["result_raw"]
    assert "input" not in event
    assert "output" not in event


def test_subagent_audit_marks_failed_task_result(tmp_path: Path) -> None:
    writer = AuditWriter(
        working_dir=tmp_path,
        thread_id="thread-2",
        agent_name="Auto-tuning",
        enabled=True,
    )
    tracker = SubagentAuditTracker(writer)
    tracker.begin_run("run-2")

    tracker.observe(
        ToolMessage(
            content="We cannot invoke subagent missing because it does not exist",
            tool_call_id="call-task-2",
            name="task",
            status="error",
        ),
        namespace=(),
    )

    reader = AuditReader(working_dir=tmp_path, thread_id="thread-2")
    events = list(reader.iter_events())
    assert len(events) == 1
    assert events[0]["event"] == AuditEventType.SUBAGENT_DELEGATION
    assert events[0]["status"] == "failed"
    assert events[0]["subagent_type"] == "unknown"


def test_audit_writer_disabled_skips_file(tmp_path: Path) -> None:
    writer = AuditWriter(
        working_dir=tmp_path,
        thread_id="thread-3",
        agent_name="Auto-tuning",
        enabled=False,
    )
    tracker = SubagentAuditTracker(writer)
    tracker.begin_run("run-3")
    tracker.observe(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {"subagent_type": "quant-tuning-evaluator", "description": "x"},
                    "id": "call-task-3",
                }
            ],
        ),
        namespace=(),
    )
    tracker.observe(
        ToolMessage(content="done", tool_call_id="call-task-3", name="task"),
        namespace=(),
    )

    audit_file = tmp_path / CONFIG_AUDIT_DIR / build_audit_filename(
        agent_name="Auto-tuning",
        thread_id="thread-3",
    )
    assert not audit_file.exists()


def test_audit_writer_rebinds_thread_file(tmp_path: Path) -> None:
    writer = AuditWriter(
        working_dir=tmp_path,
        thread_id="thread-a",
        agent_name="Auto-tuning",
        enabled=True,
    )
    writer.rebind(thread_id="thread-b")
    assert writer.path.name == build_audit_filename(agent_name="Auto-tuning", thread_id="thread-b")


def test_audit_writer_appends_pretty_json_blocks(tmp_path: Path) -> None:
    writer = AuditWriter(
        working_dir=tmp_path,
        thread_id="thread-pretty",
        agent_name="Auto-tuning",
        enabled=True,
    )
    tracker = SubagentAuditTracker(writer)
    tracker.begin_run("run-pretty")
    tracker.observe(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {
                        "subagent_type": "quant-tuning-quantizer",
                        "description": "quantize model",
                    },
                    "id": "call-pretty-1",
                }
            ],
        ),
        namespace=(),
    )
    tracker.observe(
        ToolMessage(content="quantized", tool_call_id="call-pretty-1", name="task"),
        namespace=(),
    )

    audit_file = tmp_path / CONFIG_AUDIT_DIR / build_audit_filename(
        agent_name="Auto-tuning",
        thread_id="thread-pretty",
    )
    content = audit_file.read_text(encoding="utf-8")
    payloads = list(iter_json_values(content))
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["event"] == AuditEventType.SUBAGENT_DELEGATION
    assert payload["run_id"] == "run-pretty"
    assert list(payload.keys())[0] == "agent_name"
    assert "timestamp" not in payload
    assert "protocol_version" not in payload
    assert "ts" not in payload
    assert "delegations" not in payload
    assert '\n  "' in content


def test_audit_event_model_serializes_without_null_fields() -> None:
    event = AuditEvent.delegation(
        run_id="run-1",
        agent_name="Auto-tuning",
        delegation_id="call-1",
        subagent_type="quant-tuning-evaluator",
        start_time="2026-06-02 07:42:23",
        end_time="2026-06-02 07:42:45",
        duration_ms=22000,
        status="ok",
    )
    payload = event.to_json_dict()
    assert list(payload.keys())[:3] == ["agent_name", "event", "subagent_type"]
    assert payload["event"] == AuditEventType.SUBAGENT_DELEGATION
    assert payload["start_time"] == "2026-06-02 07:42:23"
    assert payload["end_time"] == "2026-06-02 07:42:45"
    assert "timestamp" not in payload
    assert "protocol_version" not in payload
    assert "input" not in payload


def test_protocol_rejects_deprecated_evaluation_generator_fields() -> None:
    legacy_input = """\
```msagent-io v1
{
  "protocol": "msagent.subagent_io",
  "version": "1",
  "subagent_type": "quant-tuning-evaluation-generator",
  "input": {
    "model_name": "Qwen3-8B-w8a8",
    "save_path": "/tmp/record/",
    "target_datasets": ["gpqa"],
    "accuracy_targets": {"gpqa": 79.0}
  }
}
```"""
    result = parse_delegation_input(
        legacy_input,
        expected_subagent_type="quant-tuning-evaluation-generator",
    )
    assert result.parsed is True
    assert result.valid is False
    assert any("deprecated_fields" in error for error in result.errors)
    assert "missing_datasets" in result.errors


def test_subagent_audit_records_evaluation_generator_datasets_input(tmp_path: Path) -> None:
    eval_input = """\
```msagent-io v1
{
  "protocol": "msagent.subagent_io",
  "version": "1",
  "subagent_type": "quant-tuning-evaluation-generator",
  "input": {
    "model_name": "Qwen3-8B-w8a8",
    "save_path": "/tmp/record/",
    "datasets": [
      {
        "name": "gpqa",
        "config_name": "gpqa_gen_0_shot_cot_str",
        "target": 79.0,
        "tolerance": 1.0
      }
    ],
    "device_count": 2
  }
}
```"""
    eval_output = """\
```msagent-io v1
{
  "protocol": "msagent.subagent_io",
  "version": "1",
  "subagent_type": "quant-tuning-evaluation-generator",
  "status": "ok",
  "output": {
    "evaluate_config_path": "/tmp/record/evaluate.yaml"
  }
}
```"""
    writer = AuditWriter(
        working_dir=tmp_path,
        thread_id="thread-eval",
        agent_name="Auto-tuning",
        enabled=True,
    )
    tracker = SubagentAuditTracker(writer)
    tracker.begin_run("run-eval")
    tracker.observe(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {
                        "subagent_type": "quant-tuning-evaluation-generator",
                        "description": eval_input,
                    },
                    "id": "call-eval-1",
                }
            ],
        ),
        namespace=(),
    )
    tracker.observe(
        ToolMessage(content=eval_output, tool_call_id="call-eval-1", name="task"),
        namespace=(),
    )

    event = list(AuditReader(working_dir=tmp_path, thread_id="thread-eval").iter_events())[0]
    assert event["input_valid"] is True
    assert event["input"]["datasets"] == [
        {
            "name": "gpqa",
            "config_name": "gpqa_gen_0_shot_cot_str",
            "target": 79.0,
            "tolerance": 1.0,
        }
    ]
    assert event["start_time"]
    assert event["end_time"]


def test_format_audit_timestamp_uses_local_wall_clock() -> None:
    assert format_audit_timestamp(now=datetime(2026, 6, 2, 7, 42, 23)) == "2026-06-02 07:42:23"


def test_protocol_parser_extracts_structured_input_and_output() -> None:
    input_result = parse_delegation_input(
        MSAGENT_IO_INPUT,
        expected_subagent_type="quant-tuning-practice-generator",
    )
    assert input_result.parsed is True
    assert input_result.valid is True
    assert input_result.input_data == {"model_type": "qwen3", "round": 1}

    output_result = parse_completion_output(
        MSAGENT_IO_OUTPUT,
        expected_subagent_type="quant-tuning-practice-generator",
    )
    assert output_result.parsed is True
    assert output_result.valid is True
    assert output_result.io_status == "ok"
    assert output_result.output_data == {"practice_path": "/tmp/practice_round_1.yaml"}


def test_protocol_parser_reports_subagent_type_mismatch() -> None:
    result = parse_delegation_input(
        MSAGENT_IO_INPUT,
        expected_subagent_type="quant-tuning-evaluator",
    )
    assert result.parsed is True
    assert result.valid is False
    assert "subagent_type_mismatch" in result.errors


def test_resolve_audit_log_enabled_respects_agent_yaml() -> None:
    enabled_agent = SimpleNamespace(
        audit_log=AuditLogConfig(enabled=True),
    )
    disabled_agent = SimpleNamespace(audit_log=None)

    assert resolve_audit_log_enabled(enabled_agent) is True
    assert resolve_audit_log_enabled(disabled_agent) is False


def test_audit_log_config_defaults_to_disabled() -> None:
    assert AuditLogConfig().enabled is False


def test_extract_last_agent_prompt_reads_latest_assistant_message() -> None:
    prompt = extract_last_agent_prompt(
        [
            HumanMessage(content="start"),
            AIMessage(content="请确认配置是否无误。"),
        ]
    )
    assert prompt == "请确认配置是否无误。"


def test_user_turn_recorded_when_begin_run_includes_message(tmp_path: Path) -> None:
    writer = AuditWriter(
        working_dir=tmp_path,
        thread_id="thread-user-turn",
        agent_name="Auto-tuning",
        enabled=True,
    )
    tracker = SubagentAuditTracker(writer)
    tracker.begin_run("run-user-1", user_message="Tune Qwen3-8B with GPQA target 79%")

    events = list(AuditReader(working_dir=tmp_path, thread_id="thread-user-turn").iter_events())
    assert len(events) == 1
    event = events[0]
    assert event["event"] == AuditEventType.USER_TURN
    assert event["run_id"] == "run-user-1"
    assert event["agent_name"] == "Auto-tuning"
    assert "Qwen3-8B" in event["message"]
    assert list(event.keys())[:3] == ["agent_name", "event", "run_id"]


def test_user_response_choice_event(tmp_path: Path) -> None:
    writer = AuditWriter(
        working_dir=tmp_path,
        thread_id="thread-user-response",
        agent_name="Auto-tuning",
        enabled=True,
    )
    writer.begin_run("run-response-1")
    writer.emit_user_response(
        kind="choice",
        prompt="Continue Round 3?",
        options=["continue", "stop"],
        response="continue",
        context={"interrupt_id": "interrupt-1"},
    )

    event = list(AuditReader(working_dir=tmp_path, thread_id="thread-user-response").iter_events())[0]
    assert event["event"] == AuditEventType.USER_RESPONSE
    assert event["kind"] == "choice"
    assert event["response"] == "continue"
    assert event["context"]["interrupt_id"] == "interrupt-1"


def test_build_user_response_fields_for_hitl_approval() -> None:
    class _Interrupt:
        id = "int-1"
        value = {
            "action_requests": [
                {
                    "name": "execute",
                    "description": "Delete round_4 artifacts",
                    "args": {"command": "rm -rf /tmp/round_4"},
                }
            ],
            "review_configs": [],
        }

    fields = build_user_response_fields(_Interrupt(), {"decisions": [{"type": "reject"}]})
    assert fields is not None
    assert fields["kind"] == "approval"
    assert fields["response"] == "reject"
    assert fields["context"]["tool_name"] == "execute"
    assert "Delete round_4" in fields["prompt"]


def test_user_turn_includes_prior_agent_prompt(tmp_path: Path) -> None:
    writer = AuditWriter(
        working_dir=tmp_path,
        thread_id="thread-user-prompt",
        agent_name="Auto-tuning",
        enabled=True,
    )
    tracker = SubagentAuditTracker(writer)
    tracker.begin_run(
        "run-user-prompt",
        user_message="确认无误",
        prompt="请确认 base_info 是否无误。",
    )

    event = list(AuditReader(working_dir=tmp_path, thread_id="thread-user-prompt").iter_events())[0]
    assert event["message"] == "确认无误"
    assert event["prompt"] == "请确认 base_info 是否无误。"


def test_user_turn_precedes_delegation_in_same_run(tmp_path: Path) -> None:
    writer = AuditWriter(
        working_dir=tmp_path,
        thread_id="thread-timeline",
        agent_name="Auto-tuning",
        enabled=True,
    )
    tracker = SubagentAuditTracker(writer)
    tracker.begin_run("run-timeline", user_message="start tuning")
    tracker.observe(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {
                        "subagent_type": "quant-tuning-practice-generator",
                        "description": MSAGENT_IO_INPUT,
                    },
                    "id": "call-timeline-1",
                }
            ],
        ),
        namespace=(),
    )
    tracker.observe(
        ToolMessage(content=MSAGENT_IO_OUTPUT, tool_call_id="call-timeline-1", name="task"),
        namespace=(),
    )

    events = list(AuditReader(working_dir=tmp_path, thread_id="thread-timeline").iter_events())
    assert len(events) == 2
    assert events[0]["event"] == AuditEventType.USER_TURN
    assert events[1]["event"] == AuditEventType.SUBAGENT_DELEGATION
    assert events[0]["run_id"] == events[1]["run_id"] == "run-timeline"


def test_subagent_audit_preserves_full_result_raw_over_short_content(tmp_path: Path) -> None:
    writer = AuditWriter(
        working_dir=tmp_path,
        thread_id="thread-full-raw",
        agent_name="Auto-tuning",
        enabled=True,
    )
    tracker = SubagentAuditTracker(writer)
    tracker.begin_run("run-full-raw")

    full_result = "x" * 5000
    tracker.observe(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {
                        "subagent_type": "quant-tuning-practice-generator",
                        "description": "plain task",
                    },
                    "id": "call-full-raw",
                }
            ],
        ),
        namespace=(),
    )
    message = ToolMessage(content=full_result, tool_call_id="call-full-raw", name="task")
    setattr(message, "short_content", full_result[:200] + "... (truncated)")
    tracker.observe(message, namespace=())

    event = list(AuditReader(working_dir=tmp_path, thread_id="thread-full-raw").iter_events())[0]
    assert event["result_raw"] == full_result


def test_extract_last_agent_prompt_preserves_full_text() -> None:
    long_prompt = "请确认配置。" + ("详细说明。" * 500)
    prompt = extract_last_agent_prompt([HumanMessage(content="start"), AIMessage(content=long_prompt)])
    assert prompt == long_prompt


def test_build_user_response_fields_preserves_full_prompt_and_args() -> None:
    long_command = "echo " + ("a" * 2000)

    class _Interrupt:
        id = "int-long"
        value = {
            "action_requests": [
                {
                    "name": "execute",
                    "description": "Review shell command execution before running.",
                    "args": {"command": long_command},
                }
            ],
            "review_configs": [],
        }

    fields = build_user_response_fields(_Interrupt(), {"decisions": [{"type": "approve"}]})
    assert fields is not None
    assert long_command in fields["prompt"]
    assert "..." not in fields["prompt"]
