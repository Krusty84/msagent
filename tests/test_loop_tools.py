from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.tools import ToolException

from msagent.tools.catalog.loop import _session_from_runtime


def test_loop_tools_reject_non_interactive_session() -> None:
    runtime = SimpleNamespace(context=SimpleNamespace(session=SimpleNamespace(loop_tasks_enabled=False)))

    with pytest.raises(ToolException, match="only available in interactive sessions"):
        _session_from_runtime(runtime)


def test_loop_tools_accept_interactive_session() -> None:
    session = SimpleNamespace(loop_tasks_enabled=True)
    runtime = SimpleNamespace(context=SimpleNamespace(session=session))

    assert _session_from_runtime(runtime) is session
