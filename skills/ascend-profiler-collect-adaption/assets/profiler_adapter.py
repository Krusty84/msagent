"""Framework-neutral torch_npu profiler lifecycle adapter.

Copy this module into the target framework and wire one controller into each
process that actually executes NPU work. Profiling is disabled by default.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from threading import Lock, RLock
from types import TracebackType
from typing import Any, ClassVar

_INTEGER_PATTERN = re.compile(r"[+-]?\d+")
_LAUNCHER_ENVIRONMENTS = (
    ("RANK", "WORLD_SIZE"),
    ("OMPI_COMM_WORLD_RANK", "OMPI_COMM_WORLD_SIZE"),
    ("PMI_RANK", "PMI_SIZE"),
    ("SLURM_PROCID", "SLURM_NTASKS"),
)


def _parse_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
        raise ValueError(f"{name} must be 'true' or 'false', got {value!r}")
    raise TypeError(f"{name} must be a boolean or 'true'/'false', got {value!r}")


def _parse_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER_PATTERN.fullmatch(value.strip()):
        return int(value.strip())
    if isinstance(value, str):
        raise TypeError(f"{name} must be an integer string, got {value!r}")
    raise TypeError(f"{name} must be an integer, got {value!r}")


def _parse_string(name: str, value: Any, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        expected = "a non-empty string or null" if optional else "a non-empty string"
        raise TypeError(f"{name} must be {expected}, got {value!r}")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value.strip()


def _parse_ranks(value: Any) -> tuple[int, ...]:
    if isinstance(value, str):
        parts: Any = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = value
    else:
        raise TypeError(
            "ranks must be a list/tuple or comma-separated integer string, "
            f"got {value!r}"
        )
    if not parts:
        raise ValueError("ranks must not be empty")
    ranks = tuple(_parse_int("ranks entry", rank) for rank in parts)
    if len(set(ranks)) != len(ranks):
        raise ValueError(f"ranks must not contain duplicates, got {ranks!r}")
    if any(rank < -1 for rank in ranks):
        raise ValueError(f"ranks entries must be >= 0 or -1, got {ranks!r}")
    if -1 in ranks and ranks != (-1,):
        raise ValueError("rank wildcard -1 must be the only ranks entry")
    return ranks


def _read_launcher_environment() -> tuple[int | None, int | None]:
    """Read one launcher namespace without conflating unrelated launchers."""
    for rank_name, world_size_name in _LAUNCHER_ENVIRONMENTS:
        rank_text = os.environ.get(rank_name, "").strip()
        world_size_text = os.environ.get(world_size_name, "").strip()
        if rank_text or world_size_text:
            rank = _parse_int(rank_name, rank_text) if rank_text else None
            world_size = (
                _parse_int(world_size_name, world_size_text)
                if world_size_text
                else None
            )
            return rank, world_size
    return None, None


def _resolve_rank(explicit_rank: int | None, enabled: bool) -> int | None:
    if not enabled:
        return explicit_rank
    environment_rank, world_size = _read_launcher_environment()
    resolved_rank = explicit_rank if explicit_rank is not None else environment_rank
    if world_size is not None and world_size < 1:
        raise ValueError(f"world size must be >= 1, got {world_size}")
    if (
        resolved_rank is None
        and world_size in (None, 1)
        and os.environ.get("LOCAL_RANK", "").strip()
    ):
        resolved_rank = _parse_int("LOCAL_RANK", os.environ["LOCAL_RANK"])
    if resolved_rank is None and world_size is not None and world_size > 1:
        raise ValueError(
            "profiling is enabled in a multi-process environment but rank is unknown; "
            "pass profiler rank explicitly or expose a standard rank environment variable"
        )
    if resolved_rank is None:
        resolved_rank = 0
    if world_size is not None and resolved_rank >= world_size:
        raise ValueError(
            f"rank {resolved_rank} must be smaller than world size {world_size}"
        )
    return resolved_rank


@dataclass(frozen=True)
class ProfilerConfig:
    enabled: bool = False
    output_dir: str = "./profiler_output"
    start_step: int = 0
    wait: int = 0
    warmup: int = 0
    active: int = 1
    repeat: int = 1
    rank: int | None = None
    ranks: tuple[int, ...] = (0,)
    level: str = "level0"
    export_type: str = "text"
    with_cpu: bool = True
    record_shapes: bool = False
    profile_memory: bool = False
    with_stack: bool = False
    data_simplification: bool = True
    worker_name: str | None = None

    def __post_init__(self) -> None:
        boolean_fields = (
            "enabled",
            "with_cpu",
            "record_shapes",
            "profile_memory",
            "with_stack",
            "data_simplification",
        )
        for name in boolean_fields:
            if not isinstance(getattr(self, name), bool):
                raise TypeError(
                    f"{name} must be a boolean, got {getattr(self, name)!r}"
                )
        integer_fields = ("start_step", "wait", "warmup", "active", "repeat")
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer, got {value!r}")
            minimum = 1 if name in {"active", "repeat"} else 0
            if value < minimum:
                raise ValueError(f"{name} must be >= {minimum}, got {value}")
        if not isinstance(self.output_dir, str):
            raise TypeError(f"output_dir must be a string, got {self.output_dir!r}")
        if not self.output_dir.strip():
            raise ValueError("output_dir must be a non-empty string")
        if not isinstance(self.level, str):
            raise TypeError(f"level must be a string, got {self.level!r}")
        if not isinstance(self.export_type, str):
            raise TypeError(f"export_type must be a string, got {self.export_type!r}")
        if self.worker_name is not None and not isinstance(self.worker_name, str):
            raise TypeError("worker_name must be a non-empty string or null")
        if isinstance(self.worker_name, str) and not self.worker_name.strip():
            raise ValueError("worker_name must not be empty")
        if not isinstance(self.ranks, tuple):
            raise TypeError("ranks must be a tuple")
        if not self.ranks:
            raise ValueError("ranks must be a non-empty tuple")
        if any(
            isinstance(rank, bool) or not isinstance(rank, int) for rank in self.ranks
        ):
            raise TypeError(f"ranks entries must be integers, got {self.ranks!r}")
        if len(set(self.ranks)) != len(self.ranks):
            raise ValueError(f"ranks must not contain duplicates, got {self.ranks!r}")
        if any(rank < -1 for rank in self.ranks):
            raise ValueError(f"ranks entries must be >= 0 or -1, got {self.ranks!r}")
        if -1 in self.ranks and self.ranks != (-1,):
            raise ValueError("rank wildcard -1 must be the only ranks entry")
        if self.rank is not None and (
            isinstance(self.rank, bool) or not isinstance(self.rank, int)
        ):
            raise TypeError(f"rank must be an integer or null, got {self.rank!r}")
        resolved_rank = _resolve_rank(self.rank, self.enabled)
        if resolved_rank is not None and resolved_rank < 0:
            raise ValueError(f"rank must be >= 0, got {resolved_rank}")
        object.__setattr__(self, "rank", resolved_rank)
        if self.level.lower() not in {"level0", "level1", "level2", "level_none"}:
            raise ValueError(f"unsupported profiler level: {self.level}")
        if self.export_type.lower() not in {"text", "db"}:
            raise ValueError(f"unsupported export type: {self.export_type}")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> ProfilerConfig:
        if values is None:
            return cls()
        if not isinstance(values, Mapping):
            raise TypeError(f"profiler config must be a mapping, got {values!r}")
        invalid_keys = [key for key in values if not isinstance(key, str)]
        if invalid_keys:
            raise TypeError(
                f"profiler config field names must be strings, got {invalid_keys!r}"
            )
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(f"unknown profiler config fields: {', '.join(unknown)}")
        normalized = dict(values)
        for name in (
            "enabled",
            "with_cpu",
            "record_shapes",
            "profile_memory",
            "with_stack",
            "data_simplification",
        ):
            if name in normalized:
                normalized[name] = _parse_bool(name, normalized[name])
        for name in ("start_step", "wait", "warmup", "active", "repeat"):
            if name in normalized:
                normalized[name] = _parse_int(name, normalized[name])
        if "rank" in normalized and normalized["rank"] is not None:
            normalized["rank"] = _parse_int("rank", normalized["rank"])
        if "ranks" in normalized:
            normalized["ranks"] = _parse_ranks(normalized["ranks"])
        for name in ("output_dir", "level", "export_type"):
            if name in normalized:
                normalized[name] = _parse_string(name, normalized[name])
        if "worker_name" in normalized:
            normalized["worker_name"] = _parse_string(
                "worker_name", normalized["worker_name"], optional=True
            )
        return cls(**normalized)

    @property
    def required_steps(self) -> int:
        return self.start_step + (self.wait + self.warmup + self.active) * self.repeat

    @property
    def active_on_this_rank(self) -> bool:
        return self.enabled and (-1 in self.ranks or self.rank in self.ranks)


class ProfilerController:
    """Own exactly one profiler lifecycle in the current execution process."""

    _active_lock: ClassVar[Lock] = Lock()
    _active_controller: ClassVar[ProfilerController | None] = None

    def __init__(
        self, config: ProfilerConfig, profiler_module: Any | None = None
    ) -> None:
        self.config = config
        self._profiler_module = profiler_module
        self._profiler: Any | None = None
        self._lifecycle_lock = RLock()
        self._state = "idle"
        self.steps = 0

    def _load_profiler_module(self) -> Any:
        if self._profiler_module is None:
            import torch_npu  # Imported only for an enabled profiling rank.

            self._profiler_module = torch_npu.profiler
        return self._profiler_module

    @staticmethod
    def _enum_value(enum: Any, name: str) -> Any:
        try:
            return getattr(enum, name)
        except AttributeError as exc:
            raise RuntimeError(
                f"installed torch_npu profiler does not support {name}"
            ) from exc

    def _create_profiler(self) -> Any:
        profiler = self._load_profiler_module()
        Path(self.config.output_dir).mkdir(parents=True, exist_ok=True)

        activities = [self._enum_value(profiler.ProfilerActivity, "NPU")]
        if self.config.with_cpu:
            activities.insert(0, self._enum_value(profiler.ProfilerActivity, "CPU"))

        level_names = {
            "level_none": "Level_none",
            "level0": "Level0",
            "level1": "Level1",
            "level2": "Level2",
        }
        export_names = {"text": "Text", "db": "Db"}
        experimental_config = profiler._ExperimentalConfig(
            profiler_level=self._enum_value(
                profiler.ProfilerLevel, level_names[self.config.level.lower()]
            ),
            aic_metrics=self._enum_value(profiler.AiCMetrics, "AiCoreNone"),
            export_type=[
                self._enum_value(
                    profiler.ExportType, export_names[self.config.export_type.lower()]
                )
            ],
            data_simplification=self.config.data_simplification,
        )
        worker_name = self.config.worker_name or (
            f"rank_{self.config.rank}_pid_{os.getpid()}"
        )
        trace_handler = profiler.tensorboard_trace_handler(
            self.config.output_dir, worker_name=worker_name
        )
        return profiler.profile(
            activities=activities,
            schedule=profiler.schedule(
                wait=self.config.wait,
                warmup=self.config.warmup,
                active=self.config.active,
                repeat=self.config.repeat,
                skip_first=self.config.start_step,
            ),
            on_trace_ready=trace_handler,
            record_shapes=self.config.record_shapes,
            profile_memory=self.config.profile_memory,
            with_stack=self.config.with_stack,
            experimental_config=experimental_config,
        )

    def start(self) -> None:
        if not self.config.active_on_this_rank:
            return
        with self._lifecycle_lock:
            if self._state == "started":
                return
            if self._state != "idle":
                raise RuntimeError(f"invalid profiler lifecycle state: {self._state}")
            self._state = "starting"
            profiler = None
            try:
                self._reserve_active_slot()
                profiler = self._create_profiler()
                self._profiler = profiler
                profiler.start()
            except BaseException:
                if profiler is None:
                    self._profiler = None
                    self._state = "idle"
                    self._release_active_slot()
                else:
                    # start() may have partially activated the native profiler.
                    # Keep ownership so stop() can be retried explicitly.
                    self._state = "failed"
                raise
            self._state = "started"

    def step(self) -> None:
        with self._lifecycle_lock:
            if self._state != "started":
                return
            self._profiler.step()
            self.steps += 1

    def stop(self) -> None:
        with self._lifecycle_lock:
            if self._state not in {"started", "failed"}:
                return
            self._state = "stopping"
            profiler = self._profiler
            try:
                profiler.stop()
            except BaseException:
                # A failed stop leaves the underlying profiler state unknown. Keep
                # ownership and the instance so a caller can retry cleanup safely.
                self._state = "failed"
                raise
            self._profiler = None
            self._state = "idle"
            self._release_active_slot()

    def _reserve_active_slot(self) -> None:
        with self._active_lock:
            active = ProfilerController._active_controller
            if active is not None and active is not self:
                raise RuntimeError(
                    "another ProfilerController is already active in this process"
                )
            ProfilerController._active_controller = self

    def _release_active_slot(self) -> None:
        with self._active_lock:
            if ProfilerController._active_controller is self:
                ProfilerController._active_controller = None

    def __enter__(self) -> ProfilerController:  # noqa: PYI034 - Python 3.10 asset.
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if exc is None:
            self.stop()
        else:
            try:
                self.stop()
            except Exception as stop_error:  # noqa: BLE001 - preserve the active business error.
                add_note = getattr(exc, "add_note", None)
                if callable(add_note):
                    add_note(f"profiler stop also failed: {stop_error!r}")
        return False


def validate_step_budget(config: ProfilerConfig, total_steps: int) -> None:
    if not config.active_on_this_rank:
        return
    if total_steps < config.required_steps:
        raise ValueError(
            f"profiler needs at least {config.required_steps} step() calls, got {total_steps}"
        )
