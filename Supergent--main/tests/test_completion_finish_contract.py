"""Offline transport finish metadata, including safeguards before tool dispatch."""
import json
from types import SimpleNamespace

import pytest

from tests.test_provider_transports import _Response, _request


@pytest.mark.parametrize("kind,body,reason", [
    ("openai", {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}, "length"),
    ("anthropic", {"content": [{"type": "text", "text": "partial"}], "stop_reason": "max_tokens"}, "max_tokens"),
    ("gemini", {"candidates": [{"content": {"parts": [{"text": "partial"}]}, "finishReason": "MAX_TOKENS"}]}, "MAX_TOKENS"),
    ("azure", {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}, "length"),
])
def test_finish_reason_survives_the_native_transport(kind, body, reason, monkeypatch):
    from core.models import provider_interface as module
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda req, timeout: _Response(body))
    cls = {"openai": module.HttpOpenAICompatibleProvider, "anthropic": module.AnthropicMessagesProvider,
           "gemini": module.GeminiGenerateContentProvider, "azure": module.AzureOpenAIProvider}[kind]
    response = cls("offline-test-key", "https://provider.stub/v1", "fixture-model").generate(_request())
    assert response.text == "partial" and response.finish_reason == reason
    assert response.to_dict()["finish_reason"] == reason


def test_absent_finish_metadata_is_unknown_not_invented_stop():
    from core.models.provider_interface import ModelCompletionResponse
    assert ModelCompletionResponse("text").finish_reason is None


def test_truncated_json_action_cannot_execute_even_if_it_parsed(monkeypatch):
    from core.brain.capability_agent import CapabilityAgent
    from core.models.provider_interface import ModelCompletionResponse
    from core.skills.indexer import SkillIndexer
    monkeypatch.setattr(SkillIndexer, "get_index", lambda self: {})
    called = []
    action = {"tool": "native.write_file", "arguments": {"path": "unexpected.txt", "content": "partial"}}
    response = ModelCompletionResponse(json.dumps(action), parsed_json=action, finish_reason="length")
    outcome = CapabilityAgent(SimpleNamespace(generate=lambda request: response),
        router=SimpleNamespace(list_capabilities=lambda: [], execute=lambda *args, **kwargs: called.append(args)),
        max_steps=3).run("Create a file", session_id="truncated")
    assert outcome.error and "truncated" in outcome.error
    assert outcome.metrics["model_requests"] == 1 and not called and not outcome.artifacts
