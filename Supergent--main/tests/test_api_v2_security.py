"""Regression coverage for the unified API access boundary."""

from __future__ import annotations

from starlette.requests import Request


def _request(host: str, headers=None) -> Request:
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/v2/chat",
        "headers": raw_headers,
        "client": (host, 12345),
        "scheme": "http",
    })


def test_tokenless_access_is_limited_to_loopback(monkeypatch):
    from api.server import _request_is_authorized

    monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
    assert _request_is_authorized(_request("127.0.0.1")) is True
    assert _request_is_authorized(_request("198.51.100.10")) is False


def test_configured_token_is_required_even_for_local_v2_requests(monkeypatch):
    from api.server import _request_is_authorized

    monkeypatch.setenv("AGENT_API_TOKEN", "correct-secret")
    assert _request_is_authorized(_request("127.0.0.1")) is False
    assert _request_is_authorized(_request("127.0.0.1", {
        "x-agent-token": "wrong-secret",
    })) is False
    assert _request_is_authorized(_request("127.0.0.1", {
        "authorization": "Bearer correct-secret",
    })) is True


def test_v2_get_model_listing_cannot_accept_api_key_in_a_url():
    import inspect
    from api.server import v2_fetch_live_models_get

    assert "api_key" not in inspect.signature(v2_fetch_live_models_get).parameters


def test_gateway_setup_status_is_derived_from_the_live_provider(monkeypatch):
    from api.server import _handle_gateway_jsonrpc
    from core.contracts import ModelRuntimeTelemetry

    monkeypatch.setattr(
        "core.models.provider_interface.get_model_runtime_telemetry",
        lambda allow_simulation=False: ModelRuntimeTelemetry(
            active_provider="UnavailableModelProvider",
            model_name="unavailable",
            is_simulated=False,
            total_ram_gb=8,
            available_ram_gb=2,
            ram_percent=75,
            load_state="UNAVAILABLE",
        ),
    )
    status = _handle_gateway_jsonrpc("setup.status", {})
    assert status["ready"] is False
    assert status["provider_configured"] is False
    assert status["inference_provider"] == "UnavailableModelProvider"


def test_compatibility_metadata_does_not_invent_a_ready_model(monkeypatch):
    from api.server import _handle_gateway_jsonrpc

    monkeypatch.setattr(
        "api.server._live_model_snapshot",
        lambda: {
            "ready": False,
            "provider": "unavailable",
            "model": "unavailable",
            "load_state": "UNAVAILABLE",
            "context_length": None,
        },
    )

    auxiliary = _handle_gateway_jsonrpc("model.auxiliary", {})
    moa = _handle_gateway_jsonrpc("model.moa", {})

    assert auxiliary["main"]["model"] == "unavailable"
    assert auxiliary["main"]["ready"] is False
    assert moa["enabled"] is False
    assert moa["aggregator"]["enabled"] is False


def test_compatibility_capability_status_is_not_preapproved():
    from api.server import _compat_computer_use_status, _compat_terminal_backends

    computer = _compat_computer_use_status()
    terminal = _compat_terminal_backends()

    assert computer["granted"] is False
    assert "sandbox_enforced" in terminal["backends"][0]
