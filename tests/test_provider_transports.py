"""Offline contract tests for every provider transport.

No real credential or external endpoint is used here.  The tests inspect the
HTTP requests sent to a local stub, which prevents a configuration regression
from being discovered only after a user enters a provider key.
"""

from __future__ import annotations

import json


class _Response:
    def __init__(self, body: dict, status: int = 200) -> None:
        self._body = json.dumps(body).encode("utf-8")
        self._status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self._status


def _request() -> object:
    from core.models.provider_interface import ModelCompletionRequest

    return ModelCompletionRequest(
        system_prompt="System instruction",
        messages=[{"role": "user", "content": "Return provider-ok"}],
        temperature=0.2,
        max_tokens=64,
        json_schema={"type": "object"},
    )


def test_factory_selects_native_transport_for_each_catalogue_protocol():
    from core.models.provider_interface import (
        AnthropicMessagesProvider,
        AzureOpenAIProvider,
        GeminiGenerateContentProvider,
        HttpOpenAICompatibleProvider,
        create_external_model_provider,
    )

    assert isinstance(create_external_model_provider(
        provider_id="anthropic", api_key="test", base_url=None, model_name=None,
    ), AnthropicMessagesProvider)
    assert isinstance(create_external_model_provider(
        provider_id="gemini", api_key="test", base_url=None, model_name=None,
    ), GeminiGenerateContentProvider)
    assert isinstance(create_external_model_provider(
        provider_id="azure", api_key="test", base_url="https://example.test/openai/v1", model_name="deployment",
    ), AzureOpenAIProvider)
    assert isinstance(create_external_model_provider(
        provider_id="groq", api_key="test", base_url=None, model_name=None,
    ), HttpOpenAICompatibleProvider)


def test_every_catalogue_provider_maps_to_a_supported_runtime_transport():
    from core.llm.providers import PROVIDERS
    from core.models.provider_interface import (
        AnthropicMessagesProvider,
        AzureOpenAIProvider,
        GeminiGenerateContentProvider,
        HttpOpenAICompatibleProvider,
        create_external_model_provider,
    )

    for info in PROVIDERS:
        provider = create_external_model_provider(
            provider_id=info.id,
            api_key="offline-test-key",
            base_url=info.base_url,
            model_name=info.default_model,
        )
        if info.id == "anthropic":
            assert isinstance(provider, AnthropicMessagesProvider)
        elif info.id == "gemini":
            assert isinstance(provider, GeminiGenerateContentProvider)
        elif info.id == "azure":
            assert isinstance(provider, AzureOpenAIProvider)
        else:
            assert isinstance(provider, HttpOpenAICompatibleProvider)


def test_anthropic_messages_contract_uses_native_headers_and_shape(monkeypatch):
    import core.models.provider_interface as providers

    captured = {}
    monkeypatch.setattr(providers.urllib.request, "urlopen", lambda req, timeout: (
        captured.update(url=req.full_url, headers=dict(req.header_items()), body=json.loads(req.data.decode()))
        or _Response({"content": [{"type": "text", "text": '{"ok": true}'}], "usage": {"input_tokens": 3, "output_tokens": 2}})
    ))
    response = providers.AnthropicMessagesProvider("offline-test-key", "https://anthropic.stub/v1", "claude-test").generate(_request())

    assert captured["url"] == "https://anthropic.stub/v1/messages"
    assert captured["headers"]["X-api-key"] == "offline-test-key"
    assert captured["headers"]["Anthropic-version"] == "2023-06-01"
    assert captured["body"]["system"] == "System instruction"
    assert captured["body"]["messages"] == [{"role": "user", "content": "Return provider-ok"}]
    assert response.parsed_json == {"ok": True}


def test_gemini_generate_content_contract_uses_header_not_query_key(monkeypatch):
    import core.models.provider_interface as providers

    captured = {}
    monkeypatch.setattr(providers.urllib.request, "urlopen", lambda req, timeout: (
        captured.update(url=req.full_url, headers=dict(req.header_items()), body=json.loads(req.data.decode()))
        or _Response({"candidates": [{"content": {"parts": [{"text": "provider-ok"}]}}], "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2}})
    ))
    response = providers.GeminiGenerateContentProvider("offline-test-key", "https://gemini.stub/v1beta", "gemini-test").generate(_request())

    assert captured["url"] == "https://gemini.stub/v1beta/models/gemini-test:generateContent"
    assert "offline-test-key" not in captured["url"]
    assert captured["headers"]["X-goog-api-key"] == "offline-test-key"
    assert captured["body"]["systemInstruction"]["parts"][0]["text"] == "System instruction"
    assert captured["body"]["contents"][0]["role"] == "user"
    assert response.text == "provider-ok"


def test_azure_contract_uses_api_key_header(monkeypatch):
    import core.models.provider_interface as providers

    captured = {}
    monkeypatch.setattr(providers.urllib.request, "urlopen", lambda req, timeout: (
        captured.update(url=req.full_url, headers=dict(req.header_items()), body=json.loads(req.data.decode()))
        or _Response({"choices": [{"message": {"content": "provider-ok"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
    ))
    response = providers.AzureOpenAIProvider("offline-test-key", "https://azure.stub/openai/v1", "deployment").generate(_request())

    assert captured["url"] == "https://azure.stub/openai/v1/chat/completions"
    assert captured["headers"]["Api-key"] == "offline-test-key"
    assert "Authorization" not in captured["headers"]
    assert response.text == "provider-ok"


def test_catalog_fetch_uses_saved_override_and_correct_local_ollama_path(tmp_path, monkeypatch):
    import requests
    from core.llm.keystore import KeyStore
    from core.models.local_model_manager import LocalModelManager

    manager = LocalModelManager(config_path=tmp_path / "registry.json")
    manager.keystore = KeyStore(path=tmp_path / "keys.json", secret_path=tmp_path / "keys.secret")
    manager.keystore.add(name="provider:anthropic", provider="anthropic", api_key="offline-test-key",
                         model="claude-test", base_url="https://anthropic.stub/custom", priority=100)
    calls = []

    class _RequestsResponse:
        status_code = 200
        reason = "OK"

        def json(self):
            return {"data": [{"id": "claude-test"}]}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(requests, "get", lambda url, **kwargs: (calls.append((url, kwargs)) or _RequestsResponse()))
    ok, models, _message = manager.fetch_live_provider_models("anthropic")
    assert ok and models == ["claude-test"]
    assert calls[0][0].startswith("https://anthropic.stub/custom/models")

    calls.clear()

    class _OllamaResponse(_RequestsResponse):
        def json(self):
            return {"models": [{"name": "llama-local"}]}

    monkeypatch.setattr(requests, "get", lambda url, **kwargs: (calls.append((url, kwargs)) or _OllamaResponse()))
    ok, models, _message = manager.fetch_live_provider_models("ollama", base_url="http://localhost:11434/v1")
    assert ok and models == ["llama-local"]
    assert calls[0][0] == "http://localhost:11434/api/tags"


def test_keyless_local_provider_is_selectable_from_keystore(tmp_path, monkeypatch):
    import core.paths as paths
    from core.llm.keystore import KeyStore
    from core.models.provider_interface import HttpOpenAICompatibleProvider, get_model_provider, reset_model_provider
    from core.models.runtime.lfm25_model_locator import LFM25ModelLocator

    monkeypatch.setattr(paths, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(LFM25ModelLocator, "locate_model", staticmethod(lambda: None))
    KeyStore().add(name="provider:ollama", provider="ollama", api_key="", model="llama-local",
                   base_url="http://localhost:11434/v1", priority=100)
    reset_model_provider()
    try:
        provider = get_model_provider(allow_simulation=False)
        assert isinstance(provider, HttpOpenAICompatibleProvider)
        assert provider.api_key == ""
        assert provider.is_available() is True
    finally:
        reset_model_provider()


def test_canonical_router_honors_native_transport_from_saved_provider_config(tmp_path, monkeypatch):
    import core.paths as paths
    from core.llm.keystore import KeyStore
    from core.models.provider_interface import (
        AnthropicMessagesProvider,
        GeminiGenerateContentProvider,
        get_model_provider,
        reset_model_provider,
    )
    from core.models.runtime.lfm25_model_locator import LFM25ModelLocator

    monkeypatch.setattr(paths, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(LFM25ModelLocator, "locate_model", staticmethod(lambda: None))
    store = KeyStore()
    store.add(name="provider:anthropic", provider="anthropic", api_key="offline-test-key",
              model="claude-test", base_url="https://anthropic.stub/v1", priority=100)
    reset_model_provider()
    try:
        assert isinstance(get_model_provider(allow_simulation=False), AnthropicMessagesProvider)
        store.add(name="provider:gemini", provider="gemini", api_key="offline-test-key",
                  model="gemini-test", base_url="https://gemini.stub/v1beta", priority=200)
        reset_model_provider()
        assert isinstance(get_model_provider(allow_simulation=False), GeminiGenerateContentProvider)
    finally:
        reset_model_provider()


def test_environment_router_recognizes_gemini_without_a_generic_openai_fallback(tmp_path, monkeypatch):
    import core.paths as paths
    from core.models.provider_interface import GeminiGenerateContentProvider, get_model_provider, reset_model_provider
    from core.models.runtime.lfm25_model_locator import LFM25ModelLocator

    monkeypatch.setattr(paths, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(LFM25ModelLocator, "locate_model", staticmethod(lambda: None))
    monkeypatch.setenv("GEMINI_API_KEY", "offline-test-key")
    reset_model_provider()
    try:
        assert isinstance(get_model_provider(allow_simulation=False), GeminiGenerateContentProvider)
    finally:
        reset_model_provider()


def test_reset_model_provider_releases_an_active_local_runtime():
    from core.models.provider_interface import reset_model_provider, set_active_model_provider

    class _LoadedRuntime:
        unloaded = False

        def unload(self):
            self.unloaded = True

    runtime = _LoadedRuntime()
    set_active_model_provider(runtime)
    reset_model_provider()

    assert runtime.unloaded is True
