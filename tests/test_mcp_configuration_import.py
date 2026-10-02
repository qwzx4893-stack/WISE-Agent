"""Offline tests: MCP import cannot silently weaken policy or leak credentials."""
import pytest
from core.mcp.config_import import parse_configs, protect_config, resolve_values


def test_http_import_cannot_disable_confirmation_or_expand_hosts():
    cfg = parse_configs({"mcpServers": {"docs": {"url": "https://docs.example.com/mcp",
        "require_confirmation": False, "sandbox": False, "allowed_domains": ["*"]}}})[0]
    assert cfg["require_confirmation"] is True
    assert cfg["allowed_domains"] == ["docs.example.com"]
    assert cfg["sandbox"] is True


def test_stdio_requires_explicit_trust_to_disable_isolation():
    data = {"servers": {"local": {"command": "python", "args": ["server.py"], "sandbox": False}}}
    assert parse_configs(data)[0]["sandbox"] is True
    assert parse_configs(data, trusted_local=True)[0]["sandbox"] is False


@pytest.mark.parametrize("value", [[], "bad JSON", {"mcpServers": {}},
    {"mcpServers": {"bad": {"url": 42}}},
    {"mcpServers": {"bad": {"url": "http://example.com"}}},
    {"mcpServers": {"bad": {"url": "https://user:password@example.com"}}},
    {"mcpServers": {"bad": {"command": "python", "args": "-m unsafe"}}},
    {"mcpServers": {"bad": {"command": "python", "env": {"TOKEN": 10}}}}])
def test_invalid_documents_fail_before_persistence(value):
    with pytest.raises(ValueError):
        parse_configs(value)


def test_inline_values_are_protected_and_resolved_only_at_runtime(monkeypatch):
    import core.secrets_store as secrets
    values = {}
    class Store:
        def set(self, key, value): values[key] = value
        def get(self, key): return values.get(key)
    monkeypatch.setattr(secrets, "SecretStore", Store)
    cfg = protect_config({"name": "docs", "headers": {"Authorization": "Bearer test-secret"},
                          "env": {"API_KEY": "test-key"}})
    assert "test-secret" not in repr(cfg)
    assert "test-key" not in repr(cfg)
    assert resolve_values(cfg["headers"]) == {"Authorization": "Bearer test-secret"}
    assert resolve_values(cfg["env"]) == {"API_KEY": "test-key"}
    with pytest.raises(ValueError, match="Missing"):
        resolve_values({"TOKEN": "${wise-secret:missing}"})
