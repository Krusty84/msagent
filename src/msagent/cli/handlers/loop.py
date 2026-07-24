"""Loop task for slash command."""

from __future__ import annotations

from rich.table import Table

from msagent.cli.theme import console
from msagent.scheduler import (
    LOOP_DEFAULT_EXPIRE_DAYS,
    LOOP_MAX_TASKS,
    LOOP_PROMPT_FILE,
    LoopTaskKind,
    format_loop_time,
    load_default_loop_prompt,
    parse_loop_cron,
    parse_loop_delay,
    parse_loop_daily,
    parse_loop_interval,
)


class LoopHandler:
    """Manage session-scoped one-shot and recurring loop tasks."""

    def __init__(self, session) -> None:
        self.session = session

    async def handle(self, args: list[str]) -> None:
        if not args:
            self._print_usage()
            return

        command = args[0].lower()
        if command in {"list", "ls", "status"}:
            await self._list_tasks()
            return
        if command in {"cancel", "rm", "delete"}:
            if len(args) < 2:
                console.print_error("Usage: /loop cancel <task-id>")
                console.print("")
                return
            await self._cancel_task(args[1])
            return
        if command in {"stop", "clear"}:
            await self._clear_tasks()
            return

        await self._add_task(args)

    async def _add_task(self, args: list[str]) -> None:
        task_kind, schedule_text, prompt_parts = self._parse_create_args(args)
        if not schedule_text:
            self._print_usage()
            return

        try:
            schedule_expr = None
            interval_seconds = 0
            if task_kind is LoopTaskKind.RECURRING:
                interval_seconds = parse_loop_interval(schedule_text)
            elif task_kind is LoopTaskKind.ONCE:
                interval_seconds = parse_loop_delay(schedule_text)
            else:
                schedule_expr = (
                    parse_loop_daily(schedule_text)
                    if schedule_text.lower().startswith("daily ")
                    else parse_loop_cron(schedule_text)
                )
        except ValueError as exc:
            console.print_error(str(exc))
            console.print("")
            return

        prompt = " ".join(prompt_parts).strip()
        if not prompt:
            prompt = load_default_loop_prompt(self.session.context.working_dir)
        if not prompt:
            console.print_error(
                f"Loop task prompt is required. Provide text after the interval or create {LOOP_PROMPT_FILE}."
            )
            console.print("")
            return

        try:
            task = await self.session.loop_tasks.add(
                prompt=prompt,
                interval_seconds=interval_seconds,
                kind=task_kind,
                schedule_expr=schedule_expr,
            )
        except ValueError as exc:
            console.print_error(str(exc))
            console.print("")
            return

        schedule_label = (
            f"cron {schedule_expr}"
            if task_kind is LoopTaskKind.CRON and schedule_expr is not None
            else (
                f"every {self._format_interval(interval_seconds)}"
                if task_kind is LoopTaskKind.RECURRING
                else f"in {self._format_interval(interval_seconds)}"
            )
        )
        console.print_success(
            f"Loop task {task.id} scheduled {schedule_label}; next run at {format_loop_time(task.next_run_at)}"
        )
        console.print(
            f"[muted]Expires after {LOOP_DEFAULT_EXPIRE_DAYS} days. Active loop tasks are session-scoped.[/muted]"
        )
        console.print("")

    @staticmethod
    def _parse_create_args(args: list[str]) -> tuple[LoopTaskKind, str, list[str]]:
        if not args:
            return LoopTaskKind.ONCE, "", []
        if args[0].lower() == "cron":
            if len(args) < 2:
                return LoopTaskKind.CRON, "", []
            return LoopTaskKind.CRON, args[1], args[2:]
        if args[0].lower() == "daily":
            if len(args) < 2:
                return LoopTaskKind.CRON, "", []
            return LoopTaskKind.CRON, f"daily {args[1]}", args[2:]
        if args[0].lower() == "every":
            if len(args) < 2:
                return LoopTaskKind.RECURRING, "", []
            return LoopTaskKind.RECURRING, f"every {args[1]}", args[2:]
        if args[0].lower() in {"once", "in", "after"}:
            if len(args) < 2:
                return LoopTaskKind.ONCE, "", []
            return LoopTaskKind.ONCE, f"{args[0]} {args[1]}", args[2:]
        return LoopTaskKind.ONCE, args[0], args[1:]

    async def _list_tasks(self) -> None:
        tasks = await self.session.loop_tasks.list()
        if not tasks:
            console.print_warning("No active loop tasks")
            console.print("")
            return

        table = Table(title="Loop Tasks")
        table.add_column("ID", style="cyan")
        table.add_column("Type", style="blue")
        table.add_column("Schedule", style="green")
        table.add_column("Next Run", style="yellow")
        table.add_column("Runs", style="magenta")
        table.add_column("Expires", style="blue")
        table.add_column("Prompt", style="default")

        for task in tasks:
            preview = task.prompt.replace("\n", " ").strip()
            if len(preview) > 54:
                preview = f"{preview[:51]}..."
            table.add_row(
                task.id,
                task.kind.value,
                task.schedule_expr or self._format_interval(task.interval_seconds),
                format_loop_time(task.next_run_at),
                str(task.run_count),
                format_loop_time(task.expires_at),
                preview,
            )
        console.print(table)
        console.print("")

    async def _cancel_task(self, task_id: str) -> None:
        cancelled = await self.session.loop_tasks.cancel(task_id)
        if not cancelled:
            console.print_error("Loop task not found")
            console.print("")
            return
        console.print_success(f"Cancelled loop task {task_id}")
        console.print("")

    async def _clear_tasks(self) -> None:
        count = await self.session.loop_tasks.clear()
        if count == 0:
            console.print_warning("No active loop tasks")
        else:
            console.print_success(f"Stopped {count} loop task(s)")
        console.print("")

    @staticmethod
    def _format_interval(seconds: int) -> str:
        if seconds % 86400 == 0:
            return f"{seconds // 86400}d"
        if seconds % 3600 == 0:
            return f"{seconds // 3600}h"
        if seconds % 60 == 0:
            return f"{seconds // 60}m"
        return f"{seconds}s"

    @staticmethod
    def _print_usage() -> None:
        console.print("Usage:", style="primary")
        console.print("/loop in 10m <task>")
        console.print("/loop once 10m <task>")
        console.print("/loop 10m <task>")
        console.print("/loop every 5m <task>")
        console.print("/loop 1h <task>       # one-shot; same as /loop in 1h")
        console.print('/loop cron "0 6 * * *" <task>')
        console.print("/loop daily 06:00 <task>")
        console.print(f"/loop every 5m        # uses {LOOP_PROMPT_FILE} if present")
        console.print("/loop list")
        console.print("/loop cancel <task-id>")
        console.print("/loop stop")
        console.print(
            f"[muted]Loop tasks are session-scoped, checked while idle, capped at {LOOP_MAX_TASKS}, "
            f"and expire after {LOOP_DEFAULT_EXPIRE_DAYS} days.[/muted]"
        )
        console.print("")
