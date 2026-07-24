from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from msagent.scheduler.loop import (
    LoopTaskKind,
    LoopTaskManager,
    load_default_loop_prompt,
    next_cron_run,
    parse_loop_cron,
    parse_loop_daily,
    parse_loop_delay,
    parse_loop_interval,
)


def test_parse_loop_interval() -> None:
    assert parse_loop_interval("30s") == 30
    assert parse_loop_interval("5m") == 300
    assert parse_loop_interval("1h") == 3600
    assert parse_loop_interval("every 2h") == 7200


def test_parse_loop_delay() -> None:
    assert parse_loop_delay("10m") == 600
    assert parse_loop_delay("in 10m") == 600
    assert parse_loop_delay("after 1h") == 3600
    assert parse_loop_delay("once 30s") == 30


def test_parse_loop_cron_and_daily() -> None:
    assert parse_loop_cron("0 6 * * *") == "0 6 * * *"
    assert parse_loop_daily("daily 06:00") == "0 6 * * *"


def test_next_cron_run() -> None:
    after = datetime(2026, 5, 25, 5, 30, 0, tzinfo=timezone.utc)
    result = next_cron_run("0 6 * * *", after=after)
    local = result.astimezone()
    assert local.hour == 6
    assert local.minute == 0


def test_next_cron_run_uses_or_for_restricted_day_fields() -> None:
    after = datetime(2026, 5, 2, 0, 0, 0, tzinfo=timezone.utc)

    result = next_cron_run("0 9 1 * 1", after=after).astimezone()

    assert result.date().isoformat() == "2026-05-04"
    assert (result.hour, result.minute) == (9, 0)


def test_next_cron_run_honors_a_single_restricted_day_field() -> None:
    after = datetime(2026, 5, 2, 0, 0, 0, tzinfo=timezone.utc)

    weekly = next_cron_run("0 9 * * 1", after=after).astimezone()
    monthly = next_cron_run("0 9 1 * *", after=after).astimezone()

    assert weekly.date().isoformat() == "2026-05-04"
    assert monthly.date().isoformat() == "2026-06-01"


def test_loop_task_manager_lifecycle() -> None:
    async def _run() -> None:
        manager = LoopTaskManager()
        now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)
        task = await manager.add(prompt="check status", interval_seconds=60, kind=LoopTaskKind.RECURRING, now=now)

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


def test_loop_task_manager_cron_reschedules() -> None:
    async def _run() -> None:
        manager = LoopTaskManager()
        now = datetime(2026, 5, 23, 5, 30, 0, tzinfo=timezone.utc)
        task = await manager.add(
            prompt="daily report",
            interval_seconds=0,
            kind=LoopTaskKind.CRON,
            schedule_expr="0 6 * * *",
            now=now,
        )
        first_run = task.next_run_at
        await manager.mark_finished(task.id, now=first_run)
        tasks = await manager.list()
        assert len(tasks) == 1
        assert tasks[0].next_run_at > first_run

    asyncio.run(_run())


def test_loop_task_manager_once_task_is_removed_after_run() -> None:
    async def _run() -> None:
        manager = LoopTaskManager()
        now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)
        task = await manager.add(prompt="process trace", interval_seconds=60, kind=LoopTaskKind.ONCE, now=now)

        assert [item.id for item in await manager.due(now=now + timedelta(seconds=60))] == [task.id]
        await manager.mark_finished(task.id, now=now + timedelta(seconds=60))
        assert await manager.list() == []

    asyncio.run(_run())


def test_load_default_loop_prompt(tmp_path: Path) -> None:
    assert load_default_loop_prompt(tmp_path) == ""
    (tmp_path / "loop.md").write_text("review current status\n", encoding="utf-8")
    assert load_default_loop_prompt(tmp_path) == "review current status"
