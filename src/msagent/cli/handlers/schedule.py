"""Slash-command handler for scheduled tasks."""

from __future__ import annotations

import asyncio

from rich.table import Table

from msagent.cli.theme import console
from msagent.scheduler import (
    ScheduledTaskRunner,
    ScheduledTaskStore,
    format_scheduled_time,
    parse_recurring,
    parse_scheduled_time,
)


class ScheduleHandler:
    """Create, inspect, and cancel scheduled tasks from an interactive session."""

    def __init__(self, session) -> None:
        self.session = session

    async def handle(self, args: list[str]) -> None:
        if not args:
            self._print_usage()
            return

        command = args[0].lower()
        if command in {"list", "ls"}:
            await self._list_tasks()
            return
        if command in {"cancel", "rm"}:
            if len(args) < 2:
                console.print_error("Usage: /schedule cancel <task-id>")
                console.print("")
                return
            await self._cancel_task(args[1])
            return
        if command == "delete":
            if len(args) < 2:
                console.print_error("Usage: /schedule delete <task-id>")
                console.print("")
                return
            await self._delete_task(args[1])
            return
        if command == "clean":
            await self._cleanup_tasks()
            return
        if command == "worker-stop":
            self._stop_worker()
            return
        if command == "worker-status":
            self._print_worker_status()
            return

        add_args = args[1:] if command == "add" else args
        await self._add_task(add_args)

    async def _add_task(self, args: list[str]) -> None:
        if not args:
            self._print_usage()
            return

        if args[0] == "--at":
            if len(args) < 3:
                console.print_error('Usage: /schedule add --at "YYYY-MM-DD HH:MM" <task>')
                console.print("")
                return
            raw_time = args[1]
            prompt = " ".join(args[2:]).strip()
        else:
            raw_time = args[0]
            prompt = " ".join(args[1:]).strip()

        if not prompt:
            console.print_error("Scheduled task prompt is required")
            console.print("")
            return

        # Try recurring expression first (e.g. "every 1h", "daily at 09:00")
        recurring_info = parse_recurring(raw_time)
        if recurring_info:
            interval_seconds, run_at, recurring_label = recurring_info
            ctx = self.session.context
            store = ScheduledTaskStore(ctx.working_dir)
            task = await store.add_task(
                prompt=prompt,
                run_at=run_at,
                agent=ctx.agent,
                model=ctx.model,
                recurring=recurring_label,
                interval_seconds=interval_seconds,
            )
            console.print_success(
                f"Recurring task {task.id} [{recurring_label}] "
                f"next run at {format_scheduled_time(task.run_at)} using agent={task.agent}, model={task.model}"
            )
            console.print("")
            self._ensure_worker_running()
            return

        run_at = parse_scheduled_time(raw_time)
        ctx = self.session.context
        store = ScheduledTaskStore(ctx.working_dir)
        task = await store.add_task(
            prompt=prompt,
            run_at=run_at,
            agent=ctx.agent,
            model=ctx.model,
        )
        console.print_success(
            f"Scheduled task {task.id} for {format_scheduled_time(task.run_at)} using agent={task.agent}, model={task.model}"
        )
        console.print("")

        self._ensure_worker_running()

    async def _list_tasks(self) -> None:
        store = ScheduledTaskStore(self.session.context.working_dir)
        tasks = await store.list_tasks(include_finished=True, limit=20)
        if not tasks:
            console.print_warning("No scheduled tasks found")
            console.print("")
            return

        table = Table(title="Scheduled Tasks")
        table.add_column("ID", style="cyan")
        table.add_column("Run At", style="green")
        table.add_column("Recurring", style="magenta")
        table.add_column("Status", style="yellow")
        table.add_column("Prompt", style="default")
        for task in tasks:
            preview = task.prompt.replace("\n", " ").strip()
            if len(preview) > 48:
                preview = f"{preview[:45]}..."
            recurring_display = task.recurring or "-"
            table.add_row(
                task.id,
                format_scheduled_time(task.run_at),
                recurring_display,
                task.status.value,
                preview,
            )
        console.print(table)
        console.print("")

    async def _cancel_task(self, task_id: str) -> None:
        store = ScheduledTaskStore(self.session.context.working_dir)
        cancelled = await store.cancel_task(task_id)
        if not cancelled:
            console.print_error("Task not found or no longer pending")
            console.print("")
            return
        console.print_success(f"Cancelled scheduled task {task_id}")
        console.print("")

    async def _delete_task(self, task_id: str) -> None:
        store = ScheduledTaskStore(self.session.context.working_dir)
        deleted = await store.delete_task(task_id)
        if not deleted:
            console.print_error("Task not found")
            console.print("")
            return
        console.print_success(f"Deleted task {task_id}")
        console.print("")

    async def _cleanup_tasks(self) -> None:
        store = ScheduledTaskStore(self.session.context.working_dir)
        count = await store.cleanup_finished_tasks()
        if count == 0:
            console.print_warning("No finished tasks to clean up")
        else:
            console.print_success(f"Cleaned up {count} finished task(s)")
        console.print("")

    def _ensure_worker_running(self) -> None:
        if self.session._schedule_worker_task and not self.session._schedule_worker_task.done():
            return

        runner = ScheduledTaskRunner(self.session.context.working_dir)

        async def _poll() -> None:
            try:
                await runner.run_forever(poll_interval=15.0, max_tasks_per_tick=10)
            except asyncio.CancelledError:
                pass

        self.session._schedule_worker_task = asyncio.create_task(_poll())
        console.print("Schedule worker started (auto-poll every 15s)", style="info")
        console.print("")

    def _stop_worker(self) -> None:
        task = self.session._schedule_worker_task
        if task and not task.done():
            task.cancel()
            console.print_success("Schedule worker stopped")
        else:
            console.print_warning("Schedule worker is not running")
        console.print("")

    def _print_worker_status(self) -> None:
        task = self.session._schedule_worker_task
        if task and not task.done():
            console.print("Schedule worker is running (poll interval: 15s)", style="info")
        else:
            console.print_warning("Schedule worker is not running")
        console.print("")

    @staticmethod
    def _print_usage() -> None:
        console.print("Usage:", style="primary")
        console.print('/schedule "2026-05-16 00:00" <task>')
        console.print('/schedule add --at "2026-05-16 00:00" <task>')
        console.print('/schedule "+5m" <task>')
        console.print('/schedule every 1h <task>')
        console.print('/schedule "daily at 09:00" <task>')
        console.print("/schedule list")
        console.print("/schedule cancel <task-id>")
        console.print("/schedule delete <task-id>")
        console.print("/schedule clean")
        console.print("/schedule worker-stop")
        console.print("/schedule worker-status")
        console.print("")
