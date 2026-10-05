"""Self-modification helpers — used **by the model**, not by humans.

Hardened version (post-audit):

- ``safe_edit_file``: validates the match exists *before* taking a backup
  (no backup leaks on no-op errors), writes atomically via tmp+fsync+
  rename, preserves the original file mode, and serialises concurrent
  edits per absolute path with a lock. Validation is performed and the
  file is rolled back on parse failure.
- ``apply_patch``: runs ``patch`` with ``--forward --no-backup-if-mismatch
  -r -`` so reject/orig debris never lands on disk.
- ``rollback_file``: takes a "pre-rollback" backup of the *current* state
  before restoring, so a botched rollback is itself reversible.
- Backup retention: only the last ``AGENT_BACKUP_RETAIN`` (default 10)
  ``.bak.<ts>`` files are kept per target.
- Path policing: ``_resolve_inside_root`` rejects anything outside
  ``AGENT_OS_ROOT``; ``tail_log`` further restricts ``log_name`` to
  ``LOGS_DIR``.
- Live-module warning: edits to files known to be already-imported
  (``agent_core.py``, ``api/server.py``, anything under ``core/``)
  succeed but the response notes that a server restart is required.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .paths import AGENT_OS_ROOT, LOGS_DIR


# --------------------------------------------------------------------------
# Constants & shared state
# --------------------------------------------------------------------------
_BACKUP_RETAIN = int(os.environ.get("AGENT_BACKUP_RETAIN", "10"))
_FILE_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

# Files that are imported into the running interpreter — edits succeed but
# need a server restart to take effect.
_LIVE_MODULES = (
    "agent_core.py",
    "api/server.py",
    "core/agent_loop.py",
    "core/api_models.py",
    "core/system_awareness.py",
    "core/self_modify.py",
    "core/self_install.py",
    "core/tools_bridge.py",
)


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOCKS[key] = lock
        return lock


def _is_live_module(p: Path) -> bool:
    rel = str(p.relative_to(AGENT_OS_ROOT)).replace(os.sep, "/")
    return any(rel == lm or rel.endswith("/" + lm) for lm in _LIVE_MODULES)


# --------------------------------------------------------------------------
# Path / backup helpers
# --------------------------------------------------------------------------
def _resolve_inside_root(path: str, *, root: Path | None = None) -> Path:
    base = (root or AGENT_OS_ROOT).resolve()
    p = Path(path)
    p = (base / p).resolve() if not p.is_absolute() else p.resolve()
    try:
        p.relative_to(base)
    except ValueError as e:
        raise ValueError(f"المسار {p} خارج {base}") from e
    return p


def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"


def _backup(path: Path) -> Path:
    bak = path.with_suffix(path.suffix + f".bak.{_ts()}")
    shutil.copy2(path, bak)
    _enforce_retention(path)
    return bak


def _enforce_retention(path: Path) -> None:
    if _BACKUP_RETAIN <= 0:
        return
    backups = sorted(path.parent.glob(f"{path.name}.bak.*"))
    excess = len(backups) - _BACKUP_RETAIN
    for old in backups[:max(0, excess)]:
        try:
            old.unlink()
        except OSError:
            pass


def list_backups(path: str) -> str:
    try:
        p = _resolve_inside_root(path)
    except ValueError as e:
        return f"❌ {e}"
    backups = sorted(p.parent.glob(f"{p.name}.bak.*"))
    if not backups:
        return "(لا توجد نسخ)"
    return "\n".join(str(b.relative_to(AGENT_OS_ROOT)) for b in backups)


def rollback_file(path: str) -> str:
    try:
        p = _resolve_inside_root(path)
    except ValueError as e:
        return f"❌ {e}"
    backups = sorted(p.parent.glob(f"{p.name}.bak.*"))
    if not backups:
        return "❌ لا توجد نسخة احتياطية لاسترجاعها."
    latest = backups[-1]
    with _lock_for(p):
        # Snapshot the *current* state first so the rollback itself is reversible.
        if p.exists():
            try:
                shutil.copy2(p, p.with_suffix(p.suffix + f".bak.{_ts()}.pre-rollback"))
            except OSError:
                pass
        _atomic_replace(p, latest.read_bytes())
    return f"✅ تمت استعادة {p.name} من {latest.name}"


# --------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------
def verify_python(path: str) -> str:
    try:
        p = _resolve_inside_root(path)
        compile(p.read_text(encoding="utf-8"), str(p), "exec")
        return f"✅ {p.name} نحوياً صالح."
    except SyntaxError as e:
        return f"❌ خطأ نحوي في {path}:{e.lineno} — {e.msg}"
    except Exception as e:
        return f"❌ {e}"


def verify_json(path: str) -> str:
    try:
        p = _resolve_inside_root(path)
        json.loads(p.read_text(encoding="utf-8"))
        return f"✅ {p.name} JSON صالح."
    except json.JSONDecodeError as e:
        return f"❌ خطأ JSON في {path}:{e.lineno} — {e.msg}"
    except Exception as e:
        return f"❌ {e}"


def verify_yaml(path: str) -> str:
    try:
        import yaml  # type: ignore
    except ImportError:
        return f"✅ {path} (تخطّي: pyyaml غير مثبَّت)"
    try:
        p = _resolve_inside_root(path)
        yaml.safe_load(p.read_text(encoding="utf-8"))
        return f"✅ {p.name} YAML صالح."
    except Exception as e:
        return f"❌ {e}"


def _validate(path: Path) -> Tuple[bool, str]:
    suffix = path.suffix.lower()
    if suffix == ".py":
        msg = verify_python(str(path))
    elif suffix == ".json":
        msg = verify_json(str(path))
    elif suffix in {".yaml", ".yml"}:
        msg = verify_yaml(str(path))
    else:
        return True, "OK (no validator)"
    return msg.startswith("✅"), msg


# --------------------------------------------------------------------------
# Atomic write
# --------------------------------------------------------------------------
def _atomic_replace(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically, preserving the existing mode."""
    mode: Optional[int] = None
    try:
        mode = path.stat().st_mode
    except OSError:
        pass
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Mutators
# --------------------------------------------------------------------------
def safe_edit_file(path: str, old: str, new: str,
                   replace_all: bool = False) -> str:
    """Replace ``old`` with ``new`` inside ``path`` atomically.

    - If ``old`` is empty the new content overwrites the whole file.
    - Otherwise ``replace_all=False`` (default) requires exactly one match.
    """
    try:
        p = _resolve_inside_root(path)
    except ValueError as e:
        return f"❌ {e}"
    if not p.exists():
        return f"❌ الملف غير موجود: {p}"

    with _lock_for(p):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            return f"❌ تعذرت القراءة: {e}"

        if old == "":
            new_text = new
        else:
            count = text.count(old)
            if count == 0:
                return f"❌ النص المستهدف غير موجود في {p.name}"
            if count > 1 and not replace_all:
                return (
                    f"❌ النص المستهدف يظهر {count} مرات؛ مرر replace_all=true "
                    "أو وسّع المقطع ليكون فريداً."
                )
            new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)

        if new_text == text:
            return f"ℹ️ لا تغيير في {p.name}."

        # Match validated → take backup, then write atomically.
        bak = _backup(p)
        try:
            _atomic_replace(p, new_text.encode("utf-8"))
        except Exception as e:
            try:
                shutil.copy2(bak, p)
            except OSError:
                pass
            return f"❌ خطأ في الكتابة، تمت الاستعادة: {e}"

        ok, msg = _validate(p)
        if not ok:
            shutil.copy2(bak, p)
            return f"❌ فشل التحقق، تمت الاستعادة. {msg}"

        suffix = ""
        if _is_live_module(p):
            suffix = " (⚠️ هذا الملف محمَّل في الذاكرة — أعد تشغيل الخادم لتفعيل التغيير.)"
        return f"✅ تم تعديل {p.name} (نسخة احتياطية: {bak.name}). {msg}{suffix}"


def apply_patch(path: str, unified_diff: str) -> str:
    """Apply a unified diff to ``path`` using GNU ``patch``."""
    try:
        p = _resolve_inside_root(path)
    except ValueError as e:
        return f"❌ {e}"
    if not p.exists():
        return f"❌ الملف غير موجود: {p}"
    if not shutil.which("patch"):
        return "❌ أداة patch غير مثبتة."

    # Normalise CRLF → LF so the patch matches the on-disk content.
    diff = unified_diff.replace("\r\n", "\n")

    with _lock_for(p):
        bak = _backup(p)
        try:
            proc = subprocess.run(
                [
                    "patch",
                    "--forward",
                    "--no-backup-if-mismatch",
                    "--reject-file=-",  # discard rejects rather than write .rej
                    "--silent",
                    str(p),
                ],
                input=diff, text=True,
                capture_output=True, timeout=30,
            )
            if proc.returncode != 0:
                shutil.copy2(bak, p)
                return f"❌ فشل patch: {(proc.stderr or proc.stdout).strip()}"
            ok, msg = _validate(p)
            if not ok:
                shutil.copy2(bak, p)
                return f"❌ patch أُلغي بعد فشل التحقق. {msg}"
            suffix = ""
            if _is_live_module(p):
                suffix = " (⚠️ أعد تشغيل الخادم لتفعيل التغيير.)"
            return f"✅ تم تطبيق patch (نسخة احتياطية: {bak.name}). {msg}{suffix}"
        except subprocess.TimeoutExpired:
            shutil.copy2(bak, p)
            return "❌ انتهت مهلة patch — تمت الاستعادة."
        except Exception as e:
            try:
                shutil.copy2(bak, p)
            except OSError:
                pass
            return f"❌ خطأ، تمت الاستعادة: {e}"


# --------------------------------------------------------------------------
# Health / debug
# --------------------------------------------------------------------------
def run_self_check(targets: Optional[List[str]] = None) -> str:
    targets = targets or ["core", "api", "modes", "agent_core.py"]
    resolved: List[str] = []
    for t in targets:
        try:
            resolved.append(str(_resolve_inside_root(t)))
        except ValueError as e:
            return f"❌ {e}"
    cmd = [sys.executable, "-m", "compileall", "-q", *resolved]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return (
                f"❌ فشل التحقق ({proc.returncode}):\n"
                f"{proc.stdout}\n{proc.stderr}".strip()
            )
        return f"✅ كل الملفات تُجمَّع بنجاح: {', '.join(targets)}"
    except subprocess.TimeoutExpired:
        return "❌ انتهت مهلة compileall."
    except Exception as e:
        return f"❌ {e}"


def tail_log(n: int = 200, log_name: str = "agent.log") -> str:
    try:
        log_path = _resolve_inside_root(log_name, root=Path(LOGS_DIR))
    except ValueError as e:
        return f"❌ {e}"
    if not log_path.exists():
        return f"(لا يوجد سجل في {log_path})"
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, max(64 * 1024, n * 200))
            f.seek(size - chunk, os.SEEK_SET)
            data = f.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-n:])
    except Exception as e:
        return f"❌ {e}"


__all__ = [
    "safe_edit_file",
    "apply_patch",
    "rollback_file",
    "list_backups",
    "verify_python",
    "verify_json",
    "verify_yaml",
    "run_self_check",
    "tail_log",
]
