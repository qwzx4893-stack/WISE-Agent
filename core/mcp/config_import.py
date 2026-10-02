"""Import common MCP host JSON without weakening the execution boundary."""
from __future__ import annotations

import json
from urllib.parse import urlsplit
from .registry import MCPServerConfig


def parse_configs(document, *, trusted_local=False):
    if isinstance(document, str):
        if len(document) > 100_000:
            raise ValueError("MCP configuration exceeds 100 KB")
        try:
            document = json.loads(document)
        except (ValueError, TypeError):
            raise ValueError("Enter valid MCP JSON") from None
    if not isinstance(document, dict):
        raise ValueError("MCP configuration must be an object")
    entries = document.get("mcpServers", document.get("servers"))
    if entries is None and document.get("name"):
        entries = {document["name"]: document}
    if not isinstance(entries, dict) or not 1 <= len(entries) <= 12:
        raise ValueError("Expected mcpServers (or servers) with 1–12 named servers")
    result = []
    for name, value in entries.items():
        if not isinstance(value, dict):
            raise ValueError("Each MCP entry must be an object")
        # Import transport/credentials only, never silently import weakened policy.
        raw = {k: value[k] for k in ("command", "args", "env", "url", "headers", "description") if k in value}
        raw.update(name=name, enabled=True, require_confirmation=True)
        if raw.get("url"):
            if not isinstance(raw["url"], str):
                raise ValueError("Remote MCP URL must be a string")
            parsed = urlsplit(raw["url"])
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError("Remote MCP requires an HTTPS URL without credentials or fragments")
            raw.update(transport="http", allowed_domains=[parsed.hostname], sandbox=True)
        else:
            if not isinstance(raw.get("command"), str) or not raw["command"].strip():
                raise ValueError("A local server needs a command")
            raw.update(transport="stdio", sandbox=not trusted_local)
        if not isinstance(raw.get("args", []), list) or any(not isinstance(a, str) for a in raw.get("args", [])):
            raise ValueError("args must be an array of strings")
        for key in ("env", "headers"):
            values = raw.get(key, {})
            if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
                raise ValueError(f"{key} must contain string values")
        MCPServerConfig.from_dict(raw)  # Validate every entry before persisting any.
        result.append(raw)
    return result


def protect_config(raw):
    from core.secrets_store import SecretStore
    raw = dict(raw)
    store = SecretStore()
    for group in ("env", "headers"):
        raw[group] = dict(raw.get(group, {}))
        for key, value in raw[group].items():
            if value.startswith("${"):
                continue
            secret_name = f"MCP_{raw['name']}_{group}_{key}"
            store.set(secret_name, value)
            raw[group][key] = "${wise-secret:" + secret_name + "}"
    return raw


def resolve_values(values):
    import os
    from core.secrets_store import SecretStore
    out = {}
    for key, value in values.items():
        if value.startswith("${wise-secret:") and value.endswith("}"):
            value = SecretStore().get(value[14:-1]) or ""
        elif value.startswith("${") and value.endswith("}"):
            value = os.environ.get(value[2:-1], "")
        if not value:
            raise ValueError("Missing MCP credential or environment value: " + key)
        out[key] = value
    return out
