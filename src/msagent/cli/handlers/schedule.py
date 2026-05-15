"""Slash-command handler for scheduled tasks."""

from __future__ import annotations

from rich.table import Table

from msagent.cli.theme import console
from msagent.scheduler import ScheduledTaskRunner, ScheduledTaskStore, format_scheduled_time, parse_scheduled_time


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
        if command in {"cancel", "rm", "delete"}:
            if len(args) < 2:
                console.print_error("Usage: /schedule cancel <task-id>")
                console.print("")
                return
            await self._cancel_task(args[1])
            return
        if command == "run-due":
            await self._run_due_tasks()
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
        table.add_column("Status", style="yellow")
        table.add_column("Prompt", style="default")
        for task in tasks:
            preview = task.prompt.replace("\n", " ").strip()
            if len(preview) > 48:
                preview = f"{preview[:45]}..."
            table.add_row(task.id, format_scheduled_time(task.run_at), task.status.value, preview)
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

    async def _run_due_tasks(self) -> None:
        runner = ScheduledTaskRunner(self.session.context.working_dir)
        processed = await runner.run_due_tasks_once(max_tasks=10)
        console.print_success(f"Processed {processed} due scheduled task(s)")
        console.print("")

    @staticmethod
    def _print_usage() -> None:
        console.print("Usage:", style="primary")
        console.print('/schedule "2026-05-16 00:00" <task>')
        console.print('/schedule add --at "2026-05-16 00:00" <task>')
        console.print("/schedule list")
        console.print("/schedule cancel <task-id>")
        console.print("/schedule run-due")
        console.print("")
