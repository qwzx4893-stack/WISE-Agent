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
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # Windows-only optional dependency supplied by pywin32.
    import win32crypt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on non-Windows hosts.
    win32crypt = None


_LOCK = threading.Lock()


class SecretStorageError(RuntimeError):
    """Credential storage failed without discarding existing data."""


def _checked_path(path: Path) -> os.stat_result | None:
    """Reject existing links/reparse points and multiply linked secret files.

    The runtime directory still needs a trusted owner and appropriate ACLs:
    path checks cannot exclude a concurrent writer with directory access.
    """
    absolute = Path(os.path.abspath(path))
    result = None
    for part in reversed((absolute, *absolute.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SecretStorageError("Credential paths cannot contain links or reparse points")
        if part == absolute:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SecretStorageError("Credential file must be a regular file with one link")
            result = info
        elif not stat.S_ISDIR(info.st_mode):
            raise SecretStorageError("Credential parent must be a directory")
    return result


def _checked_read(path: Path) -> bytes | None:
    path = Path(os.path.abspath(path))
    info = _checked_path(path)
    if info is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                    or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1):
                raise SecretStorageError("Credential file changed while opening it")
            return stream.read()
    except OSError as exc:
        raise SecretStorageError("Could not read credential storage") from exc


def _atomic_write(path: Path, content: bytes) -> None:
    """Use an exclusive unpredictable sibling; never open a fixed .tmp path."""
    path = Path(os.path.abspath(path))
    _checked_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _checked_path(path)
    fd, filename = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(fd, "wb") as stream:
            # POSIX mkstemp already creates mode 0600. Windows permissions are
            # inherited ACLs; chmod is not an ACL or user-isolation guarantee.
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _checked_path(temporary)
        _checked_path(path)
        os.replace(temporary, path)
    except OSError as exc:
        raise SecretStorageError("Could not write credential storage") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _load_legacy_secret(path: Path, store_path: Path) -> bytes | None:
    path = Path(os.path.abspath(path))
    store_path = Path(os.path.abspath(store_path))
    if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(store_path)):
        raise SecretStorageError("Credential store and legacy secret material must use distinct paths")
    _checked_path(store_path)
    material = _checked_read(path)
    if material is not None:
        if len(material) != 32:
            raise SecretStorageError("Invalid legacy secret material; preserve the original for recovery")
        return material
    # A replacement sidecar cannot recover old XOR entries. Leave both files
    # untouched so an operator can restore the original material from backup.
    if store_path.exists():
        return None
    material = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _checked_path(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(material)
    except FileExistsError:
        material = _checked_read(path)
        if material is None or len(material) != 32:
            raise SecretStorageError("Invalid legacy secret material")
    except OSError as exc:
        raise SecretStorageError("Could not create legacy secret material") from exc
    return material


def _protect_windows(value: str, description: str) -> str:
    if win32crypt is None:
        raise SecretStorageError("Windows DPAPI is unavailable; refusing to store a secret")
    try:
        protected = win32crypt.CryptProtectData(value.encode("utf-8"), description, None, None, None, 0)
        if not isinstance(protected, bytes) or not protected:
            raise ValueError("Empty DPAPI output")
        return "dpapi:" + base64.urlsafe_b64encode(protected).decode("ascii")
    except Exception as exc:
        # Do not include the input secret or a dependency's error text.
        raise SecretStorageError("Windows DPAPI protection failed; credential was not saved") from exc


def _decode_legacy(value: str, material: bytes | None) -> str:
    try:
        raw = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeError):
        return value  # Historical plaintext entries, when unambiguously not base64.
    if material is None:
        return ""
    try:
        return _xor(raw, material).decode("utf-8")
    except UnicodeError:
        return ""


def _read_json(path: Path, collection: str) -> Dict[str, Any]:
    content = _checked_read(path)
    if content is None:
        return {"version": 1, collection: [] if collection == "keys" else {}}
    try:
        data = json.loads(content.decode("utf-8"))
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] not in (1, 2):
            raise ValueError("Invalid credential schema")
        entries = data[collection]
        if collection == "keys":
            if not isinstance(entries, list) or any(not isinstance(k, dict) or not isinstance(k.get("api_key", ""), str) for k in entries):
                raise ValueError("Invalid credential entries")
        elif not isinstance(entries, dict) or any(not isinstance(v, str) for v in entries.values()):
            raise ValueError("Invalid secret entries")
        return data
    except (ValueError, KeyError, UnicodeError) as exc:
        raise SecretStorageError("Invalid credential storage; existing file was preserved") from exc


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
    def _load_secret(self) -> bytes | None:
        return _load_legacy_secret(self.secret_path, self.path)

    # ------------------------------------------------------------------
    # Encoding
    # ------------------------------------------------------------------
    def _enc(self, value: str) -> str:
        if not value:
            return ""
        if os.name == "nt":
            return _protect_windows(value, "WISE LLM credential")
        if self._secret is None:
            raise SecretStorageError("Legacy secret material is unavailable; refusing to save a secret")
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
        return _decode_legacy(value, self._secret)

    # ------------------------------------------------------------------
    # File IO
    # ------------------------------------------------------------------
    def _read(self) -> Dict[str, Any]:
        return _read_json(self.path, "keys")

    def _write(self, data: Dict[str, Any]) -> None:
        _atomic_write(self.path, json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))

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
                    decoded = self._dec(encoded)
                    if not decoded:
                        # Never replace an inaccessible legacy key with an
                        # empty string or mark that file as migrated.
                        return
                    entry["api_key"] = self._enc(decoded)
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
