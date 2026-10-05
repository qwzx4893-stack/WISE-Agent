"""QA-only transparent, bounded observation of actual public tool results."""
from __future__ import annotations
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_lock = threading.Lock()
_secret = re.compile(r"(?:sk-or-v1-|ghp_|github_pat_)[A-Za-z0-9_-]+|\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I)


def redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if str(key).lower() in {
            "authorization", "api_key", "access_token", "refresh_token", "password", "secret", "token"
        } else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return _secret.sub("[REDACTED]", value) if isinstance(value, str) else value


def record(destination, tool, result):
    if tool not in {"native.web_fetch", "native.web_search", "intelligence.duckdb"} and not tool.startswith("source."):
        return
    observed = {"tool": tool, "retrieved_at": datetime.now(timezone.utc).isoformat(), "result": redact(result.to_dict())}
    line = json.dumps(observed, ensure_ascii=False, default=str)
    if len(line.encode("utf-8")) > 300000:
        raise ValueError("QA tool observation exceeds its explicit evidence budget")
    with _lock, destination.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def install(destination):
    destination = Path(destination).resolve()
    if ROOT / "qa-results" not in destination.parents or destination.suffix != ".ndjson":
        raise ValueError("QA observations require an owned qa-results NDJSON path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    from core.capability_router import CapabilityRouter
    original = CapabilityRouter.execute
    def observed(self, id_or_name, *args, **kwargs):
        result = original(self, id_or_name, *args, **kwargs)
        record(destination, id_or_name, result)
        return result
    CapabilityRouter.execute = observed
