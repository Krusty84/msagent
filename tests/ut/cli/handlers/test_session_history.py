from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from msagent.cli.handlers import session_history


@pytest.fixture(autouse=True)
def count_messages_as_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """One message == one token, so budgets can be reasoned about in message counts."""
    monkeypatch.setattr(
        session_history, "calculate_message_tokens", lambda messages, _llm: len(messages)
    )


def _turns(count: int) -> list[AnyMessage]:
    """Alternating user/assistant turns: H0, A1, H2, A3, ..."""
    return [
        HumanMessage(content=f"user {i}") if i % 2 == 0 else AIMessage(content=f"assistant {i}")
        for i in range(count)
    ]


def test_trim_history_is_noop_without_context_window() -> None:
    messages = _turns(30)

    assert session_history.trim_history(messages, None, None) == (messages, 0)
    assert session_history.trim_history(messages, None, 0) == (messages, 0)


def test_trim_history_is_noop_within_budget() -> None:
    messages = _turns(10)

    kept, omitted = session_history.trim_history(messages, None, 100)

    assert kept == messages
    assert omitted == 0


def test_head_tail_keeps_task_statement_and_newest_tail() -> None:
    messages = _turns(40)

    # budget = int(20 * 0.6) = 12 messages
    kept, omitted = session_history.trim_history(messages, None, 20)

    assert len(kept) <= 12
    assert kept[:6] == messages[:6]
    tail = kept[6:]
    assert tail, "the newest messages must survive"
    assert isinstance(tail[0], HumanMessage)
    assert tail == messages[len(messages) - len(tail) :]
    assert omitted == len(messages) - len(kept)


def test_head_tail_head_boundary_does_not_leave_unanswered_tool_call() -> None:
    messages: list[AnyMessage] = [
        HumanMessage(content="task"),
        AIMessage(content="", tool_calls=[{"name": "run", "args": {}, "id": "call-1"}]),
        ToolMessage(content="ok", tool_call_id="call-1"),
        AIMessage(content="done"),
        HumanMessage(content="next"),
        # index 5 is the default head boundary; its ToolMessage (index 6) would be cut away
        AIMessage(content="", tool_calls=[{"name": "run", "args": {}, "id": "call-2"}]),
        ToolMessage(content="ok", tool_call_id="call-2"),
        *_turns(40),
    ]

    kept, omitted = session_history.trim_history(messages, None, 20)

    assert kept[:5] == messages[:5]
    assert isinstance(kept[5], HumanMessage)
    assert not session_history._has_unanswered_tool_calls(kept)
    assert omitted == len(messages) - len(kept)


def test_head_tail_falls_back_to_dropping_head_when_head_alone_exceeds_budget() -> None:
    messages = _turns(8)

    # budget = int(5 * 0.6) = 3 messages, smaller than the default head of 6
    kept, omitted = session_history.trim_history(messages, None, 5)

    assert 0 < len(kept) <= 3
    assert isinstance(kept[0], HumanMessage)
    assert kept == messages[len(messages) - len(kept) :]
    assert omitted == len(messages) - len(kept)


def test_tail_strategy_drops_oldest_messages() -> None:
    messages = _turns(40)

    kept, omitted = session_history.trim_history(messages, None, 20, strategy="tail")

    assert len(kept) <= 12
    assert isinstance(kept[0], HumanMessage)
    assert kept == messages[len(messages) - len(kept) :]
    assert omitted == len(messages) - len(kept)
