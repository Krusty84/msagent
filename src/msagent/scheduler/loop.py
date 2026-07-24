"""Loop tasks for session."""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

LOOP_DEFAULT_EXPIRE_DAYS = 7
LOOP_MAX_TASKS = 50
LOOP_POLL_INTERVAL_SECONDS = 1.0
LOOP_PROMPT_FILE = "loop.md"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=utc_now().astimezone().tzinfo).astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


def format_loop_time(value: datetime) -> str:
    return ensure_utc(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


_INTERVAL_RE = re.compile(
    r"^(?:every\s+)?(\d+)\s*(s(?:ec(?:ond)?)?s?|m(?:in(?:ute)?)?s?|h(?:our)?s?|d(?:ay)?s?)$",
    re.IGNORECASE,
)
_DAILY_AT_RE = re.compile(r"^daily\s+(\d{1,2}):(\d{2})$", re.IGNORECASE)


def parse_loop_interval(raw: str) -> int:
    """Parse Claude-style loop interval text into seconds."""
    text = raw.strip()
    match = _INTERVAL_RE.fullmatch(text)
    if not match:
        raise ValueError("Loop interval must look like '30s', '5m', '1h', or 'every 1h'")

    amount = int(match.group(1))
    unit = match.group(2).lower()[0]
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    seconds = amount * multiplier
    if seconds <= 0:
        raise ValueError("Loop interval must be greater than zero")
    return seconds


def parse_loop_delay(raw: str) -> int:
    """Parse a one-shot delay such as '10m', 'in 10m', or 'after 1h'."""
    text = raw.strip()
    lowered = text.lower()
    for prefix in ("in ", "after ", "once "):
        if lowered.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    if text.lower().startswith("every "):
        raise ValueError("One-shot delay must not start with 'every'")
    return parse_loop_interval(text)


def load_default_loop_prompt(working_dir: Path) -> str:
    """Load the default loop prompt from loop.md if present."""
    path = Path(working_dir) / LOOP_PROMPT_FILE
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class LoopTaskKind(str, Enum):
    ONCE = "once"
    RECURRING = "recurring"
    CRON = "cron"


def parse_loop_daily(raw: str) -> str:
    """Parse 'daily HH:MM' into a cron expression."""
    text = raw.strip()
    match = _DAILY_AT_RE.fullmatch(text)
    if not match:
        raise ValueError("Daily schedule must look like 'daily 06:00'")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Daily schedule time is out of range")
    return f"{minute} {hour} * * *"


def parse_loop_cron(raw: str) -> str:
    """Validate a 5-field cron expression."""
    expr = raw.strip()
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("Cron schedule must have 5 fields, e.g. '0 6 * * *'")
    return expr


def next_cron_run(expr: str, *, after: datetime | None = None) -> datetime:
    """Compute the next run time for a cron expression in local time, returned as UTC."""
    current = ensure_utc(after or utc_now()).astimezone()
    minute_s, hour_s, dom_s, month_s, dow_s = expr.split()

    def _parse_field(spec: str, minimum: int, maximum: int) -> set[int]:
        allowed: set[int] = set()
        for part in spec.split(","):
            part = part.strip()
            if part == "*":
                allowed.update(range(minimum, maximum + 1))
                continue
            if "/" in part:
                base, step_text = part.split("/", 1)
                step = int(step_text)
            else:
                base, step = part, 1
            if base == "*":
                low, high = minimum, maximum
            elif "-" in base:
                low_s, high_s = base.split("-", 1)
                low, high = int(low_s), int(high_s)
            else:
                low = high = int(base)
            if low < minimum or high > maximum or low > high or step < 1:
                raise ValueError(f"Invalid cron field: {spec}")
            allowed.update(range(low, high + 1, step))
        return allowed

    minutes = _parse_field(minute_s, 0, 59)
    hours = _parse_field(hour_s, 0, 23)
    dom = _parse_field(dom_s, 1, 31)
    month = _parse_field(month_s, 1, 12)
    dow = _parse_field(dow_s, 0, 6)
    dom_restricted = dom_s != "*"
    dow_restricted = dow_s != "*"

    dt = current.replace(second=0, microsecond=0) + timedelta(minutes=1)
    deadline = dt + timedelta(days=366)
    while dt <= deadline:
        cron_dow = (dt.weekday() + 1) % 7
        dom_matches = dt.day in dom
        dow_matches = cron_dow in dow
        # Traditional crontab treats two restricted day fields as alternatives.
        if dom_restricted and dow_restricted:
            day_matches = dom_matches or dow_matches
        else:
            day_matches = dom_matches and dow_matches
        if dt.minute in minutes and dt.hour in hours and day_matches and dt.month in month:
            return dt.astimezone(timezone.utc)
        dt += timedelta(minutes=1)
    raise ValueError(f"No matching time found within one year for cron: {expr}")


@dataclass(slots=True)
class LoopTask:
    id: str
    prompt: str
    kind: LoopTaskKind
    interval_seconds: int
    schedule_expr: str | None
    next_run_at: datetime
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    run_count: int = 0
    last_run_at: datetime | None = None
    last_error: str | None = None

    @property
    def expired(self) -> bool:
        return utc_now() >= self.expires_at


class LoopTaskManager:
    """In-memory session-scoped one-shot and recurring task manager."""

    def __init__(self) -> None:
        self._tasks: dict[str, LoopTask] = {}
        self._lock = asyncio.Lock()

    async def add(
        self,
        *,
        prompt: str,
        interval_seconds: int,
        kind: LoopTaskKind = LoopTaskKind.RECURRING,
        schedule_expr: str | None = None,
        now: datetime | None = None,
    ) -> LoopTask:
        async with self._lock:
            self._expire_locked(now=now)
            if len(self._tasks) >= LOOP_MAX_TASKS:
                raise ValueError(f"Cannot create more than {LOOP_MAX_TASKS} active loop tasks")

            current = ensure_utc(now or utc_now())
            task = LoopTask(
                id=str(uuid.uuid4())[:8],
                prompt=prompt.strip(),
                kind=kind,
                interval_seconds=interval_seconds,
                schedule_expr=schedule_expr,
                next_run_at=(
                    next_cron_run(schedule_expr, after=current)
                    if kind is LoopTaskKind.CRON and schedule_expr is not None
                    else current + timedelta(seconds=interval_seconds)
                ),
                created_at=current,
                updated_at=current,
                expires_at=current + timedelta(days=LOOP_DEFAULT_EXPIRE_DAYS),
            )
            self._tasks[task.id] = task
            return task

    async def list(self) -> list[LoopTask]:
        async with self._lock:
            self._expire_locked()
            return sorted(self._tasks.values(), key=lambda item: item.next_run_at)

    async def cancel(self, task_id: str) -> bool:
        async with self._lock:
            return self._tasks.pop(task_id, None) is not None

    async def clear(self) -> int:
        async with self._lock:
            count = len(self._tasks)
            self._tasks.clear()
            return count

    async def due(self, *, now: datetime | None = None) -> list[LoopTask]:
        async with self._lock:
            current = ensure_utc(now or utc_now())
            self._expire_locked(now=current)
            return [
                task
                for task in sorted(self._tasks.values(), key=lambda item: item.next_run_at)
                if task.next_run_at <= current
            ]

    async def mark_finished(self, task_id: str, *, error: str | None = None, now: datetime | None = None) -> None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            current = ensure_utc(now or utc_now())
            task.run_count += 1
            task.last_run_at = current
            task.last_error = error
            task.updated_at = current
            if task.kind is LoopTaskKind.ONCE:
                self._tasks.pop(task_id, None)
                return
            if task.kind is LoopTaskKind.CRON and task.schedule_expr is not None:
                task.next_run_at = next_cron_run(task.schedule_expr, after=current)
            else:
                task.next_run_at = current + timedelta(seconds=task.interval_seconds)
            self._expire_locked(now=current)

    def _expire_locked(self, *, now: datetime | None = None) -> None:
        current = ensure_utc(now or utc_now())
        expired_ids = [task_id for task_id, task in self._tasks.items() if task.expires_at <= current]
        for task_id in expired_ids:
            self._tasks.pop(task_id, None)
