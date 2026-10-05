"""Regression tests for WISE's live-provider and Brave browser paths."""

from __future__ import annotations

import base64
from collections import Counter
import json
import os
from pathlib import Path


def test_v2_external_provider_persists_full_openrouter_configuration(tmp_path):
    from core.llm.keystore import KeyStore
    from core.models.local_model_manager import LocalModelManager

    manager = LocalModelManager(config_path=tmp_path / "model_registry.json")
    manager.keystore = KeyStore(
        path=tmp_path / "keys.json",
        secret_path=tmp_path / "keys.secret",
    )

    ok, message = manager.configure_external_provider(
        provider_id="openrouter",
        api_key="test-openrouter-key",
        model="openrouter/free",
        base_url="https://openrouter.ai/api/v1",
        set_as_active=True,
    )

    assert ok, message
    configured = manager.keystore.get_for_provider("openrouter", reveal=True)
    assert configured is not None
    assert configured["api_key"] == "test-openrouter-key"
    assert configured["model"] == "openrouter/free"
    assert configured["base_url"] == "https://openrouter.ai/api/v1"
    assert configured["priority"] == 100
    status = next(item for item in manager.list_external_providers() if item["id"] == "openrouter")
    assert status["has_api_key"] is True


def test_model_settings_persist_runtime_values_and_disconnect_provider(tmp_path, monkeypatch):
    from core.llm.keystore import KeyStore
    from core.models.local_model_manager import LocalModelManager

    manager = LocalModelManager(config_path=tmp_path / "model_registry.json")
    manager.keystore = KeyStore(path=tmp_path / "keys.json", secret_path=tmp_path / "keys.secret")
    resets = []
    monkeypatch.setattr(manager, "_reset_canonical_provider", lambda: resets.append(True))

    gguf = tmp_path / "wise-test.gguf"
    gguf.write_bytes(b"GGUF-test-fixture")
    ok, message, model = manager.import_local_model(str(gguf), set_as_default=True)
    assert ok, message
    assert model is not None
    model_id = model["id"]
    ok, message = manager.set_active_local_model(model_id)
    assert ok, message
    ok, message = manager.configure_runtime_params(model_id, {
        "n_ctx": 8192,
        "n_gpu_layers": 21,
        "threads": 6,
        "temperature": 0.35,
    })
    assert ok, message
    configured = next(item for item in manager.list_local_models() if item["id"] == model_id)
    assert configured["runtime_params"] == {
        "n_ctx": 8192,
        "n_gpu_layers": 21,
        "threads": 6,
        "temperature": 0.35,
    }

    ok, message = manager.configure_external_provider(
        provider_id="openrouter",
        api_key="offline-key",
        model="openrouter/free",
        set_as_active=True,
    )
    assert ok, message
    ok, message = manager.disconnect_external_provider("openrouter")
    assert ok, message
    assert manager.keystore.get_for_provider("openrouter", reveal=True) is None
    assert resets


def test_windows_keystore_uses_dpapi_and_migrates_legacy_entries(tmp_path):
    if os.name != "nt":
        return

    from core.llm.keystore import KeyStore

    key_path = tmp_path / "keys.json"
    secret_path = tmp_path / "keys.secret"
    legacy = KeyStore(path=key_path, secret_path=secret_path)
    # Create a representative pre-DPAPI entry without ever storing plaintext.
    # _enc currently creates DPAPI, so emulate the historical XOR encoding.
    from core.llm.keystore import _xor
    legacy_encoded = base64.urlsafe_b64encode(
        _xor(b"migration-key", secret_path.read_bytes())
    ).decode("ascii")
    key_path.write_text(json.dumps({"version": 1, "keys": [{
        "name": "legacy", "provider": "openrouter", "api_key": legacy_encoded,
    }]}), encoding="utf-8")

    migrated = KeyStore(path=key_path, secret_path=secret_path)
    raw = json.loads(key_path.read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert raw["keys"][0]["api_key"].startswith("dpapi:")
    assert migrated.get("legacy")["api_key"] == "migration-key"


def test_canonical_provider_uses_keystore_configuration(tmp_path, monkeypatch):
    import core.paths as paths
    from core.llm.keystore import KeyStore
    from core.models.provider_interface import (
        HttpOpenAICompatibleProvider,
        get_model_provider,
        reset_model_provider,
    )
    from core.models.runtime.lfm25_model_locator import LFM25ModelLocator

    monkeypatch.setattr(paths, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(LFM25ModelLocator, "locate_model", staticmethod(lambda: None))
    KeyStore().add(
        name="provider:openrouter",
        api_key="test-openrouter-key",
        provider="openrouter",
        model="openrouter/free",
        base_url="https://openrouter.ai/api/v1",
        priority=100,
    )
    reset_model_provider()
    try:
        provider = get_model_provider(allow_simulation=False)
        assert isinstance(provider, HttpOpenAICompatibleProvider)
        assert provider.model_name == "openrouter/free"
        assert provider.base_url == "https://openrouter.ai/api/v1"
    finally:
        reset_model_provider()


def test_brave_is_the_default_and_resolves_to_an_explicit_executable(tmp_path, monkeypatch):
    import core.browser.browser_session as browser_session
    from core.browser.browser_models import BrowserSessionConfig
    from core.browser.browser_session import BrowserSession

    brave = tmp_path / "brave.exe"
    brave.write_bytes(b"test")
    monkeypatch.setattr(browser_session, "_BRAVE_PATHS", (brave,))

    session = BrowserSession(BrowserSessionConfig(headless=True))
    kwargs = session._launch_kwargs()

    assert session.config.channel == "brave"
    assert kwargs["executable_path"] == str(brave)
    assert kwargs["headless"] is True


def test_v2_routes_have_one_websocket_contract_per_path():
    from api.server import app

    paths = [route.path for route in app.routes if getattr(route, "path", None)]
    assert Counter(paths)["/api/v2/stream"] == 1
    assert Counter(paths)["/api/v2/chat/stream"] == 1


def test_truncated_planning_json_is_never_returned_as_a_chat_answer():
    from core.brain.cognitive_decision_engine import CognitiveDecisionEngine
    from core.models.provider_interface import (
        ModelCompletionResponse,
        ModelProviderType,
        SimulatedTestProvider,
    )

    class TruncatedPlanningProvider(SimulatedTestProvider):
        def generate(self, request):
            return ModelCompletionResponse(
                text='{"can_answer_directly": true, "direct_answer":',
                provider_type=ModelProviderType.CLOUD_OPENAI_COMPATIBLE,
                model_name="test-live-provider",
                is_simulated=False,
            )

    decision = CognitiveDecisionEngine(
        provider=TruncatedPlanningProvider(),
    ).preflight_analyze("Reply with exactly WISE_LIVE_OK and nothing else.")

    assert decision.can_answer_directly is True
    assert decision.direct_answer is None
    assert decision.selected_capabilities == ["none"]


def test_unconfigured_production_vision_reports_unavailable_not_simulated(monkeypatch):
    import core.vision.vlm_engine as vlm

    monkeypatch.delenv("WISE_VLM_API_KEY", raising=False)
    monkeypatch.delenv("WISE_VLM_BASE_URL", raising=False)
    monkeypatch.delenv("WISE_LLM_API_KEY", raising=False)
    monkeypatch.setattr(vlm, "_ACTIVE_VLM_PROVIDER", None)

    provider = vlm.get_vlm_provider()
    result = provider.analyze_image(b"", "Inspect this image")

    assert provider.is_available() is False
    assert result.is_simulated is False
    assert result.error
