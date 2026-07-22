from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

from msagent.agents.factory import AgentFactory, _TAVILY_KEY_VALIDATION_CACHE
from msagent.configs import LLMConfig
from msagent.core import settings as settings_module
from msagent.llms.factory import LLMFactory


def test_llm_factory_disables_ssl_verification_for_openai_clients(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_init_chat_model(model_name: str, **kwargs):
        captured["model_name"] = model_name
        captured["kwargs"] = kwargs
        return SimpleNamespace(model_name=model_name, kwargs=kwargs)

    monkeypatch.setattr("msagent.llms.factory.patch_chat_openai_reasoning_content_support", lambda: None)
    monkeypatch.setattr("msagent.llms.factory.init_chat_model", fake_init_chat_model)
    monkeypatch.setattr(
        "msagent.llms.factory.httpx.Client",
        lambda **kwargs: SimpleNamespace(kwargs=kwargs),
    )
    monkeypatch.setattr(
        "msagent.llms.factory.httpx.AsyncClient",
        lambda **kwargs: SimpleNamespace(kwargs=kwargs),
    )

    config = LLMConfig(
        provider="openai",
        model="gpt-5.4",
        alias="default",
        base_url="https://gmn.chuangzuoli.com/v1",
        http2=True,
        max_tokens=0,
        temperature=0.1,
        streaming=True,
        request_timeout_seconds=120,
    )

    LLMFactory().create(config)

    kwargs = captured["kwargs"]
    assert "verify" not in kwargs["http_client"].kwargs
    assert "verify" not in kwargs["http_async_client"].kwargs


class _DummyAsyncClient:
    def __init__(self, capture: dict[str, object], **kwargs):
        capture["client_kwargs"] = kwargs
        self._capture = capture

    async def __aenter__(self) -> _DummyAsyncClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def get(self, url, headers):
        self._capture["url"] = url
        self._capture["headers"] = headers
        return SimpleNamespace(status_code=200)


def test_ssl_verification_is_disabled_by_default_on_import(monkeypatch) -> None:
    monkeypatch.setattr(settings_module.ssl, "create_default_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        settings_module.ssl,
        "_create_default_https_context",
        lambda *args, **kwargs: None,
    )

    reloaded = importlib.reload(settings_module)

    context = reloaded.ssl.create_default_context()
    assert context.check_hostname is False
    assert context.verify_mode is reloaded.ssl.CERT_NONE
    assert reloaded.ssl._create_default_https_context is reloaded.ssl.create_default_context


def test_tavily_probe_disables_ssl_verification_by_default(monkeypatch) -> None:
    capture: dict[str, object] = {}

    monkeypatch.setattr(
        "msagent.agents.factory.httpx.AsyncClient",
        lambda **kwargs: _DummyAsyncClient(capture, **kwargs),
    )
    _TAVILY_KEY_VALIDATION_CACHE.clear()

    ok = asyncio.run(AgentFactory._probe_tavily_api_key("placeholder"))

    assert ok is True
    assert "verify" not in capture["client_kwargs"]
