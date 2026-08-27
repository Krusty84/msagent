"""Shared assertions over msagent subprocess results."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from validator_core.agent_runner import RunResult
from validator_core.trace_parser import (
    get_mcp_tool_calls,
    get_mcp_tool_results,
    get_tool_calls,
    get_tool_results,
)


def assert_session_succeeded(
    result: RunResult,
    *,
    require_assistant_message: bool = True,
) -> None:
    """Assert process, trace completion, and optional response invariants."""
    assert result.returncode == 0, (
        f"msagent 进程退出码为 {result.returncode}\n"
        f"stderr:\n{result.stderr}\napp.log:\n{result.app_log}"
    )
    errors = [event for event in result.traces if event.get("type") == "error"]
    assert not errors, json.dumps(errors, ensure_ascii=False, indent=2)

    finished = [
        event for event in result.traces if event.get("type") == "session_finished"
    ]
    assert finished, f"缺少 session_finished 事件: {result.traces!r}"
    assert finished[-1].get("exit_code") == 0, finished[-1]

    if require_assistant_message:
        messages = [
            event
            for event in result.traces
            if event.get("type") == "assistant_message"
            and str(event.get("content") or "").strip()
        ]
        assert messages, "会话没有产生非空 assistant_message"


def assert_app_log_has_no_fatal_exception(result: RunResult) -> None:
    """Reject traceback/exception lines except explicitly informational ones."""
    assert result.app_log.strip(), "未读取到本次运行的 app.log"
    fatal_lines = [
        line
        for line in result.app_log.splitlines()
        if ("traceback" in line.casefold() or "exception" in line.casefold())
        and " - info - " not in line.casefold()
    ]
    assert not fatal_lines, "app.log 包含致命异常:\n" + "\n".join(fatal_lines)


def assert_tool_invoked(
    result: RunResult,
    tool_name: str,
    *,
    input_matches: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[dict, dict]:
    """Return a matching tool call and its result after validating linkage."""
    calls = get_tool_calls(result.traces, tool_name)
    matching_calls = []
    for event in calls:
        tool_input = event.get("input")
        if not isinstance(tool_input, dict):
            continue
        if input_matches is None or input_matches(tool_input):
            matching_calls.append(event)

    assert matching_calls, (
        f"没有发现符合输入条件的 {tool_name} 调用，实际调用为："
        f"{json.dumps(calls, ensure_ascii=False, indent=2)}"
    )
    call = matching_calls[-1]
    item_id = call.get("item_id")
    assert item_id, f"{tool_name} 调用缺少 item_id: {call!r}"

    results = get_tool_results(result.traces, tool_name)
    matching_results = [event for event in results if event.get("item_id") == item_id]
    assert matching_results, (
        f"没有找到与 {tool_name} 调用 {item_id!r} 对应的结果："
        f"{json.dumps(results, ensure_ascii=False, indent=2)}"
    )
    return call, matching_results[-1]


def assert_mcp_tool_invoked(
    result: RunResult,
    server_name: str,
    tool_name: str,
    *,
    input_matches: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[dict, dict]:
    """Validate and return a linked MCP call/result pair.

    This is separate from :func:`assert_tool_invoked` because MCP adapters may
    expose the same raw tool using either ``server__tool`` or ``server_tool``.
    """
    calls = get_mcp_tool_calls(result.traces, server_name, tool_name)
    matching_calls = []
    for event in calls:
        tool_input = event.get("input")
        if not isinstance(tool_input, dict):
            continue
        if input_matches is None or input_matches(tool_input):
            matching_calls.append(event)

    assert matching_calls, (
        f"没有发现符合输入条件的 MCP 工具 {server_name}/{tool_name} 调用，"
        f"实际调用为：{json.dumps(calls, ensure_ascii=False, indent=2)}"
    )
    call = matching_calls[-1]
    item_id = call.get("item_id")
    assert item_id, f"MCP 工具 {server_name}/{tool_name} 调用缺少 item_id: {call!r}"

    results = get_mcp_tool_results(result.traces, server_name, tool_name)
    matching_results = [event for event in results if event.get("item_id") == item_id]
    assert matching_results, (
        f"没有找到与 MCP 工具 {server_name}/{tool_name} 调用 {item_id!r} "
        f"对应的结果：{json.dumps(results, ensure_ascii=False, indent=2)}"
    )
    return call, matching_results[-1]
