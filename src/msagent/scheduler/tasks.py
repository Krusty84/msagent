"""Scheduled task storage and execution."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from msagent.core.constants import CONFIG_SCHEDULES_DB_FILE_NAME
from msagent.core.logging import get_logger

logger = get_logger(__name__)

_SCHEDULE_TIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
)


class ScheduledTaskStatus(str, Enum):
    """Lifecycle states for a scheduled task."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class ScheduledTask:
    """Persisted scheduled task metadata."""

    id: str
    prompt: str
    run_at: datetime
    status: ScheduledTaskStatus
    agent: str
    model: str
    thread_id: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_error: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_utc_now().astimezone().tzinfo).astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_scheduled_time(raw: str, *, now: datetime | None = None) -> datetime:
    """Parse a user-provided scheduled time into UTC."""
    text = raw.strip()
    if not text:
        raise ValueError("Scheduled time is required")

    normalized = text.replace("Z", "+00:00")
    parsed: datetime | None = None

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None

    if parsed is None:
        for fmt in _SCHEDULE_TIME_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        parsed = _parse_relative(text, now=now)

    if parsed is None:
        raise ValueError(
            "Invalid time format. Use 'YYYY-MM-DD HH:MM', an ISO datetime such as "
            "'2026-05-15T23:00:00+08:00', or a relative time like '+5m', '+1h', 'in 5 minutes'."
        )

    parsed_utc = _ensure_utc(parsed)
    current = now.astimezone(timezone.utc) if now is not None else _utc_now()
    if parsed_utc <= current:
        raise ValueError("Scheduled time must be in the future")
    return parsed_utc


_RELATIVE_PATTERN = re.compile(
    r"(?:in\s+)?\+?\s*(\d+)\s*(s(?:ec(?:ond)?)?s?|m(?:in(?:ute)?)?s?|h(?:our)?s?|d(?:ay)?s?)(?![a-zA-Z])",
    re.IGNORECASE,
)


def _parse_relative(text: str, *, now: datetime | None = None) -> datetime | None:
    """Parse relative time expressions like '+5m', 'in 30 seconds', '1h'."""
    total_seconds = 0.0
    cursor = 0

    for match in _RELATIVE_PATTERN.finditer(text):
        if match.start() != cursor:
            break
        cursor = match.end()
        value = int(match.group(1))
        unit = match.group(2).lower()[0]

        if unit == "s":
            total_seconds += value
        elif unit == "m":
            total_seconds += value * 60
        elif unit == "h":
            total_seconds += value * 3600
        elif unit == "d":
            total_seconds += value * 86400

    if total_seconds <= 0:
        return None

    remaining = text[cursor:].strip()
    if remaining:
        # Recurse to parse remaining components (e.g. "1h30m")
        extra = _parse_relative(remaining, now=now)
        if extra is None:
            return None
        return extra + timedelta(seconds=total_seconds)

    current = now if now is not None else _utc_now()
    return current + timedelta(seconds=total_seconds)


def format_scheduled_time(value: datetime) -> str:
    """Format a UTC timestamp into local wall clock time."""
    return _ensure_utc(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


class ScheduledTaskStore:
    """SQLite-backed scheduled task repository scoped to a working directory."""

    def __init__(self, working_dir: Path):
        self.working_dir = Path(working_dir)
        self.db_path = self.working_dir / CONFIG_SCHEDULES_DB_FILE_NAME

    async def ensure_initialized(self) -> None:
        await asyncio.to_thread(self._ensure_initialized_sync)

    def _ensure_initialized_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id TEXT PRIMARY KEY,
                    prompt TEXT NOT NULL,
                    run_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    model TEXT NOT NULL,
                    thread_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    last_error TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_status_run_at
                ON scheduled_tasks (status, run_at)
                """
            )
            conn.commit()

    async def add_task(
        self,
        *,
        prompt: str,
        run_at: datetime,
        agent: str,
        model: str,
    ) -> ScheduledTask:
        await self.ensure_initialized()
        now = _utc_now()
        task = ScheduledTask(
            id=str(uuid.uuid4()),
            prompt=prompt.strip(),
            run_at=_ensure_utc(run_at),
            status=ScheduledTaskStatus.PENDING,
            agent=agent,
            model=model,
            thread_id=None,
            created_at=now,
            updated_at=now,
        )
        await asyncio.to_thread(self._insert_task_sync, task)
        return task

    def _insert_task_sync(self, task: ScheduledTask) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO scheduled_tasks (
                    id, prompt, run_at, status, agent, model, thread_id,
                    created_at, updated_at, started_at, completed_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.prompt,
                    task.run_at.isoformat(),
                    task.status.value,
                    task.agent,
                    task.model,
                    task.thread_id,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                    None,
                    None,
                    None,
                ),
            )
            conn.commit()

    async def list_tasks(
        self,
        *,
        include_finished: bool = True,
        limit: int = 50,
    ) -> list[ScheduledTask]:
        await self.ensure_initialized()
        rows = await asyncio.to_thread(self._list_tasks_sync, include_finished, limit)
        return [self._row_to_task(row) for row in rows]

    def _list_tasks_sync(self, include_finished: bool, limit: int) -> list[sqlite3.Row]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if include_finished:
                cursor = conn.execute(
                    """
                    SELECT * FROM scheduled_tasks
                    ORDER BY
                        CASE status
                            WHEN 'pending' THEN 0
                            WHEN 'running' THEN 1
                            WHEN 'failed' THEN 2
                            WHEN 'completed' THEN 3
                            WHEN 'cancelled' THEN 4
                            ELSE 5
                        END,
                        run_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM scheduled_tasks
                    WHERE status IN (?, ?)
                    ORDER BY run_at ASC
                    LIMIT ?
                    """,
                    (ScheduledTaskStatus.PENDING.value, ScheduledTaskStatus.RUNNING.value, limit),
                )
            return cursor.fetchall()

    async def cancel_task(self, task_id: str) -> bool:
        await self.ensure_initialized()
        return await asyncio.to_thread(self._cancel_task_sync, task_id)

    def _cancel_task_sync(self, task_id: str) -> bool:
        now = _utc_now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE scheduled_tasks
                SET status = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    ScheduledTaskStatus.CANCELLED.value,
                    now,
                    task_id,
                    ScheduledTaskStatus.PENDING.value,
                ),
            )
            conn.commit()
            return (cursor.rowcount or 0) > 0

    async def claim_due_task(self) -> ScheduledTask | None:
        await self.ensure_initialized()
        return await asyncio.to_thread(self._claim_due_task_sync)

    def _claim_due_task_sync(self) -> ScheduledTask | None:
        now = _utc_now()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE status = ? AND run_at <= ?
                ORDER BY run_at ASC
                LIMIT 1
                """,
                (ScheduledTaskStatus.PENDING.value, now.isoformat()),
            ).fetchone()
            if row is None:
                conn.commit()
                return None

            update = conn.execute(
                """
                UPDATE scheduled_tasks
                SET status = ?, started_at = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    ScheduledTaskStatus.RUNNING.value,
                    now.isoformat(),
                    now.isoformat(),
                    row["id"],
                    ScheduledTaskStatus.PENDING.value,
                ),
            )
            if (update.rowcount or 0) == 0:
                conn.rollback()
                return None

            conn.commit()

        task = self._row_to_task(row)
        task.status = ScheduledTaskStatus.RUNNING
        task.started_at = now
        task.updated_at = now
        return task

    async def mark_completed(self, task_id: str, *, thread_id: str | None) -> None:
        await self._mark_final(
            task_id,
            status=ScheduledTaskStatus.COMPLETED,
            thread_id=thread_id,
            last_error=None,
        )

    async def mark_failed(self, task_id: str, *, error: str, thread_id: str | None = None) -> None:
        await self._mark_final(
            task_id,
            status=ScheduledTaskStatus.FAILED,
            thread_id=thread_id,
            last_error=error,
        )

    async def _mark_final(
        self,
        task_id: str,
        *,
        status: ScheduledTaskStatus,
        thread_id: str | None,
        last_error: str | None,
    ) -> None:
        await self.ensure_initialized()
        await asyncio.to_thread(self._mark_final_sync, task_id, status, thread_id, last_error)

    def _mark_final_sync(
        self,
        task_id: str,
        status: ScheduledTaskStatus,
        thread_id: str | None,
        last_error: str | None,
    ) -> None:
        now = _utc_now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE scheduled_tasks
                SET status = ?, thread_id = ?, completed_at = ?, updated_at = ?, last_error = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    thread_id,
                    now,
                    now,
                    last_error,
                    task_id,
                ),
            )
            conn.commit()

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> ScheduledTask:
        return ScheduledTask(
            id=str(row["id"]),
            prompt=str(row["prompt"]),
            run_at=datetime.fromisoformat(str(row["run_at"])).astimezone(timezone.utc),
            status=ScheduledTaskStatus(str(row["status"])),
            agent=str(row["agent"]),
            model=str(row["model"]),
            thread_id=row["thread_id"],
            created_at=datetime.fromisoformat(str(row["created_at"])).astimezone(timezone.utc),
            updated_at=datetime.fromisoformat(str(row["updated_at"])).astimezone(timezone.utc),
            started_at=(
                datetime.fromisoformat(str(row["started_at"])).astimezone(timezone.utc) if row["started_at"] else None
            ),
            completed_at=(
                datetime.fromisoformat(str(row["completed_at"])).astimezone(timezone.utc)
                if row["completed_at"]
                else None
            ),
            last_error=(str(row["last_error"]) if row["last_error"] is not None else None),
        )


class ScheduledTaskRunner:
    """Execute due scheduled tasks by sending them through msAgent one-shot sessions."""

    def __init__(self, working_dir: Path):
        self.working_dir = Path(working_dir)
        self.store = ScheduledTaskStore(self.working_dir)

    async def run_due_tasks_once(self, *, max_tasks: int = 10) -> int:
        processed = 0
        while processed < max_tasks:
            task = await self.store.claim_due_task()
            if task is None:
                break
            processed += 1
            await self._execute_task(task)
        return processed

    async def run_forever(self, *, poll_interval: float = 30.0, max_tasks_per_tick: int = 10) -> None:
        while True:
            processed = await self.run_due_tasks_once(max_tasks=max_tasks_per_tick)
            if processed == 0:
                await asyncio.sleep(poll_interval)

    async def _execute_task(self, task: ScheduledTask) -> None:
        context = None
        try:
            context = await self._create_context(task)
            exit_code = await self._send_task(context, task.prompt)
            if exit_code == 0:
                await self.store.mark_completed(task.id, thread_id=context.thread_id)
                logger.info("Scheduled task completed: %s", task.id)
                return

            await self.store.mark_failed(
                task.id,
                error=f"msagent exited with status {exit_code}",
                thread_id=context.thread_id,
            )
            logger.warning("Scheduled task failed with exit code %s: %s", exit_code, task.id)
        except Exception as exc:
            thread_id = context.thread_id if context is not None else None
            await self.store.mark_failed(
                task.id,
                error=str(exc) or type(exc).__name__,
                thread_id=thread_id,
            )
            logger.exception("Scheduled task crashed: %s", task.id, exc_info=exc)

    async def _create_context(self, task: ScheduledTask):
        from msagent.cli.core.context import Context
        from msagent.configs.approval import ApprovalMode

        return await Context.create(
            agent=task.agent,
            model=task.model,
            approval_mode=ApprovalMode.ACTIVE,
            working_dir=self.working_dir,
            stream_output=False,
        )

    async def _send_task(self, context, prompt: str) -> int:
        from msagent.cli.core.session import Session

        session = Session(context)
        return await session.send(prompt)
