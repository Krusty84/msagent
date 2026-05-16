from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from msagent.scheduler import ScheduledTaskRunner, ScheduledTaskStatus, ScheduledTaskStore, parse_scheduled_time
from msagent.scheduler import tasks as tasks_module


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
