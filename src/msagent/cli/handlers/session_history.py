"""Read-only access to persisted thread history for report-style commands."""

from __future__ import annotations

from langchain_core.messages import AnyMessage
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
) -> tuple[list[AnyMessage], int]:
    """Drop oldest messages until history fits the token budget."""
    if not context_window or context_window <= 0:
        return list(messages), 0

    budget = int(context_window * budget_ratio)
    kept = list(messages)
    omitted = 0
    while len(kept) > 1 and calculate_message_tokens(kept, llm) > budget:
        drop = max(1, len(kept) // 10)
        kept = kept[drop:]
        omitted += drop
    return kept, omitted