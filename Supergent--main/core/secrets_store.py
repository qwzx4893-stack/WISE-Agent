"""Persistent store for arbitrary named secrets (GitHub PAT, channel tokens…).

Uses user-bound Windows DPAPI when available, with backward-compatible
legacy XOR decoding. Non-Windows fallback is obfuscation, not strong
encryption; shared deployments should use an operating-system vault or KMS.

Stored at ``MEMORY_DIR/secrets.json``. Schema:

```
{"version": 1, "items": {"github_token": "<xor-b64>", ...}}
```
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Dict, Optional

from .paths import MEMORY_DIR

_LOCK = threading.RLock()


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


class SecretStore:
    """Singleton-like JSON store for arbitrary named secrets."""

    _instance: Optional["SecretStore"] = None

    def __new__(cls) -> "SecretStore":
        with _LOCK:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()  # type: ignore[attr-defined]
            return cls._instance

    def _init(self) -> None:
        self.path: Path = MEMORY_DIR / "secrets.json"
        self.secret_path: Path = MEMORY_DIR / ".secrets_secret"
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self._secret = self._load_secret()

    # ------------------------------------------------------------------
    def _load_secret(self) -> bytes:
        if self.secret_path.exists():
            return self.secret_path.read_bytes()
        material = secrets.token_bytes(32)
        self.secret_path.write_bytes(material)
        try:
            os.chmod(self.secret_path, 0o600)
        except Exception:
            pass
        return material

    def _enc(self, value: str) -> str:
        if not value:
            return ""
        if os.name == "nt":
            from core.llm.keystore import win32crypt
            if win32crypt is not None:
                protected = win32crypt.CryptProtectData(value.encode("utf-8"), "WISE connection secret", None, None, None, 0)
                return "dpapi:" + base64.urlsafe_b64encode(protected).decode("ascii")
        return base64.urlsafe_b64encode(
            _xor(value.encode("utf-8"), self._secret)).decode("ascii")

    def _dec(self, value: str) -> str:
        if value.startswith("dpapi:"):
            from core.llm.keystore import win32crypt
            if os.name != "nt" or win32crypt is None:
                return ""
            try:
                return win32crypt.CryptUnprotectData(base64.urlsafe_b64decode(value[6:]), None, None, None, 0)[1].decode("utf-8")
            except Exception:
                return ""
        if not value:
            return ""
        try:
            raw = base64.urlsafe_b64decode(value.encode("ascii"))
        except Exception:
            return value
        return _xor(raw, self._secret).decode("utf-8", errors="replace")

    # ------------------------------------------------------------------ IO
    def _read(self) -> Dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data.get("items") or {}
        except Exception:
            return {}

    def _write(self, items: Dict[str, str]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"version": 1, "items": items},
                                  indent=2, ensure_ascii=False),
                       encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ API
    def get(self, name: str) -> Optional[str]:
        """Return the decoded secret. ``None`` if unset.

        Environment variables override the persisted store, so operators
        can always shadow a saved secret without touching the file.
        """
        env = os.environ.get(name.upper())
        if env:
            return env
        with _LOCK:
            return self._dec(self._read().get(name, "")) or None

    def set(self, name: str, value: str) -> None:
        with _LOCK:
            items = self._read()
            items[name] = self._enc(value)
            self._write(items)

    def delete(self, name: str) -> bool:
        with _LOCK:
            items = self._read()
            if name in items:
                items.pop(name)
                self._write(items)
                return True
            return False

    def list(self, *, reveal: bool = False) -> Dict[str, str]:
        with _LOCK:
            items = self._read()
            return {n: (self._dec(v) if reveal else _mask(self._dec(v)))
                    for n, v in items.items()}


def get_secret(name: str) -> Optional[str]:
    return SecretStore().get(name)


__all__ = ["SecretStore", "get_secret"]
