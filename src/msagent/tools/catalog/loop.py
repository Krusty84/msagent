"""Runtime tools for session-scoped loop tasks."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field

from msagent.scheduler import LoopTaskKind, format_loop_time, parse_loop_delay, parse_loop_interval


def _session_from_runtime(runtime: Any) -> Any:
    context = getattr(runtime, "context", None)
    session = getattr(context, "session", None) if context is not None else None
    if session is None:
        raise ToolException("Loop task tools are only available in interactive sessions")
    return session


class AddLoopTaskInput(BaseModel):
    delay: str = Field(description="Delay or interval such as '10m', 'in 10m', '30s', '5m', or 'every 1h'")
    prompt: str = Field(description="Prompt to send when the loop task wakes up")
    recurring: bool = Field(default=False, description="Whether to repeat the task after each run")


@tool("add_loop_task", args_schema=AddLoopTaskInput)
async def add_loop_task(*, delay: str, prompt: str, recurring: bool = False, runtime: Any = None) -> str:
    """Create a session-scoped one-shot or recurring loop task."""
    session = _session_from_runtime(runtime)
    kind = LoopTaskKind.RECURRING if recurring else LoopTaskKind.ONCE
    interval_seconds = parse_loop_interval(delay) if recurring else parse_loop_delay(delay)
    task = await session.loop_tasks.add(prompt=prompt, interval_seconds=interval_seconds, kind=kind)
    label = f"every {delay}" if recurring else f"in {delay}"
    return (
        f"Loop task {task.id} scheduled {label}; "
        f"next run at {format_loop_time(task.next_run_at)}; expires at {format_loop_time(task.expires_at)}"
    )


@tool("list_loop_tasks")
async def list_loop_tasks(*, runtime: Any = None) -> str:
    """List active session-scoped loop tasks."""
    session = _session_from_runtime(runtime)
    tasks = await session.loop_tasks.list()
    if not tasks:
        return "No active loop tasks"
    lines = []
    for task in tasks:
        preview = task.prompt.replace("\n", " ").strip()
        if len(preview) > 80:
            preview = f"{preview[:77]}..."
        lines.append(
            f"{task.id}: {task.kind.value} {task.interval_seconds}s, next={format_loop_time(task.next_run_at)}, "
            f"runs={task.run_count}, prompt={preview}"
        )
    return "\n".join(lines)


class CancelLoopTaskInput(BaseModel):
    task_id: str = Field(description="Loop task ID to cancel")


@tool("cancel_loop_task", args_schema=CancelLoopTaskInput)
async def cancel_loop_task(*, task_id: str, runtime: Any = None) -> str:
    """Cancel a session-scoped loop task."""
    session = _session_from_runtime(runtime)
    cancelled = await session.loop_tasks.cancel(task_id)
    if not cancelled:
        return f"Loop task {task_id} not found"
    return f"Cancelled loop task {task_id}"
