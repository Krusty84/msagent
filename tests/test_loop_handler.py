from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from msagent.cli.handlers.loop import LoopHandler
from msagent.scheduler import LoopTaskKind, LoopTaskManager


def test_loop_handler_adds_task(tmp_path: Path) -> None:
    async def _run() -> None:
        session = SimpleNamespace(
            context=SimpleNamespace(working_dir=tmp_path),
            loop_tasks=LoopTaskManager(),
        )
        handler = LoopHandler(session)

        await handler.handle(["every", "5m", "check", "status"])

        tasks = await session.loop_tasks.list()
        assert len(tasks) == 1
        assert tasks[0].kind is LoopTaskKind.RECURRING
        assert tasks[0].interval_seconds == 300
        assert tasks[0].prompt == "check status"

    asyncio.run(_run())


def test_loop_handler_uses_loop_md(tmp_path: Path) -> None:
    async def _run() -> None:
        (tmp_path / "loop.md").write_text("summarize progress", encoding="utf-8")
        session = SimpleNamespace(
            context=SimpleNamespace(working_dir=tmp_path),
            loop_tasks=LoopTaskManager(),
        )
        handler = LoopHandler(session)

        await handler.handle(["every", "1h"])

        tasks = await session.loop_tasks.list()
        assert len(tasks) == 1
        assert tasks[0].kind is LoopTaskKind.RECURRING
        assert tasks[0].interval_seconds == 3600
        assert tasks[0].prompt == "summarize progress"

    asyncio.run(_run())


def test_loop_handler_adds_once_task(tmp_path: Path) -> None:
    async def _run() -> None:
        session = SimpleNamespace(
            context=SimpleNamespace(working_dir=tmp_path),
            loop_tasks=LoopTaskManager(),
        )
        handler = LoopHandler(session)

        await handler.handle(["in", "10m", "process", "trace_view.json"])

        tasks = await session.loop_tasks.list()
        assert len(tasks) == 1
        assert tasks[0].kind is LoopTaskKind.ONCE
        assert tasks[0].interval_seconds == 600
        assert tasks[0].prompt == "process trace_view.json"

    asyncio.run(_run())
