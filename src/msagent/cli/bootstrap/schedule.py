"""CLI entrypoints for scheduled task management."""

from __future__ import annotations

from rich.table import Table

from msagent.cli.theme import console
from msagent.scheduler import (
    ScheduledTaskRunner,
    ScheduledTaskStore,
    format_scheduled_time,
    parse_scheduled_time,
)


async def handle_schedule_command(args) -> int:
    """Handle persistent scheduled task commands."""
    store = ScheduledTaskStore(args.working_dir)

    if args.schedule_command == "add":
        run_at = parse_scheduled_time(args.at)
        task = await store.add_task(
            prompt=" ".join(args.prompt).strip(),
            run_at=run_at,
            agent=args.agent or "general",
            model=args.model or "default",
        )
        console.print_success(
            f"Scheduled task {task.id} for {format_scheduled_time(task.run_at)} using agent={task.agent}, model={task.model}"
        )
        return 0

    if args.schedule_command == "list":
        tasks = await store.list_tasks(include_finished=args.all, limit=args.limit)
        if not tasks:
            console.print_warning("No scheduled tasks found")
            console.print("")
            return 0

        table = Table(title="Scheduled Tasks")
        table.add_column("ID", style="cyan")
        table.add_column("Run At", style="green")
        table.add_column("Status", style="yellow")
        table.add_column("Agent", style="blue")
        table.add_column("Model", style="magenta")
        table.add_column("Prompt", style="default")
        for task in tasks:
            prompt_preview = task.prompt.replace("\n", " ").strip()
            if len(prompt_preview) > 60:
                prompt_preview = f"{prompt_preview[:57]}..."
            table.add_row(
                task.id,
                format_scheduled_time(task.run_at),
                task.status.value,
                task.agent,
                task.model,
                prompt_preview,
            )
        console.print(table)
        return 0

    if args.schedule_command == "cancel":
        cancelled = await store.cancel_task(args.task_id)
        if not cancelled:
            console.print_error("Task not found or no longer pending")
            console.print("")
            return 1
        console.print_success(f"Cancelled scheduled task {args.task_id}")
        return 0

    runner = ScheduledTaskRunner(args.working_dir)
    if args.schedule_command == "run-due":
        processed = await runner.run_due_tasks_once(max_tasks=args.max_tasks)
        console.print_success(f"Processed {processed} due scheduled task(s)")
        return 0

    if args.schedule_command == "worker":
        console.print(
            f"Starting schedule worker in {args.working_dir} with poll interval {args.poll_interval:.1f}s"
        )
        console.print("")
        await runner.run_forever(
            poll_interval=args.poll_interval,
            max_tasks_per_tick=args.max_tasks,
        )
        return 0

    console.print_error("Missing schedule subcommand. Use add, list, cancel, run-due, or worker.")
    console.print("")
    return 1
