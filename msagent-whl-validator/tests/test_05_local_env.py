"""通过真实 msagent 工具轨迹验证文件系统和本地命令环境。"""

from __future__ import annotations

from pathlib import Path

import pytest

from validator_core.agent_runner import RunResult
from validator_core.assertions import (
    assert_app_log_has_no_fatal_exception,
    assert_session_succeeded,
    assert_tool_invoked,
)
from validator_core.trace_parser import get_tool_calls


FILESYSTEM_MARKER = "MSAGENT_FILESYSTEM_VALIDATION_OK"
LOCALSHELL_MARKER = "MSAGENT_LOCALSHELL_VALIDATION_OK"
MISSING_FILE_ERROR_KEYWORDS = (
    "error",
    "no such file",
    "not found",
    "does not exist",
    "不存在",
)

pytestmark = [pytest.mark.llm, pytest.mark.local]


def _create_runtime(msagent_runtime_factory, test_workspace):
    """创建包含固定测试数据、且不依赖 Profiler MCP 的隔离运行时。"""
    return msagent_runtime_factory(
        "openai",
        workspace_seed=test_workspace,
    )


def _tool_output(event: dict, tool_name: str) -> tuple[dict, str]:
    """提取工具输出结构和文本，并在 trace 格式异常时给出直接诊断。"""
    output = event.get("output")
    assert isinstance(output, dict), f"{tool_name} 返回结构无效: {event!r}"
    content = str(output.get("content") or "")
    assert content.strip(), f"{tool_name} 返回内容为空: {output!r}"
    return output, content


def _assert_run_is_healthy(result: RunResult) -> None:
    """工具完成后，msagent 进程、会话和 DEBUG 日志均应正常。"""
    assert_session_succeeded(result)
    assert_app_log_has_no_fatal_exception(result)


def test_filesystem_read_trace_is_successful(
    msagent_runtime_factory,
    test_workspace,
) -> None:
    """read_file 应读取隔离 workspace 文件并返回唯一验证标记。"""
    runtime = _create_runtime(msagent_runtime_factory, test_workspace)
    marker_path = (runtime.workspace_dir / "read_marker.txt").resolve()
    prompt = (
        "请必须使用 read_file 工具读取以下绝对路径的文件：\n"
        f"{marker_path}\n"
        "不要使用 execute、cat 或其他命令读取，也不要猜测文件内容。"
        "读取完成后，只回复文件中的内容。"
    )

    result = runtime.run(prompt, agent_name="Accuracy")

    _, tool_result = assert_tool_invoked(
        result,
        "read_file",
        input_matches=lambda tool_input: _same_path(
            tool_input.get("file_path"), marker_path
        ),
    )
    assert not get_tool_calls(result.traces, "execute"), (
        "文件读取用例不应绕过 read_file 使用 execute"
    )

    output, content = _tool_output(tool_result, "read_file")
    assert output.get("is_error") is False, f"read_file 执行失败: {output!r}"
    assert FILESYSTEM_MARKER in content, f"read_file 未返回验证标记: {content!r}"
    assert FILESYSTEM_MARKER in result.stdout, (
        f"最终回复未包含文件内容: {result.stdout!r}"
    )
    _assert_run_is_healthy(result)


def test_read_file_reports_missing_file(
    msagent_runtime_factory,
    test_workspace,
) -> None:
    """read_file 读取不存在的文件时，应返回明确错误且会话正常收口。"""
    runtime = _create_runtime(msagent_runtime_factory, test_workspace)
    missing_path = (runtime.workspace_dir / "missing-validation-file.txt").resolve()
    assert not missing_path.exists(), f"缺失文件测试路径意外存在: {missing_path}"

    prompt = (
        "请必须使用 read_file 工具读取以下绝对路径的文件：\n"
        f"{missing_path}\n"
        "不要使用 execute，也不要创建该文件。请根据工具的真实返回说明读取结果。"
    )
    result = runtime.run(prompt, agent_name="Accuracy")

    _, tool_result = assert_tool_invoked(
        result,
        "read_file",
        input_matches=lambda tool_input: _same_path(
            tool_input.get("file_path"), missing_path
        ),
    )
    assert not get_tool_calls(result.traces, "execute"), "缺失文件用例不应调用 execute"

    output, content = _tool_output(tool_result, "read_file")
    normalized = content.casefold()
    has_explicit_error = output.get("is_error") is True or any(
        keyword in normalized for keyword in MISSING_FILE_ERROR_KEYWORDS
    )
    assert has_explicit_error, f"read_file 未明确报告文件不存在: {output!r}"
    assert missing_path.name in content, (
        f"错误结果没有指明目标文件 {missing_path.name!r}: {content!r}"
    )
    assert FILESYSTEM_MARKER not in content, (
        f"缺失文件结果错误包含成功标记: {content!r}"
    )
    assert not missing_path.exists(), "read_file 不应创建缺失的目标文件"
    _assert_run_is_healthy(result)


def test_localshell_execution_trace_is_successful(
    msagent_runtime_factory,
    test_workspace,
) -> None:
    """execute 应运行 workspace 脚本并返回成功退出码和唯一输出标记。"""
    runtime = _create_runtime(msagent_runtime_factory, test_workspace)
    script_path = (runtime.workspace_dir / "print_marker.sh").resolve()
    prompt = (
        "请必须使用 execute 工具执行以下命令：\n"
        f'bash "{script_path}"\n'
        "不要直接猜测脚本输出。执行完成后，只回复命令输出。"
    )

    result = runtime.run(prompt, agent_name="Accuracy")

    _, tool_result = assert_tool_invoked(
        result,
        "execute",
        input_matches=lambda tool_input: script_path.as_posix()
        in str(tool_input.get("command") or ""),
    )
    output, content = _tool_output(tool_result, "execute")
    assert output.get("is_error") is False, f"execute 执行失败: {output!r}"
    assert LOCALSHELL_MARKER in content, f"脚本输出缺少验证标记: {content!r}"
    assert "command succeeded with exit code 0" in content.casefold(), (
        f"execute 没有报告成功退出码 0: {content!r}"
    )
    assert LOCALSHELL_MARKER in result.stdout, (
        f"最终回复未包含脚本输出: {result.stdout!r}"
    )
    _assert_run_is_healthy(result)


def test_msprof_analyze_cli_help_is_available(
    msagent_runtime_factory,
    test_workspace,
) -> None:
    """whl 环境应提供可由 Agent 调用的 msprof-analyze CLI。"""
    runtime = _create_runtime(msagent_runtime_factory, test_workspace)
    prompt = (
        "请必须使用 execute 工具执行以下命令：\n"
        "msprof-analyze --help\n"
        "不要安装或升级任何软件包，也不要猜测输出。"
        "执行完成后，只总结命令是否成功以及可用的主要子命令。"
    )

    result = runtime.run(prompt, agent_name="Accuracy")

    _, tool_result = assert_tool_invoked(
        result,
        "execute",
        input_matches=lambda tool_input: (
            "msprof-analyze" in str(tool_input.get("command") or "")
            and "--help" in str(tool_input.get("command") or "")
        ),
    )
    output, content = _tool_output(tool_result, "execute")
    normalized = content.casefold()
    assert output.get("is_error") is False, (
        f"msprof-analyze --help 执行失败: {output!r}"
    )
    assert "usage: msprof-analyze" in normalized, (
        f"输出不是 msprof-analyze 帮助信息: {content!r}"
    )
    for command in ("cluster", "advisor"):
        assert command in normalized, (
            f"msprof-analyze 帮助缺少关键子命令 {command!r}: {content!r}"
        )
    assert "command succeeded with exit code 0" in normalized, (
        f"msprof-analyze 没有报告成功退出码 0: {content!r}"
    )
    for error_text in ("command not found", "no such file"):
        assert error_text not in normalized, (
            f"msprof-analyze CLI 在目标环境中不可用: {content!r}"
        )
    _assert_run_is_healthy(result)


def _same_path(raw_path, expected: Path) -> bool:
    """比较模型工具参数中的路径，并将无效输入视为不匹配。"""
    if not isinstance(raw_path, str) or not raw_path.strip():
        return False
    try:
        return Path(raw_path).expanduser().resolve() == expected
    except OSError:
        return False
