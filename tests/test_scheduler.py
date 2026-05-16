from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from msagent.scheduler.loop import LoopTaskManager, load_default_loop_prompt, parse_loop_interval


def test_parse_loop_interval() -> None:
    assert parse_loop_interval("30s") == 30
    assert parse_loop_interval("5m") == 300
    assert parse_loop_interval("1h") == 3600
    assert parse_loop_interval("every 2h") == 7200


def test_loop_task_manager_lifecycle() -> None:
    async def _run() -> None:
        manager = LoopTaskManager()
        now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)
        task = await manager.add(prompt="check status", interval_seconds=60, now=now)

        assert task.next_run_at == now + timedelta(seconds=60)
        assert await manager.due(now=now + timedelta(seconds=59)) == []
        due = await manager.due(now=now + timedelta(seconds=60))
        assert [item.id for item in due] == [task.id]

        await manager.mark_finished(task.id, now=now + timedelta(seconds=60))
        tasks = await manager.list()
        assert tasks[0].run_count == 1
        assert tasks[0].next_run_at == now + timedelta(seconds=120)

        assert await manager.cancel(task.id) is True
        assert await manager.list() == []

    asyncio.run(_run())


def test_load_default_loop_prompt(tmp_path: Path) -> None:
    assert load_default_loop_prompt(tmp_path) == ""
    (tmp_path / "loop.md").write_text("review current status\n", encoding="utf-8")
    assert load_default_loop_prompt(tmp_path) == "review current status"
