"""
Agent OS Self-Maintenance Sidecar (v3.0)

Safe, syntactically valid implementation of the self-maintenance helpers used
by Agent OS. Intentionally avoids circular imports from ``agent_core`` — the
caller can pass the runtime sandbox/log function explicitly.

Public surface used by ``agent_core.main``:

- ``SelfMaintenance.authenticate_admin(password)`` -> bool
- ``SelfMaintenance.add_tool_pack(json_text)``      -> str
- ``SelfMaintenance.repair()``                       -> str
- ``SelfMaintenance.merge_packs()``                  -> str
- ``SelfMaintenance.update_from_repo(repo_url, dest)`` -> str
- ``SelfMaintenance.health()``                       -> dict
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .paths import (
    AGENT_OS_ROOT,
    BACKUPS_DIR,
    CONFIG_DIR,
    CUSTOM_TOOLS_DIR,
    MEMORY_DIR,
    TOOLS_PACKS_DIR,
    WORKSPACE_DIR,
)


CONFIG_FILE = CONFIG_DIR / "admin.json"


def _ensure_dirs() -> None:
    for d in (BACKUPS_DIR, CONFIG_DIR, CUSTOM_TOOLS_DIR, WORKSPACE_DIR, MEMORY_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


_ensure_dirs()


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


class SelfMaintenance:
    """Class-level coordinator for the self-maintenance sidecar."""

    _failed_attempts: int = 0
    _max_failed_attempts: int = 5
    _lockout_time: int = 60
    _lock = threading.Lock()
    _admin_authenticated: bool = False
    _log_func: Optional[Callable[[str, str], None]] = None

    # ------------------------------------------------------------------
    # Logging plumbing
    # ------------------------------------------------------------------
    @classmethod
    def set_log_func(cls, fn: Callable[[str, str], None]) -> None:
        cls._log_func = fn

    @classmethod
    def _log(cls, message: str, level: str = "INFO") -> None:
        if cls._log_func:
            try:
                cls._log_func(message, level)
                return
            except Exception:
                pass
        print(f"[{level}] {message}")

    # ------------------------------------------------------------------
    # Admin password handling
    # ------------------------------------------------------------------
    @classmethod
    def _read_admin_config(cls) -> Dict[str, Any]:
        if not CONFIG_FILE.exists():
            return {}
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @classmethod
    def _write_admin_config(cls, cfg: Dict[str, Any]) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)

    @classmethod
    def _ensure_admin_password_hash(cls) -> str:
        cfg = cls._read_admin_config()
        existing = cfg.get("admin_password_hash")
        if existing:
            return existing

        # First-run bootstrap: generate a one-time admin password.
        default_password = secrets.token_urlsafe(8)
        default_hash = _hash_password(default_password)
        cls._log(
            f"⚠ تم إنشاء كلمة مرور إدارية افتراضية: {default_password} (احفظها فوراً)",
            "SECURITY",
        )
        cfg["admin_password_hash"] = default_hash
        cls._write_admin_config(cfg)
        return default_hash

    @classmethod
    def authenticate_admin(cls, password: str) -> bool:
        with cls._lock:
            if cls._failed_attempts >= cls._max_failed_attempts:
                cls._log("محاولات كثيرة جدًا. انتظر 60 ثانية.", "SECURITY")
                time.sleep(cls._lockout_time)
                cls._failed_attempts = 0

            expected = cls._ensure_admin_password_hash()
            actual = _hash_password(password)
            if secrets.compare_digest(expected, actual):
                cls._admin_authenticated = True
                cls._failed_attempts = 0
                return True

            cls._failed_attempts += 1
            cls._log(
                f"فشل مصادقة الأدمن (محاولة {cls._failed_attempts}/{cls._max_failed_attempts}).",
                "SECURITY",
            )
            return False

    # ------------------------------------------------------------------
    # Tool pack maintenance
    # ------------------------------------------------------------------
    @classmethod
    def add_tool_pack(cls, json_text: str, filename: str = "custom_pack.json") -> str:
        try:
            data = json.loads(json_text)
        except Exception as e:
            return f"❌ JSON غير صالح: {e}"

        if not isinstance(data, list):
            return "❌ يجب أن يكون ملف الأدوات قائمة JSON."

        TOOLS_PACKS_DIR.mkdir(parents=True, exist_ok=True)
        target = TOOLS_PACKS_DIR / filename
        if target.exists():
            backup = BACKUPS_DIR / f"{filename}.{int(time.time())}.bak"
            target.replace(backup)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return f"✅ تم حفظ {len(data)} أداة في {target}"

    @classmethod
    def merge_packs(cls, output_name: str = "merged.json") -> str:
        merged: List[Dict[str, Any]] = []
        for pack in sorted(TOOLS_PACKS_DIR.glob("*.json")):
            try:
                with open(pack, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    merged.extend(data)
            except Exception as e:
                cls._log(f"تخطي {pack.name}: {e}", "WARN")

        out = TOOLS_PACKS_DIR / output_name
        with open(out, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
        return f"✅ تم دمج {len(merged)} أداة في {out}"

    @classmethod
    def repair(cls) -> str:
        """Quickly verify every pack file parses as JSON."""
        broken: List[str] = []
        for pack in TOOLS_PACKS_DIR.glob("*.json"):
            try:
                with open(pack, "r", encoding="utf-8") as f:
                    json.load(f)
            except Exception as e:
                broken.append(f"{pack.name}: {e}")

        if not broken:
            return "✅ كل الحزم صالحة JSON."
        return "❌ ملفات معطلة:\n" + "\n".join(broken)

    @staticmethod
    def _repo_host_allowed(repo_url: str) -> bool:
        """Allow only hosts listed in ``AGENT_REPO_ALLOWLIST`` (comma-separated).

        When the env var is missing, we default to ``github.com,gitlab.com``.
        """
        import os as _os
        from urllib.parse import urlparse

        allow = _os.environ.get(
            "AGENT_REPO_ALLOWLIST", "github.com,gitlab.com,bitbucket.org",
        )
        allowed = {h.strip().lower() for h in allow.split(",") if h.strip()}
        host = (urlparse(repo_url).hostname or "").lower()
        # Bare ``git@host:owner/repo`` style clones; treat the part before ":".
        if not host and "@" in repo_url and ":" in repo_url:
            host = repo_url.split("@", 1)[1].split(":", 1)[0].lower()
        return host in allowed

    @classmethod
    def update_from_repo(cls, repo_url: str, dest_subdir: str = "external") -> str:
        """Clone or pull a Git repo into ``tools/custom/<dest_subdir>``."""
        if not cls._repo_host_allowed(repo_url):
            return (
                f"❌ مضيف المستودع غير مسموح: {repo_url}. "
                "أضفه إلى AGENT_REPO_ALLOWLIST."
            )
        target = CUSTOM_TOOLS_DIR / dest_subdir
        if target.exists():
            cmd = ["git", "-C", str(target), "pull", "--ff-only"]
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            cmd = ["git", "clone", repo_url, str(target)]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if proc.returncode != 0:
                return f"❌ فشل git: {proc.stderr.strip()}"
            return f"✅ {target} → {repo_url}\n{proc.stdout.strip()}"
        except Exception as e:
            return f"❌ خطأ في تنفيذ git: {e}"

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    @classmethod
    def health(cls) -> Dict[str, Any]:
        packs = list(TOOLS_PACKS_DIR.glob("*.json")) if TOOLS_PACKS_DIR.exists() else []
        return {
            "tools_packs": len(packs),
            "config_present": CONFIG_FILE.exists(),
            "bootstrap_required": not CONFIG_FILE.exists(),
            "checked_at": datetime.utcnow().isoformat() + "Z",
        }


__all__ = ["SelfMaintenance"]
