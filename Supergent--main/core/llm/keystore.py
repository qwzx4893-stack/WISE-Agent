"""Persistent credential store for LLM API keys.

On Windows, API keys are encrypted using DPAPI for the current user before
they are written to ``MEMORY_DIR/keys.json``.  This binds the secret to the
Windows account that configured WISE and removes the recoverable XOR-only
storage that earlier builds used.  Legacy XOR-obfuscated entries are migrated
on the next KeyStore initialization.  Non-Windows deployments retain the
legacy compatibility codec and should prefer an environment secret manager.

Schema (``keys.json``):

```
{
  "version": 2,
  "keys": [
    {
      "name":      "primary",
      "provider":  "openrouter",
      "model":     "openrouter/auto",
      "base_url":  "https://openrouter.ai/api/v1",
      "api_key":   "dpapi:<base64>",
      "enabled":   true,
      "priority":  10
    }
  ]
}
```
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # Windows-only optional dependency supplied by pywin32.
    import win32crypt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on non-Windows hosts.
    win32crypt = None


_LOCK = threading.Lock()


def _xor(data: bytes, key: bytes) -> bytes:
    if not key:
        return data
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "…" + value[-4:]


class KeyStore:
    """Persistent JSON store for LLM credentials."""

    def __init__(self, path: Path | None = None,
                 secret_path: Path | None = None):
        if path is None:
            from ..paths import MEMORY_DIR

            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            path = MEMORY_DIR / "keys.json"
            secret_path = MEMORY_DIR / ".keystore_secret"
        self.path = Path(path)
        self.secret_path = Path(secret_path) if secret_path else self.path.with_suffix(".secret")
        self._secret = self._load_secret()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_entries()

    # ------------------------------------------------------------------
    # Secret material
    # ------------------------------------------------------------------
    def _load_secret(self) -> bytes:
        if self.secret_path.exists():
            return self.secret_path.read_bytes()
        s = secrets.token_bytes(32)
        self.secret_path.parent.mkdir(parents=True, exist_ok=True)
        self.secret_path.write_bytes(s)
        try:
            os.chmod(self.secret_path, 0o600)
        except Exception:
            pass
        return s

    # ------------------------------------------------------------------
    # Encoding
    # ------------------------------------------------------------------
    def _enc(self, value: str) -> str:
        if not value:
            return ""
        if os.name == "nt" and win32crypt is not None:
            protected = win32crypt.CryptProtectData(
                value.encode("utf-8"),
                "WISE LLM credential",
                None,
                None,
                None,
                0,
            )
            return "dpapi:" + base64.urlsafe_b64encode(protected).decode("ascii")
        raw = _xor(value.encode("utf-8"), self._secret)
        return base64.urlsafe_b64encode(raw).decode("ascii")

    def _dec(self, value: str) -> str:
        if not value:
            return ""
        if value.startswith("dpapi:"):
            if os.name != "nt" or win32crypt is None:
                return ""
            try:
                protected = base64.urlsafe_b64decode(value.removeprefix("dpapi:").encode("ascii"))
                return win32crypt.CryptUnprotectData(protected, None, None, None, 0)[1].decode("utf-8")
            except Exception:
                # Do not fall back to treating ciphertext as a usable key.
                return ""
        try:
            raw = base64.urlsafe_b64decode(value.encode("ascii"))
        except Exception:
            return value  # plain — accept legacy unmigrated entries
        return _xor(raw, self._secret).decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # File IO
    # ------------------------------------------------------------------
    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "keys": []}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "keys": []}

    def _write(self, data: Dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, self.path)

    def _migrate_legacy_entries(self) -> None:
        """Upgrade recoverable legacy credentials to Windows DPAPI in place."""
        if os.name != "nt" or win32crypt is None or not self.path.exists():
            return
        with _LOCK:
            data = self._read()
            changed = data.get("version") != 2
            for entry in data.get("keys", []):
                encoded = str(entry.get("api_key", ""))
                if encoded and not encoded.startswith("dpapi:"):
                    entry["api_key"] = self._enc(self._dec(encoded))
                    changed = True
            if changed:
                data["version"] = 2
                self._write(data)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def list(self, *, reveal: bool = False) -> List[Dict[str, Any]]:
        data = self._read()
        out: List[Dict[str, Any]] = []
        for k in data.get("keys", []):
            decrypted = self._dec(k.get("api_key", ""))
            entry = {
                "name": k.get("name"),
                "provider": k.get("provider"),
                "model": k.get("model"),
                "base_url": k.get("base_url"),
                "enabled": k.get("enabled", True),
                "priority": k.get("priority", 0),
                "api_key": decrypted if reveal else _mask(decrypted),
            }
            out.append(entry)
        return out

    def get(self, name: str, *, reveal: bool = True) -> Optional[Dict[str, Any]]:
        for k in self.list(reveal=reveal):
            if k["name"] == name:
                return k
        return None

    def get_for_provider(self, provider: str, *, reveal: bool = True) -> Optional[Dict[str, Any]]:
        """Return the highest-priority configured credential for *provider*.

        The v2 model settings screen addresses providers (``openrouter``),
        whereas the legacy admin API addresses named credentials.  Keeping
        this lookup here prevents those two surfaces from drifting apart.
        """
        matches = [
            entry for entry in self.list(reveal=reveal)
            if entry.get("provider") == provider
        ]
        if not matches:
            return None
        matches.sort(key=lambda entry: int(entry.get("priority", 0) or 0), reverse=True)
        return matches[0]

    def add(self, *, name: str, api_key: str = "",
            provider: Optional[str] = None,
            model: Optional[str] = None,
            base_url: Optional[str] = None,
            enabled: bool = True,
            priority: int = 0) -> Dict[str, Any]:
        from .providers import detect_provider, get_provider

        p = get_provider(provider) if provider else None
        if p is None:
            p = detect_provider(api_key, base_url)
        provider_id = p.id
        base_url = base_url or p.base_url
        model = model or p.default_model

        with _LOCK:
            data = self._read()
            entry = {
                "name": name,
                "provider": provider_id,
                "model": model,
                "base_url": base_url,
                "api_key": self._enc(api_key),
                "enabled": enabled,
                "priority": priority,
            }
            keys = [k for k in data.get("keys", []) if k.get("name") != name]
            keys.append(entry)
            data["version"] = 2 if os.name == "nt" and win32crypt is not None else 1
            data["keys"] = keys
            self._write(data)
        # Return a public view (masked key) for confirmation.
        view = entry.copy()
        view["api_key"] = _mask(api_key)
        return view

    def remove(self, name: str) -> bool:
        with _LOCK:
            data = self._read()
            before = len(data.get("keys", []))
            data["keys"] = [k for k in data.get("keys", []) if k.get("name") != name]
            self._write(data)
            return len(data["keys"]) < before

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with _LOCK:
            data = self._read()
            found = False
            for k in data.get("keys", []):
                if k.get("name") == name:
                    k["enabled"] = bool(enabled)
                    found = True
                    break
            if found:
                self._write(data)
            return found


__all__ = ["KeyStore"]
