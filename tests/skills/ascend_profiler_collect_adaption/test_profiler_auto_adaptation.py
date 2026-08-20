from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
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


def write_valid_text_session(root: Path, worker: str) -> Path:
    output = root / f"{worker}_1_20260820120000000_ascend_pt" / "ASCEND_PROFILER_OUTPUT"
    output.mkdir(parents=True)
    (output / "trace_view.json").write_text(
        json.dumps(
            [
                {
                    "name": "MatMul",
                    "cat": "async_npu",
                    "ph": "X",
                    "pid": 1,
                    "tid": 2,
                    "ts": 1,
                    "dur": 2,
                }
            ]
        ),
        encoding="utf-8",
    )
    (output / "kernel_details.csv").write_text(
        "Name,Duration(us),Device_id\nMatMul,2.0,0\n", encoding="utf-8"
    )
    return output


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


def test_profiler_agent_loads_adaptation_skill_by_default() -> None:
    config = (
        ROOT / "resources" / "configs" / "default" / "agents" / "Profiler.yml"
    ).read_text(encoding="utf-8")
    assert "default:torch-npu-profiler-adaptation" in config


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


def test_enabled_controller_derives_isolated_worker_name(tmp_path: Path) -> None:
    fake = FakeProfilerModule()
    controller = adapter.ProfilerController(
        adapter.ProfilerConfig(
            enabled=True, rank=3, ranks=(3,), output_dir=str(tmp_path)
        ),
        profiler_module=fake,
    )
    controller.start()
    controller.stop()
    assert fake.profile_kwargs["on_trace_ready"]["worker_name"].startswith(
        "rank_3_pid_"
    )


def test_controller_serializes_concurrent_lifecycle_and_can_restart(
    tmp_path: Path,
) -> None:
    fake = FakeProfilerModule()
    controller = adapter.ProfilerController(
        adapter.ProfilerConfig(enabled=True, output_dir=str(tmp_path)),
        profiler_module=fake,
    )
    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(lambda _: controller.start(), range(12)))
    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(lambda _: controller.stop(), range(12)))
    with controller:
        controller.step()

    assert (fake.instance.started, fake.instance.stopped) == (2, 2)
    assert fake.instance.stepped == 1


def test_context_manager_preserves_business_exception_when_stop_fails(
    tmp_path: Path,
) -> None:
    fake = FakeProfilerModule()

    def failing_stop() -> None:
        raise RuntimeError("stop failed")

    original_stop = fake.instance.stop
    fake.instance.stop = failing_stop
    config = adapter.ProfilerConfig(enabled=True, output_dir=str(tmp_path))
    controller = adapter.ProfilerController(config, profiler_module=fake)
    with (
        pytest.raises(ValueError, match="business failed") as error,
        controller,
    ):
        raise ValueError("business failed")
    assert "profiler stop also failed" in " ".join(error.value.__notes__)
    fake.instance.stop = original_stop
    controller.stop()


def test_failed_stop_keeps_process_slot_until_retry_succeeds(tmp_path: Path) -> None:
    first_fake = FakeProfilerModule()
    original_stop = first_fake.instance.stop
    first_fake.instance.stop = lambda: (_ for _ in ()).throw(
        RuntimeError("stop failed")
    )
    first = adapter.ProfilerController(
        adapter.ProfilerConfig(enabled=True, output_dir=str(tmp_path / "first")),
        profiler_module=first_fake,
    )
    second = adapter.ProfilerController(
        adapter.ProfilerConfig(enabled=True, output_dir=str(tmp_path / "second")),
        profiler_module=FakeProfilerModule(),
    )
    first.start()
    with pytest.raises(RuntimeError, match="stop failed"):
        first.stop()
    with pytest.raises(RuntimeError, match="already active"):
        second.start()
    first_fake.instance.stop = original_stop
    first.stop()
    second.start()
    second.stop()


def test_failed_native_start_keeps_process_slot_until_cleanup(tmp_path: Path) -> None:
    first_fake = FakeProfilerModule()
    original_start = first_fake.instance.start
    first_fake.instance.start = lambda: (_ for _ in ()).throw(
        RuntimeError("start failed")
    )
    first = adapter.ProfilerController(
        adapter.ProfilerConfig(enabled=True, output_dir=str(tmp_path / "first")),
        profiler_module=first_fake,
    )
    second = adapter.ProfilerController(
        adapter.ProfilerConfig(enabled=True, output_dir=str(tmp_path / "second")),
        profiler_module=FakeProfilerModule(),
    )

    with pytest.raises(RuntimeError, match="start failed"):
        first.start()
    with pytest.raises(RuntimeError, match="already active"):
        second.start()

    first_fake.instance.start = original_start
    first.stop()
    second.start()
    second.stop()


def test_profiler_creation_failure_releases_process_slot(tmp_path: Path) -> None:
    first_fake = FakeProfilerModule()
    first_fake.profile = lambda **_: (_ for _ in ()).throw(
        RuntimeError("create failed")
    )
    first = adapter.ProfilerController(
        adapter.ProfilerConfig(enabled=True, output_dir=str(tmp_path / "first")),
        profiler_module=first_fake,
    )
    second = adapter.ProfilerController(
        adapter.ProfilerConfig(enabled=True, output_dir=str(tmp_path / "second")),
        profiler_module=FakeProfilerModule(),
    )

    with pytest.raises(RuntimeError, match="create failed"):
        first.start()
    second.start()
    second.stop()


def test_config_parses_serialized_values_and_rejects_ambiguous_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = adapter.ProfilerConfig.from_mapping(
        {
            "enabled": "false",
            "start_step": "2",
            "rank": "0",
            "ranks": "0,2",
            "with_cpu": "true",
        }
    )
    assert config.enabled is False
    assert config.start_step == 2
    assert config.rank == 0
    assert config.ranks == (0, 2)

    with pytest.raises(ValueError, match="true.*false"):
        adapter.ProfilerConfig.from_mapping({"enabled": "yes"})
    with pytest.raises(TypeError, match="start_step"):
        adapter.ProfilerConfig.from_mapping({"start_step": 2.5})
    with pytest.raises(TypeError, match="ranks"):
        adapter.ProfilerConfig.from_mapping({"ranks": 0})

    for name in (
        "RANK",
        "OMPI_COMM_WORLD_RANK",
        "PMI_RANK",
        "SLURM_PROCID",
        "LOCAL_RANK",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WORLD_SIZE", "2")
    with pytest.raises(ValueError, match="rank is unknown"):
        adapter.ProfilerConfig.from_mapping({"enabled": True})


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

    adapter.validate_step_budget(adapter.ProfilerConfig(enabled=False, active=10), 2)
    inactive_rank = adapter.ProfilerConfig(enabled=True, rank=1, ranks=(0,), active=10)
    adapter.validate_step_budget(inactive_rank, 2)


def test_rank_resolution_uses_launcher_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RANK", "1")
    monkeypatch.setenv("WORLD_SIZE", "4")
    monkeypatch.setenv("SLURM_PROCID", "0")
    monkeypatch.setenv("SLURM_NTASKS", "1")
    assert adapter.ProfilerConfig(enabled=True, ranks=(-1,)).rank == 1
    assert adapter.ProfilerConfig(enabled=True, rank=1, ranks=(-1,)).rank == 1


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
    assert candidates[0].confidence == "high"


def test_discovery_uses_relative_ignores_and_does_not_score_nested_loop(
    tmp_path: Path,
) -> None:
    root = tmp_path / "site-packages" / "new_framework"
    root.mkdir(parents=True)
    (root / "engine.py").write_text(
        "def train(model, batches):\n"
        "    for epoch in range(2):\n"
        "        for batch in batches:\n"
        "            loss = model(batch)\n"
        "            loss.backward()\n",
        encoding="utf-8",
    )
    candidates, errors = discoverer.discover(root)
    assert not errors
    assert [(item.line, item.loop_type) for item in candidates] == [(3, "For")]


@pytest.mark.parametrize("framework", ["transformers", "accelerate", "diffusers"])
def test_profile_validator_accepts_visualizable_framework_trace(
    tmp_path: Path, framework: str
) -> None:
    write_valid_text_session(tmp_path, framework)
    report = validator.build_report(
        tmp_path,
        "text",
        expected_sessions=1,
        expected_workers={framework},
    )
    assert report["passed"] is True
    assert report["parsed_output_ready"] is True
    assert report["visualizable"] is True


def test_text_validation_requires_parsed_statistics(tmp_path: Path) -> None:
    output = (
        tmp_path / "worker_1_20260820120000000_ascend_pt" / "ASCEND_PROFILER_OUTPUT"
    )
    output.mkdir(parents=True)
    (output / "trace_view.json").write_text(
        json.dumps(
            [
                {
                    "name": "op",
                    "cat": "cann",
                    "ph": "X",
                    "pid": 1,
                    "tid": 1,
                    "ts": 1,
                    "dur": 2,
                }
            ]
        ),
        encoding="utf-8",
    )
    report = validator.build_report(tmp_path, "text")
    assert report["visualizable"] is True
    assert report["parsed_output_ready"] is False
    assert report["passed"] is False


def test_db_validation_does_not_claim_visualization(tmp_path: Path) -> None:
    output = (
        tmp_path / "worker_1_20260820120000000_ascend_pt" / "ASCEND_PROFILER_OUTPUT"
    )
    output.mkdir(parents=True)
    database = output / "ascend_pytorch_profiler_0.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE NPU_INFO(id INTEGER, name TEXT)")
        connection.execute("INSERT INTO NPU_INFO VALUES (0, 'Ascend 910')")
        connection.execute(
            "CREATE TABLE CANN_API(startNs INTEGER, endNs INTEGER, name TEXT)"
        )
        connection.execute("INSERT INTO CANN_API VALUES (1, 2, 'MatMul')")
    report = validator.build_report(tmp_path, "db")
    assert report["passed"] is True
    assert report["visualizable"] is False


def test_db_validation_rejects_unrelated_sqlite_database(tmp_path: Path) -> None:
    output = (
        tmp_path / "worker_1_20260820120000000_ascend_pt" / "ASCEND_PROFILER_OUTPUT"
    )
    output.mkdir(parents=True)
    database = output / "ascend_pytorch_profiler_unrelated.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE business_records(name TEXT)")
        connection.execute("INSERT INTO business_records VALUES ('not profiling')")
    report = validator.build_report(tmp_path, "db")
    assert report["passed"] is False
    assert "lacks non-empty PTA device/activity tables" in report["checks"][0]["detail"]


def test_validator_rejects_substring_npu_category_and_invalid_device(
    tmp_path: Path,
) -> None:
    output = (
        tmp_path / "worker_1_20260820120000000_ascend_pt" / "ASCEND_PROFILER_OUTPUT"
    )
    output.mkdir(parents=True)
    (output / "trace_view.json").write_text(
        json.dumps(
            [
                {
                    "name": "CPU",
                    "cat": "cpu",
                    "ph": "X",
                    "pid": 1,
                    "tid": 1,
                    "ts": 1,
                    "dur": 2,
                },
                {"name": "fake", "cat": "cpu_not_npu", "ph": "X"},
            ]
        ),
        encoding="utf-8",
    )
    (output / "kernel_details.csv").write_text(
        "Name,Duration(us),Device_id\nFakeKernel,1.0,not-a-device\n",
        encoding="utf-8",
    )
    report = validator.build_report(tmp_path, "text")
    assert report["passed"] is False
    assert all(check["passed"] is False for check in report["checks"])


def test_db_validation_rejects_empty_substring_table(tmp_path: Path) -> None:
    output = (
        tmp_path / "worker_1_20260820120000000_ascend_pt" / "ASCEND_PROFILER_OUTPUT"
    )
    output.mkdir(parents=True)
    database = output / "ascend_pytorch_profiler_empty.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE memory_cache(name TEXT)")
    report = validator.build_report(tmp_path, "db")
    assert report["passed"] is False


def test_validator_rejects_fake_trace_and_database(tmp_path: Path) -> None:
    output = (
        tmp_path / "worker_1_20260820120000000_ascend_pt" / "ASCEND_PROFILER_OUTPUT"
    )
    output.mkdir(parents=True)
    (output / "trace_view.json").write_text(
        json.dumps(
            [{"name": "cpu_only", "ph": "X", "pid": 1, "tid": 1, "ts": 1, "dur": 2}]
        ),
        encoding="utf-8",
    )
    (output / "kernel_details.csv").write_text(
        "Name,Duration(us),Device_id\nFakeKernel,1.0,0\n", encoding="utf-8"
    )
    (output / "ascend_pytorch_profiler_fake.db").write_bytes(b"SQLite format 3\x00")
    report = validator.build_report(tmp_path, "either")
    assert report["passed"] is False
    assert any(
        check["kind"] == "trace" and not check["passed"] for check in report["checks"]
    )


def test_validator_does_not_combine_artifacts_from_different_sessions(
    tmp_path: Path,
) -> None:
    trace_output = write_valid_text_session(tmp_path, "trace-worker")
    (trace_output / "kernel_details.csv").unlink()
    csv_output = (
        tmp_path / "csv-worker_1_20260820120000000_ascend_pt" / "ASCEND_PROFILER_OUTPUT"
    )
    csv_output.mkdir(parents=True)
    (csv_output / "kernel_details.csv").write_text(
        "Name,Duration(us),Device_id\nMatMul,2.0,0\n", encoding="utf-8"
    )
    report = validator.build_report(tmp_path, "text", expected_sessions=2)
    assert report["passed"] is False
    assert len(report["sessions"]) == 2
    assert all(session["passed"] is False for session in report["sessions"])


def test_validator_enforces_expected_worker_and_rank(tmp_path: Path) -> None:
    write_valid_text_session(tmp_path, "rank_3_worker")
    accepted = validator.build_report(
        tmp_path,
        "text",
        expected_sessions=1,
        expected_workers={"rank_3_worker"},
        expected_ranks={3},
    )
    assert accepted["passed"] is True

    rejected = validator.build_report(
        tmp_path,
        "text",
        expected_workers={"rank_4_worker"},
        expected_ranks={4},
    )
    assert rejected["passed"] is False
    assert rejected["expectations"]["missing_workers"] == ["rank_4_worker"]
    assert rejected["expectations"]["missing_ranks"] == [4]


@pytest.mark.parametrize(
    ("project", "patch_name"),
    [
        ("transformers_trainer", "profiler-auto-adaptation-transformers.patch"),
        ("accelerate_training", "profiler-auto-adaptation-accelerate.patch"),
        ("diffusers_inference", "profiler-auto-adaptation-diffusers.patch"),
    ],
)
def test_saved_msagent_patches_apply_to_unadapted_frameworks(
    tmp_path: Path, project: str, patch_name: str
) -> None:
    fixture = (
        Path(__file__).parent / "fixtures" / "unadapted_frameworks" / project / "run.py"
    )
    assert "ProfilerController" not in fixture.read_text(encoding="utf-8")
    target = tmp_path / project
    target.mkdir()
    (target / "run.py").write_bytes(fixture.read_bytes())
    patch = ROOT / "docs" / "zh" / "best_practices" / "evidence" / patch_name
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=target, check=True)
    subprocess.run(["git", "apply", str(patch)], cwd=target, check=True)
    (target / "profiler_adapter.py").write_bytes(
        (SKILL / "assets" / "profiler_adapter.py").read_bytes()
    )
    subprocess.run(
        [sys.executable, "-m", "py_compile", "run.py", "profiler_adapter.py"],
        cwd=target,
        check=True,
    )
    assert "ProfilerController" in (target / "run.py").read_text(encoding="utf-8")


def test_multiprocess_service_fixture_is_unadapted() -> None:
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "unadapted_frameworks"
        / "multiprocess_inference_service"
        / "run.py"
    )
    source = fixture.read_text(encoding="utf-8")
    assert "mp.get_context" in source
    assert "ProfilerController" not in source


def test_real_npu_evidence_matches_committed_patches_and_adapter() -> None:
    evidence_dir = ROOT / "docs" / "zh" / "best_practices" / "evidence"
    evidence = json.loads(
        (evidence_dir / "profiler-auto-adaptation-20260820.json").read_text(
            encoding="utf-8"
        )
    )
    adapter_sha = hashlib.sha256(
        (SKILL / "assets" / "profiler_adapter.py").read_bytes()
    ).hexdigest()
    assert len(evidence["results"]) >= 3
    for result in evidence["results"]:
        patch_sha = hashlib.sha256(
            (evidence_dir / result["patch"]).read_bytes()
        ).hexdigest()
        assert patch_sha == result["patch_sha256"]
        assert adapter_sha == result["adapter_sha256"]
        assert result["baseline_equals_disabled"] is True
        assert result["disabled_equals_enabled"] is True
        assert result["trace"]["npu_async_pairs"] > 0
        assert result["kernel_csv"]["valid_rows"] > 0
        assert result["validator"] == {
            "passed": True,
            "text_ready": True,
            "parsed_output_ready": True,
            "visualizable": True,
        }


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
