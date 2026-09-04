"""Read-only access to persisted thread history for report-style commands."""

from __future__ import annotations

from typing import Literal

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from msagent.cli.bootstrap.initializer import initializer
from msagent.utils.compression import calculate_message_tokens


async def load_history(session, target: str | None) -> tuple[str, list[AnyMessage]]:
    """Return (thread_id, messages) without mutating any thread.

    target=None -> current thread; "last" -> most recent other thread;
    otherwise -> explicit thread id.
    """
    ctx = session.context

    if target is None and session.graph is not None:
        config = RunnableConfig(configurable={"thread_id": ctx.thread_id})
        snapshot = await session.graph.aget_state(config)
        values = snapshot.values if snapshot is not None else {}
        return ctx.thread_id, list(values.get("messages", []) or [])

    async with initializer.get_checkpointer(ctx.agent, ctx.working_dir) as checkpointer:
        thread_id = target or ctx.thread_id
        if target == "last":
            latest = await latest_other_thread(checkpointer, exclude=ctx.thread_id)
            if latest is None:
                return "last", []
            thread_id = latest

        checkpoint_tuple = await checkpointer.aget_tuple(
            RunnableConfig(configurable={"thread_id": thread_id})
        )
        if checkpoint_tuple is None:
            return thread_id, []
        channel_values = (checkpoint_tuple.checkpoint or {}).get("channel_values", {})
        return thread_id, list(channel_values.get("messages", []) or [])


async def latest_other_thread(checkpointer, *, exclude: str) -> str | None:
    """Find the most recent thread other than the current one."""
    conn = getattr(checkpointer, "conn", None)
    if conn is not None:
        await checkpointer.setup()
        async with conn.execute(
            "SELECT thread_id FROM checkpoints "
            "WHERE checkpoint_ns = '' AND thread_id != ? "
            "GROUP BY thread_id ORDER BY MAX(checkpoint_id) DESC LIMIT 1",
            (exclude,),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    async for checkpoint_tuple in checkpointer.alist(None):
        thread_id = str(checkpoint_tuple.config.get("configurable", {}).get("thread_id", ""))
        if thread_id and thread_id != exclude:
            return thread_id
    return None


def trim_history(
    messages: list[AnyMessage],
    llm,
    context_window: int | None,
    *,
    budget_ratio: float = 0.6,
    strategy: Literal["tail", "head_tail"] = "head_tail",
    head_keep: int = 6,
) -> tuple[list[AnyMessage], int]:
    """Trim history to ``budget_ratio`` of the context window; return (kept, omitted count).

    ``tail`` drops the oldest messages until the rest fits. ``head_tail`` (default) keeps
    the first ``head_keep`` messages — the task statement, the most valuable part for
    distillation — and cuts from the middle, keeping the newest messages. In both
    strategies the kept tail starts with a ``HumanMessage``, so the replay never opens
    with an orphaned tool result or model reply.
    """
    kept = list(messages)
    if not context_window or context_window <= 0:
        return kept, 0

    budget = int(context_window * budget_ratio)
    if calculate_message_tokens(kept, llm) <= budget:
        return kept, 0

    if strategy == "tail":
        return _drop_oldest(kept, llm, budget)

    # The head must not end inside a tool exchange: an AIMessage whose tool_calls lost
    # their ToolMessage to the cut is rejected by OpenAI-compatible APIs. Messages popped
    # here are not lost — they become the start of the tail.
    head = kept[:head_keep]
    while head and _has_unanswered_tool_calls(head):
        head.pop()
    tail = kept[len(head) :]

    omitted = 0
    while tail and calculate_message_tokens(head + tail, llm) > budget:
        drop = max(1, len(tail) // 10)
        tail = tail[drop:]
        omitted += drop
        tail, realigned = _align_to_human(tail)
        omitted += realigned

    if tail:
        return head + tail, omitted

    # Even the head alone exceeds the budget: fall back to the tail strategy on the full
    # history, so at least the newest messages survive.
    return _drop_oldest(kept, llm, budget)


def _drop_oldest(messages: list[AnyMessage], llm, budget: int) -> tuple[list[AnyMessage], int]:
    """Drop messages from the start until the list fits ``budget``; keep a HumanMessage first."""
    kept = list(messages)
    omitted = 0
    while len(kept) > 1 and calculate_message_tokens(kept, llm) > budget:
        drop = max(1, len(kept) // 10)
        kept = kept[drop:]
        omitted += drop
        kept, realigned = _align_to_human(kept)
        omitted += realigned
    return kept, omitted


def _align_to_human(messages: list[AnyMessage]) -> tuple[list[AnyMessage], int]:
    """Drop leading messages until the list starts with a HumanMessage."""
    index = 0
    while index < len(messages) and not isinstance(messages[index], HumanMessage):
        index += 1
    return messages[index:], index


def _has_unanswered_tool_calls(messages: list[AnyMessage]) -> bool:
    """True when some AIMessage.tool_calls in the slice lacks its ToolMessage."""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return any(
        call.get("id") not in answered
        for m in messages
        if isinstance(m, AIMessage)
        for call in (m.tool_calls or [])
    )
