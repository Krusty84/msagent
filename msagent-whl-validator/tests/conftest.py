"""Shared pytest fixtures for the msagent whl validation suite."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest

from validator_core.artifacts import create_case_artifact_dir
from validator_core.config import ValidationConfig, load_validation_config
from validator_core.llm_capture_server import LlmCaptureServer, ToolCallScript
from validator_core.msagent_runtime import MsagentRuntime


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "test_config.yaml"
WORKSPACE_SEED = PROJECT_ROOT / "testdata" / "workspace_seed"

_CASE_ARTIFACTS: dict[str, Path] = {}
_FAILED_CASES: set[str] = set()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Remember failures so session cleanup retains only useful artifacts."""
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)
    if report.failed:
        _FAILED_CASES.add(item.nodeid)


def pytest_sessionfinish(session, exitstatus) -> None:
    """Apply the configured artifact retention policy after all reports exist."""
    try:
        retention = load_validation_config(CONFIG_PATH).artifacts.retention
    except Exception:
        return
    if retention == "all":
        return

    for nodeid, case_dir in _CASE_ARTIFACTS.items():
        if retention == "failed" and nodeid in _FAILED_CASES:
            continue
        shutil.rmtree(case_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def validation_config() -> ValidationConfig:
    """Load and validate suite configuration once per pytest session."""
    return load_validation_config(CONFIG_PATH)


@pytest.fixture(scope="session")
def test_config(validation_config: ValidationConfig) -> dict:
    """Expose raw YAML temporarily for not-yet-migrated test cases."""
    return validation_config.raw


@pytest.fixture(scope="session")
def validation_run_dir(validation_config: ValidationConfig) -> Path:
    """Resolve this pytest session's durable artifact directory."""
    configured = os.getenv("MSAGENT_VALIDATION_RUN_DIR", "").strip()
    if configured:
        run_dir = Path(configured).expanduser().resolve()
    else:
        run_id = datetime.now().strftime("manual-%Y%m%d-%H%M%S")
        run_id = f"{run_id}-{os.getpid()}"
        run_dir = validation_config.artifacts.root_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


@pytest.fixture
def case_artifact_dir(request, validation_run_dir: Path) -> Path:
    """Allocate one artifact directory for all invocations in a test case."""
    case_dir = create_case_artifact_dir(validation_run_dir, request.node.nodeid)
    _CASE_ARTIFACTS[request.node.nodeid] = case_dir
    return case_dir


@pytest.fixture
def test_workspace(tmp_path: Path) -> Path:
    """Copy deterministic local-tool inputs into an isolated workspace."""
    workspace = tmp_path / "workspace"
    shutil.copytree(WORKSPACE_SEED, workspace)
    script = workspace / "print_marker.sh"
    script.chmod(script.stat().st_mode | 0o111)
    return workspace


@pytest.fixture
def msagent_runtime_factory(
    request,
    validation_config: ValidationConfig,
    tmp_path: Path,
    case_artifact_dir: Path,
) -> Iterator[Callable[..., MsagentRuntime]]:
    """Create isolated runtimes sharing artifacts with the current test case."""
    runtime_index = 0
    runtimes: list[MsagentRuntime] = []

    def create(
        provider: str = "openai",
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        workspace_seed: Path | None = None,
        runtime_env: dict[str, str] | None = None,
    ) -> MsagentRuntime:
        nonlocal runtime_index
        protocol = validation_config.llm.protocol(provider)
        resolved_api_key = (
            api_key.strip()
            if isinstance(api_key, str)
            else os.getenv(validation_config.llm.api_key_env, "").strip()
        )
        if not resolved_api_key:
            pytest.skip(
                f"{provider} 测试缺少环境变量: {validation_config.llm.api_key_env}"
            )
        resolved_base_url = (
            base_url.strip()
            if isinstance(base_url, str) and base_url.strip()
            else protocol.base_url
        )

        runtime_root = tmp_path / f"runtime-{runtime_index:02d}-{provider}"
        runtime_artifacts = case_artifact_dir / f"runtime-{runtime_index:02d}"
        runtime_index += 1
        workspace = runtime_root / "workspace"
        msagent_home = runtime_root / "msagent-home"
        if workspace_seed is None:
            workspace.mkdir(parents=True)
        else:
            shutil.copytree(workspace_seed, workspace)

        scripts_dir = Path(sys.executable).parent
        extra_env = {
            "MSAGENT_HOME": str(msagent_home),
            "MSAGENT_FAKE_BACKEND": "0",
            "PATH": str(scripts_dir) + os.pathsep + os.environ.get("PATH", ""),
            protocol.provider_api_key_env: resolved_api_key,
            protocol.base_url_env: resolved_base_url,
        }
        if runtime_env is not None:
            if not isinstance(runtime_env, dict):
                raise TypeError("runtime_env must be a dictionary or None")
            invalid_runtime_env = [
                key
                for key, value in runtime_env.items()
                if not isinstance(key, str) or not isinstance(value, str)
            ]
            if invalid_runtime_env:
                raise TypeError("runtime_env keys and values must all be strings")
            extra_env.update(runtime_env)
        command_env = os.environ.copy()
        command_env.update(extra_env)
        executable = validation_config.msagent.executable
        if shutil.which(executable, path=extra_env["PATH"]) is None:
            pytest.fail(
                f"当前 Python 环境中找不到 {executable!r}；"
                "请先安装待验证的 mindstudio-agent whl",
                pytrace=False,
            )

        configured = subprocess.run(
            [
                executable,
                "config",
                "--llm-provider",
                provider,
                "--llm-base-url",
                resolved_base_url,
                "--llm-model",
                validation_config.llm.model,
                "-w",
                str(workspace),
            ],
            env=command_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=validation_config.msagent.timeout_seconds,
        )
        assert configured.returncode == 0, (
            f"{provider} 配置失败。\n"
            f"stdout:\n{configured.stdout}\nstderr:\n{configured.stderr}"
        )

        runtime = MsagentRuntime(
            provider=provider,
            model=validation_config.llm.model,
            workspace_dir=workspace,
            msagent_home=msagent_home,
            artifact_dir=runtime_artifacts,
            extra_env=extra_env,
            executable=executable,
        )
        runtimes.append(runtime)
        return runtime

    yield create

    report = getattr(request.node, "rep_call", None)
    if (
        report is not None
        and report.failed
        and validation_config.artifacts.retain_workspace_on_failure
    ):
        snapshots = case_artifact_dir / "failed-workspaces"
        for index, runtime in enumerate(runtimes):
            if runtime.workspace_dir.exists():
                shutil.copytree(
                    runtime.workspace_dir,
                    snapshots / f"runtime-{index:02d}",
                    dirs_exist_ok=True,
                )


@pytest.fixture
def llm_runtime_factory(msagent_runtime_factory):
    """Compatibility name used by existing LLM, Skill, and Prompt tests."""
    return msagent_runtime_factory


@pytest.fixture
def llm_capture_server(case_artifact_dir: Path) -> Iterator[LlmCaptureServer]:
    """Capture final LLM payloads and persist them beside failed traces."""
    server = LlmCaptureServer.start()
    try:
        yield server
    finally:
        (case_artifact_dir / "llm-requests.json").write_text(
            json.dumps(server.requests, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        server.close()


@pytest.fixture
def scripted_llm_server_factory(
    case_artifact_dir: Path,
) -> Iterator[Callable[[ToolCallScript], LlmCaptureServer]]:
    """Start deterministic multi-turn LLM mocks for real tool execution tests."""
    servers: list[LlmCaptureServer] = []

    def create(script: ToolCallScript) -> LlmCaptureServer:
        server = LlmCaptureServer.start(script=script)
        servers.append(server)
        return server

    yield create

    captured = []
    errors = []
    for index, server in enumerate(servers):
        captured.append({"server_index": index, "requests": server.requests})
        errors.extend(server.errors)
        server.close()

    (case_artifact_dir / "scripted-llm-requests.json").write_text(
        json.dumps(captured, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if errors:
        (case_artifact_dir / "scripted-llm-errors.json").write_text(
            json.dumps(errors, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
