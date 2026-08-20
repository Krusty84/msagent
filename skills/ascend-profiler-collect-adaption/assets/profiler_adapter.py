"""Framework-neutral torch_npu profiler lifecycle adapter.

Copy this module into the target framework and wire one controller into each
process that actually executes NPU work. Profiling is disabled by default.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from threading import Lock
from types import TracebackType
from typing import Any, ClassVar, Self


@dataclass(frozen=True)
class ProfilerConfig:
    enabled: bool = False
    output_dir: str = "./profiler_output"
    start_step: int = 0
    wait: int = 0
    warmup: int = 0
    active: int = 1
    repeat: int = 1
    rank: int = 0
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
        integer_fields = ("start_step", "wait", "warmup", "active", "repeat")
        for name in integer_fields:
            value = getattr(self, name)
            minimum = 1 if name in {"active", "repeat"} else 0
            if value < minimum:
                raise ValueError(f"{name} must be >= {minimum}, got {value}")
        if self.level.lower() not in {"level0", "level1", "level2", "level_none"}:
            raise ValueError(f"unsupported profiler level: {self.level}")
        if self.export_type.lower() not in {"text", "db"}:
            raise ValueError(f"unsupported export type: {self.export_type}")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> ProfilerConfig:
        if values is None:
            return cls()
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(f"unknown profiler config fields: {', '.join(unknown)}")
        normalized = dict(values)
        if "ranks" in normalized:
            normalized["ranks"] = tuple(int(rank) for rank in normalized["ranks"])
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
        self._started = False
        self._stopped = False
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
        handler_kwargs = {}
        if self.config.worker_name:
            handler_kwargs["worker_name"] = self.config.worker_name
        trace_handler = profiler.tensorboard_trace_handler(
            self.config.output_dir, **handler_kwargs
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
        if not self.config.active_on_this_rank or self._started:
            return
        if self._stopped:
            raise RuntimeError("a stopped ProfilerController cannot be restarted")
        with self._active_lock:
            active = self._active_controller
            if active is not None and active is not self:
                raise RuntimeError(
                    "another ProfilerController is already active in this process"
                )
            self.__class__._active_controller = self
        try:
            self._profiler = self._create_profiler()
            self._profiler.start()
        except BaseException:
            self._release_active_slot()
            raise
        self._started = True

    def step(self) -> None:
        if not self._started or self._stopped:
            return
        self._profiler.step()
        self.steps += 1

    def stop(self) -> None:
        if not self._started or self._stopped:
            return
        try:
            self._profiler.stop()
        finally:
            self._stopped = True
            self._release_active_slot()

    def _release_active_slot(self) -> None:
        with self._active_lock:
            if self._active_controller is self:
                self.__class__._active_controller = None

    def __enter__(self) -> Self:
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
                exc.add_note(f"profiler stop also failed: {stop_error!r}")
        return False


def validate_step_budget(config: ProfilerConfig, total_steps: int) -> None:
    if total_steps < config.required_steps:
        raise ValueError(
            f"profiler needs at least {config.required_steps} step() calls, got {total_steps}"
        )
