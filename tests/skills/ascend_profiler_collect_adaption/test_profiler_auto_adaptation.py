from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "skills" / "ascend-profiler-collect-adaption"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


adapter = load_module("test_profiler_adapter", SKILL / "assets" / "profiler_adapter.py")
discoverer = load_module(
    "test_discover_execution_loops", SKILL / "scripts" / "discover_execution_loops.py"
)
validator = load_module(
    "test_validate_profile_output", SKILL / "scripts" / "validate_profile_output.py"
)


class FakeProfile:
    def __init__(self) -> None:
        self.started = 0
        self.stepped = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def step(self) -> None:
        self.stepped += 1

    def stop(self) -> None:
        self.stopped += 1


class FakeProfilerModule:
    ProfilerActivity = SimpleNamespace(CPU="cpu", NPU="npu")
    ProfilerLevel = SimpleNamespace(
        Level_none="none", Level0="level0", Level1="level1", Level2="level2"
    )
    AiCMetrics = SimpleNamespace(AiCoreNone="none")
    ExportType = SimpleNamespace(Text="text", Db="db")

    def __init__(self) -> None:
        self.instance = FakeProfile()
        self.profile_kwargs = None

    @staticmethod
    def _ExperimentalConfig(**kwargs):
        return kwargs

    @staticmethod
    def schedule(**kwargs):
        return kwargs

    @staticmethod
    def tensorboard_trace_handler(output_dir, **kwargs):
        return {"output_dir": output_dir, **kwargs}

    def profile(self, **kwargs):
        self.profile_kwargs = kwargs
        return self.instance


def test_disabled_controller_has_no_side_effects(tmp_path: Path) -> None:
    output_dir = tmp_path / "must-not-exist"
    controller = adapter.ProfilerController(
        adapter.ProfilerConfig(output_dir=str(output_dir))
    )
    controller.start()
    controller.step()
    controller.stop()
    assert controller.steps == 0
    assert not output_dir.exists()


def test_enabled_controller_owns_idempotent_lifecycle(tmp_path: Path) -> None:
    fake = FakeProfilerModule()
    config = adapter.ProfilerConfig(
        enabled=True, output_dir=str(tmp_path), active=2, worker_name="rank0"
    )
    controller = adapter.ProfilerController(config, profiler_module=fake)
    controller.start()
    controller.start()
    controller.step()
    controller.step()
    controller.stop()
    controller.stop()

    assert (fake.instance.started, fake.instance.stepped, fake.instance.stopped) == (
        1,
        2,
        1,
    )
    assert fake.profile_kwargs["schedule"]["active"] == 2
    assert fake.profile_kwargs["on_trace_ready"]["worker_name"] == "rank0"


def test_context_manager_preserves_business_exception_when_stop_fails(
    tmp_path: Path,
) -> None:
    fake = FakeProfilerModule()

    def failing_stop() -> None:
        raise RuntimeError("stop failed")

    fake.instance.stop = failing_stop
    config = adapter.ProfilerConfig(enabled=True, output_dir=str(tmp_path))
    with (
        pytest.raises(ValueError, match="business failed") as error,
        adapter.ProfilerController(config, profiler_module=fake),
    ):
        raise ValueError("business failed")
    assert "profiler stop also failed" in " ".join(error.value.__notes__)


def test_config_rejects_unknown_fields_and_short_step_budget() -> None:
    with pytest.raises(ValueError, match="unknown profiler config fields"):
        adapter.ProfilerConfig.from_mapping({"enabled": True, "typo": 1})
    config = adapter.ProfilerConfig(enabled=True, start_step=2, active=2)
    with pytest.raises(ValueError, match="at least 4"):
        adapter.validate_step_budget(config, 3)

    repeated = adapter.ProfilerConfig(
        enabled=True, start_step=1, wait=2, warmup=1, active=3, repeat=3
    )
    assert repeated.required_steps == 19
    with pytest.raises(ValueError, match="at least 19"):
        adapter.validate_step_budget(repeated, 18)


def test_process_rejects_two_active_controllers(tmp_path: Path) -> None:
    first = adapter.ProfilerController(
        adapter.ProfilerConfig(enabled=True, output_dir=str(tmp_path / "first")),
        profiler_module=FakeProfilerModule(),
    )
    second = adapter.ProfilerController(
        adapter.ProfilerConfig(enabled=True, output_dir=str(tmp_path / "second")),
        profiler_module=FakeProfilerModule(),
    )
    first.start()
    with pytest.raises(RuntimeError, match="already active"):
        second.start()
    first.stop()
    second.start()
    second.stop()


def test_discovery_ranks_model_execution_loop(tmp_path: Path) -> None:
    source = tmp_path / "engine.py"
    source.write_text(
        "def train(model, optimizer, batches):\n"
        "    for batch in batches:\n"
        "        loss = model(batch)\n"
        "        loss.backward()\n"
        "        optimizer.step()\n",
        encoding="utf-8",
    )
    candidates, errors = discoverer.discover(tmp_path)
    assert not errors
    assert candidates[0].path == "engine.py"
    assert candidates[0].scope == "train"
    assert candidates[0].score >= 12


@pytest.mark.parametrize("framework", ["transformers", "accelerate", "diffusers"])
def test_profile_validator_accepts_visualizable_framework_trace(
    tmp_path: Path, framework: str
) -> None:
    output = tmp_path / framework / "ASCEND_PROFILER_OUTPUT"
    output.mkdir(parents=True)
    trace_payload = [
        {"name": framework, "ph": "X", "pid": 1, "tid": 1, "ts": 1, "dur": 2}
    ]
    if framework == "transformers":
        trace_payload = {"traceEvents": trace_payload}
    (output / "trace_view.json").write_text(
        json.dumps(trace_payload),
        encoding="utf-8",
    )
    (output / "op_statistic.csv").write_text(
        "OP Type,Count\nMatMul,1\n", encoding="utf-8"
    )
    report = validator.build_report(tmp_path, "text")
    assert report["passed"] is True
    assert report["parsed_output_ready"] is True
    assert report["visualizable"] is True


def test_text_validation_requires_parsed_statistics(tmp_path: Path) -> None:
    (tmp_path / "trace_view.json").write_text(
        json.dumps([{"name": "op", "ph": "X", "pid": 1, "tid": 1, "ts": 1, "dur": 2}]),
        encoding="utf-8",
    )
    report = validator.build_report(tmp_path, "text")
    assert report["visualizable"] is True
    assert report["parsed_output_ready"] is False
    assert report["passed"] is False


def test_db_validation_does_not_claim_visualization(tmp_path: Path) -> None:
    database = tmp_path / "ascend_pytorch_profiler_0.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE profiler_events(name TEXT)")
        connection.execute("INSERT INTO profiler_events VALUES ('MatMul')")
    report = validator.build_report(tmp_path, "db")
    assert report["passed"] is True
    assert report["visualizable"] is False


def test_validator_rejects_fake_trace_and_database(tmp_path: Path) -> None:
    (tmp_path / "trace_view.json").write_text('[{"ph": "X"}]', encoding="utf-8")
    (tmp_path / "op_statistic.csv").write_text(
        "OP Type,Count\nMatMul,1\n", encoding="utf-8"
    )
    (tmp_path / "ascend_pytorch_profiler_fake.db").write_bytes(b"SQLite format 3\x00")
    report = validator.build_report(tmp_path, "either")
    assert report["passed"] is False
    assert all(
        check["passed"] is False for check in report["checks"] if check["kind"] != "csv"
    )


def test_scaffolder_is_idempotent_and_refuses_overwrite(tmp_path: Path) -> None:
    script = SKILL / "scripts" / "scaffold_adapter.py"
    command = [
        sys.executable,
        str(script),
        str(tmp_path),
        "--destination",
        "pkg/profiler_adapter.py",
    ]
    first = subprocess.run(command, check=True, capture_output=True, text=True)
    second = subprocess.run(command, check=True, capture_output=True, text=True)
    assert "created" in first.stdout
    assert "unchanged" in second.stdout

    (tmp_path / "pkg" / "profiler_adapter.py").write_text("different", encoding="utf-8")
    conflict = subprocess.run(command, check=False, capture_output=True, text=True)
    assert conflict.returncode != 0
    assert "refusing to overwrite" in conflict.stderr
