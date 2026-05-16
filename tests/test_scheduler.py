from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from msagent.scheduler import ScheduledTaskRunner, ScheduledTaskStatus, ScheduledTaskStore, parse_scheduled_time
from msagent.scheduler import tasks as tasks_module
from msagent.scheduler.tasks import _parse_relative, parse_recurring


def test_scheduled_task_store_lifecycle(tmp_path: Path) -> None:
    async def _run() -> None:
        store = ScheduledTaskStore(tmp_path)
        run_at = parse_scheduled_time("2099-01-01 00:00")

        created = await store.add_task(
            prompt="send daily digest",
            run_at=run_at,
            agent="general",
            model="default",
        )

        tasks = await store.list_tasks()
        assert [task.id for task in tasks] == [created.id]
        assert tasks[0].status is ScheduledTaskStatus.PENDING

        cancelled = await store.cancel_task(created.id)
        assert cancelled is True

        tasks = await store.list_tasks()
        assert tasks[0].status is ScheduledTaskStatus.CANCELLED

    asyncio.run(_run())


def test_scheduled_task_runner_executes_due_task(tmp_path: Path, monkeypatch) -> None:
    async def _run() -> None:
        store = ScheduledTaskStore(tmp_path)
        now = tasks_module._utc_now()
        task = await store.add_task(
            prompt="run nightly check",
            run_at=now,
            agent="general",
            model="default",
        )

        context = SimpleNamespace(thread_id="thread-scheduled")

        async def fake_create_context(self, scheduled_task):
            assert scheduled_task.id == task.id
            return context

        async def fake_send_task(self, ctx, message: str) -> int:
            assert ctx is context
            assert message == "run nightly check"
            return 0

        runner = ScheduledTaskRunner(tmp_path)
        monkeypatch.setattr(runner, "_create_context", fake_create_context.__get__(runner, ScheduledTaskRunner))
        monkeypatch.setattr(runner, "_send_task", fake_send_task.__get__(runner, ScheduledTaskRunner))
        processed = await runner.run_due_tasks_once(max_tasks=5)

        assert processed == 1
        tasks = await store.list_tasks()
        assert tasks[0].id == task.id
        assert tasks[0].status is ScheduledTaskStatus.COMPLETED
        assert tasks[0].thread_id == "thread-scheduled"

    asyncio.run(_run())


def test_parse_relative_time() -> None:
    now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)

    cases = [
        ("+5m", timedelta(minutes=5)),
        ("5m", timedelta(minutes=5)),
        ("+1h", timedelta(hours=1)),
        ("+30s", timedelta(seconds=30)),
        ("+1d", timedelta(days=1)),
        ("+1h30m", timedelta(hours=1, minutes=30)),
        ("in 5 minutes", timedelta(minutes=5)),
        ("in 30 seconds", timedelta(seconds=30)),
        ("in 1 hour", timedelta(hours=1)),
        ("in 2 days", timedelta(days=2)),
        ("10min", timedelta(minutes=10)),
    ]

    for text, expected_delta in cases:
        result = _parse_relative(text, now=now)
        assert result is not None, f"'{text}' should parse"
        assert result == now + expected_delta, f"'{text}': expected {now + expected_delta}, got {result}"

    invalid = ["hello", "", "abc123", "2026-05-23 12:00"]
    for text in invalid:
        assert _parse_relative(text, now=now) is None, f"'{text}' should return None"


def test_parse_recurring() -> None:
    now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)

    # Interval-based
    result = parse_recurring("every 5m", now=now)
    assert result is not None
    interval, first_run, label = result
    assert interval == 300
    assert label == "every 5m"
    assert first_run == now + timedelta(minutes=5)

    result = parse_recurring("every 1h", now=now)
    assert result is not None
    assert result[0] == 3600
    assert result[2] == "every 1h"

    result = parse_recurring("every 30s", now=now)
    assert result is not None
    assert result[0] == 30
    assert result[2] == "every 30s"

    result = parse_recurring("every 1d", now=now)
    assert result is not None
    assert result[0] == 86400

    # daily at
    result = parse_recurring("daily at 09:00", now=now)
    assert result is not None
    assert result[0] == 86400
    assert result[2] == "daily at 09:00"
    local_first = result[1].astimezone()
    assert local_first.hour == 9
    assert local_first.minute == 0

    result = parse_recurring("daily 18:30", now=now)
    assert result is not None

    # hourly / daily
    result = parse_recurring("hourly", now=now)
    assert result is not None
    assert result[0] == 3600

    result = parse_recurring("daily", now=now)
    assert result is not None
    assert result[0] == 86400

    # Non-recurring
    assert parse_recurring("+5m", now=now) is None
    assert parse_recurring("2026-05-23 12:00", now=now) is None
    assert parse_recurring("hello", now=now) is None


def test_recurring_task_reschedule(tmp_path: Path, monkeypatch) -> None:
    async def _run() -> None:
        store = ScheduledTaskStore(tmp_path)
        now = tasks_module._utc_now()
        task = await store.add_task(
            prompt="heartbeat",
            run_at=now,
            agent="general",
            model="default",
            recurring="every 1h",
            interval_seconds=3600,
        )

        context = SimpleNamespace(thread_id="thread-x")

        async def fake_create_context(self, scheduled_task):
            return context

        async def fake_send_task(self, ctx, message: str) -> int:
            return 0

        runner = ScheduledTaskRunner(tmp_path)
        monkeypatch.setattr(runner, "_create_context", fake_create_context.__get__(runner, ScheduledTaskRunner))
        monkeypatch.setattr(runner, "_send_task", fake_send_task.__get__(runner, ScheduledTaskRunner))
        processed = await runner.run_due_tasks_once(max_tasks=5)

        assert processed == 1
        tasks = await store.list_tasks()
        # Original completed + new pending task
        assert len(tasks) >= 2
        original = next(t for t in tasks if t.id == task.id)
        assert original.status is ScheduledTaskStatus.COMPLETED
        new_task = next(t for t in tasks if t.id != task.id)
        assert new_task.status is ScheduledTaskStatus.PENDING
        assert new_task.recurring == "every 1h"
        assert new_task.interval_seconds == 3600

    asyncio.run(_run())


def test_delete_and_cleanup(tmp_path: Path) -> None:
    async def _run() -> None:
        store = ScheduledTaskStore(tmp_path)
        now = tasks_module._utc_now()

        t1 = await store.add_task(prompt="task 1", run_at=now, agent="g", model="m")
        t2 = await store.add_task(prompt="task 2", run_at=now, agent="g", model="m")

        # Mark t1 as completed, t2 as failed
        await store.mark_completed(t1.id, thread_id="x")
        await store.mark_failed(t2.id, error="boom")

        # Delete specific task
        deleted = await store.delete_task(t1.id)
        assert deleted is True
        tasks = await store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].id == t2.id

        # Cleanup finished
        count = await store.cleanup_finished_tasks()
        assert count == 1
        tasks = await store.list_tasks()
        assert len(tasks) == 0

    asyncio.run(_run())
