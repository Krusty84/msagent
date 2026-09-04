from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from langchain_core.messages import AIMessage, HumanMessage

# The handlers package must be initialized before the module is imported directly:
# handlers/__init__ re-exports the handler while the handler imports session_history
# from that same package (a pre-existing import cycle that the CLI never triggers).
import msagent.cli.handlers  # noqa: F401
from msagent.skill_evolver import direct_skill_generation as module
from msagent.skill_evolver.direct_skill_generation import (
    DirectSkillGenerationConfig,
    DirectSkillGenerationHandler,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
THREAD_ID = "thread-123456789"


class _NullStatus:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _ConsoleSpy:
    def __init__(self) -> None:
        self.info: list[str] = []
        self.success: list[str] = []
        self.warning: list[str] = []
        self.error: list[str] = []
        self.console = SimpleNamespace(status=lambda *_args, **_kwargs: _NullStatus())

    def print(self, *_args, **_kwargs) -> None:
        pass

    def print_info(self, content: str) -> None:
        self.info.append(content)

    def print_success(self, content: str) -> None:
        self.success.append(content)

    def print_warning(self, content: str) -> None:
        self.warning.append(content)

    def print_error(self, content: str) -> None:
        self.error.append(content)


@pytest.fixture
def handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spy = _ConsoleSpy()
    monkeypatch.setattr(module, "console", spy)

    async def fake_load_history(_session, _target):
        return THREAD_ID, [HumanMessage(content="привет")]

    async def fake_load_prompt_template(self, _root, _cfg):
        return "instruction template", "packaged"

    monkeypatch.setattr(module, "load_history", fake_load_history)
    monkeypatch.setattr(
        DirectSkillGenerationHandler,
        "_load_config",
        staticmethod(lambda: DirectSkillGenerationConfig()),
    )
    monkeypatch.setattr(DirectSkillGenerationHandler, "_load_prompt_template", fake_load_prompt_template)

    session = SimpleNamespace(
        context=SimpleNamespace(agent="msagent", thread_id=THREAD_ID, working_dir=tmp_path, model="default"),
        graph=None,
    )
    return DirectSkillGenerationHandler(session), spy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    ["Nothing to save.", "nothing to save", "  Nothing to save  \n", "`Nothing to save.`"],
)
async def test_handle_writes_nothing_on_sentinel(handler, tmp_path: Path, monkeypatch, answer: str) -> None:
    instance, spy = handler

    async def fake_generate(self, _messages, _template, _thread_id):
        return answer

    monkeypatch.setattr(DirectSkillGenerationHandler, "_generate_skill_md", fake_generate)

    await instance.handle([])

    assert not (tmp_path / "skills").exists()
    assert spy.info == [f"Nothing to save: no durable learning found in thread {THREAD_ID}"]
    assert spy.success == []
    assert spy.error == []


@pytest.mark.asyncio
async def test_handle_writes_skill_md(handler, tmp_path: Path, monkeypatch) -> None:
    instance, spy = handler
    skill = (
        "---\nname: generated-source-debugging\n"
        "description: Use when diagnosing failures involving generated source artifacts.\n---\n\n"
        "# Generated Source Debugging\n\n## Inputs\n\n- The failing output.\n\n"
        "## Workflow\n\n1. Reproduce the failure.\n\n## Outputs\n\nA verified diagnosis.\n"
    )

    async def fake_generate(self, _messages, _template, _thread_id):
        return skill

    monkeypatch.setattr(DirectSkillGenerationHandler, "_generate_skill_md", fake_generate)

    await instance.handle([])

    path = tmp_path / "skills" / "default" / "generated-source-debugging" / "SKILL.md"
    assert path.is_file()
    assert path.read_text(encoding="utf-8").startswith(skill.rstrip())
    assert spy.info == []
    assert spy.error == []
    assert spy.success == [f"Skill draft saved to {path}"]


@pytest.mark.asyncio
async def test_generate_skill_md_reports_middle_omissions(handler, monkeypatch) -> None:
    instance, _spy = handler
    payloads: list[list] = []

    class _FakeLLM:
        async def ainvoke(self, payload):
            payloads.append(list(payload))
            return AIMessage(content="Nothing to save.")

    async def fake_load_llm_config(_model, _working_dir):
        return SimpleNamespace(context_window=1000)

    async def fake_snapshot(self):
        return "- demo-skill: no description"

    monkeypatch.setattr(module.initializer, "load_llm_config", fake_load_llm_config)
    monkeypatch.setattr(module.initializer.llm_factory, "create", lambda _config: _FakeLLM())
    monkeypatch.setattr(module, "trim_history", lambda messages, _llm, _window, **_kw: (list(messages), 3))
    monkeypatch.setattr(DirectSkillGenerationHandler, "_build_skill_library_snapshot", fake_snapshot)

    result = await instance._generate_skill_md(
        [HumanMessage(content="привет")], "Library:\n{skill_library}", THREAD_ID
    )

    assert result == "Nothing to save."
    instruction = payloads[0][-1].content
    assert instruction.startswith(
        "[Note: 3 messages from the middle of the session were omitted due to context limits.]"
    )
    assert "- demo-skill: no description" in instruction


def test_load_config_reads_valid_packaged_default(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        cfg = DirectSkillGenerationHandler._load_config()

    assert cfg.active == "default"
    assert cfg.prompt_file == "prompt_v1.md"
    assert "Unsafe variant name" not in caplog.text


def test_packaged_config_active_passes_variant_validation() -> None:
    config_path = REPO_ROOT / "resources" / "configs" / "default" / "config.skill.evolver.yml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert module._VARIANT_NAME_PATTERN.fullmatch(str(data["active"]))


def test_packaged_prompt_matches_single_call_pipeline() -> None:
    prompt_path = (
        REPO_ROOT / "resources" / "configs" / "default" / "skill-evolver" / "prompts" / "default" / "prompt_v1.md"
    )
    text = prompt_path.read_text(encoding="utf-8")

    assert "# Output contract" in text
    assert "{skill_library}" in text
    for agentic_marker in (
        "skills_list",
        "skill_view",
        "skill_manage",
        "`mkdir`",
        "# Standard filesystem and shell access",
        "# Execution rules",
        "# Final response",
    ):
        assert agentic_marker not in text
