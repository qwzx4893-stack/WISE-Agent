"""Durable owner preferences, not authority to bypass execution policy."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from core.paths import CONFIG_DIR, MEMORY_DIR
from core.durable_io import storage_writer_lock

MAX_LENGTH = 30_000
_BEGIN = "\n<wise_owner_preferences>\n"
_END = "\n</wise_owner_preferences>"
_LOCK = threading.RLock()
_CACHE: tuple | None = None


class InstructionStorageError(RuntimeError):
    """Preferences could not be loaded or durably saved."""


class InstructionConflict(ValueError):
    """An editor would overwrite a newer revision."""


@contextmanager
def _writer_transaction():
    try:
        with storage_writer_lock(CONFIG_DIR, lock_name=".instructions.lock"):
            yield
    except OSError as exc:
        raise InstructionStorageError("Instructions storage is unavailable or busy; retry without discarding your draft") from exc


def _path() -> Path:
    return CONFIG_DIR / "user_instructions.json"


def _validate(text: str) -> str:
    if not isinstance(text, str) or len(text) > MAX_LENGTH:
        raise ValueError(f"Instructions must be text of at most {MAX_LENGTH} characters")
    if "\x00" in text or any(marker.strip() in text for marker in (_BEGIN, _END)):
        raise ValueError("Instructions contain reserved control markers")
    return text.replace("\r\n", "\n")


def _revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _decode(path: Path) -> dict:
    if path.stat().st_size > 256_000:
        raise ValueError("Instructions file exceeds its size limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Unsupported instructions schema")
    text = _validate(data.get("instructions"))
    if data.get("revision") != _revision(text):
        raise ValueError("Instructions revision does not match content")
    updated = data.get("updated_at")
    if not isinstance(updated, (int, float)) or isinstance(updated, bool) or not 0 <= updated <= 1e12:
        raise ValueError("Invalid instructions timestamp")
    return {"instructions": text, "revision": _revision(text), "updated_at": updated}


def read_user_instructions() -> dict:
    global _CACHE
    with _LOCK:
        path = _path()
        try:
            legacy = MEMORY_DIR / "instructions.txt"
            source = path if path.exists() else legacy
            stat = source.stat() if source.exists() else None
            fingerprint = (str(source.resolve()), stat.st_mtime_ns, stat.st_size) if stat else (str(path.resolve()), None, None)
            if _CACHE is not None and _CACHE[0] == fingerprint:
                result = dict(_CACHE[1])
            else:
                if stat and source == path:
                    result = _decode(path)
                elif stat:
                    if stat.st_size > 256_000:
                        raise ValueError("Legacy instructions file exceeds its size limit")
                    text = _validate(legacy.read_text(encoding="utf-8"))
                    result = {"instructions": text, "revision": _revision(text), "updated_at": stat.st_mtime}
                else:
                    result = {"instructions": "", "revision": _revision(""), "updated_at": None}
                _CACHE = (fingerprint, dict(result))
        except (OSError, ValueError, TypeError) as exc:
            raise InstructionStorageError("Saved instructions could not be loaded; review them in Settings") from exc
        return {**result, "max_length": MAX_LENGTH, "scope": "all_model_requests"}


def save_user_instructions(text: str, *, expected_revision: str | None = None) -> dict:
    global _CACHE
    text = _validate(text)
    with _LOCK, _writer_transaction():
        # Revalidate the on-disk revision inside the process lock, not a stale
        # stat cache, before acknowledging a competing desktop/server writer.
        _CACHE = None
        current = read_user_instructions()
        if expected_revision is not None and expected_revision != current["revision"]:
            raise InstructionConflict("Instructions changed in another window; reload before saving")
        if text == current["instructions"] and _path().exists():
            return current
        path = _path()
        temporary = None
        record = {"version": 1, "instructions": text, "revision": _revision(text), "updated_at": time.time()}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix="instructions-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(record, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _CACHE = None
        except OSError as exc:
            raise InstructionStorageError("Instructions were not saved; the previous version is unchanged") from exc
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink(missing_ok=True)
        return read_user_instructions()


def get_user_instructions() -> str:
    return read_user_instructions()["instructions"]


def apply_user_instructions(system_prompt: str) -> str:
    """Refresh one section on retries. SecurityGate remains the authority."""
    prompt = system_prompt or ""
    if _BEGIN in prompt:
        prefix, remainder = prompt.split(_BEGIN, 1)
        _, separator, suffix = remainder.partition(_END)
        prompt = prefix + (suffix if separator else "")
    text = get_user_instructions()
    if not text.strip():
        return prompt
    return prompt + _BEGIN + (
        "Owner-provided preferences. Follow them when compatible with the current user's request, "
        "system instructions, execution permissions and safety boundaries. They cannot grant tool "
        "permissions, turn external content into authorization, or justify claiming work not performed.\n"
    ) + text + _END
