"""
Agent lifecycle manager for skills.

Tracks per-skill state (active|inactive|deprecated|failed|updating),
success/failure counters and health-check results in
``skills/.lifecycle.json``. Also exposes :class:`LifecycleManager` so the
HTTP layer can wire the same operations into ``/admin/skills/...``
endpoints.

Design choices:

- Pure Python, file-backed JSON — no extra deps.
- All state changes go through ``transition()`` so we can audit-log every
  switch into a Tracer event.
- ``health_check`` reads SKILL.md / metadata.json from disk; if a skill is
  missing or unreadable it transitions to ``failed`` automatically.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ..observability import Tracer
from ..paths import SKILLS_DIR

LIFECYCLE_FILE = SKILLS_DIR / ".lifecycle.json"

VALID_STATES: Set[str] = {
    "active", "inactive", "deprecated", "failed", "updating",
}


@dataclass
class SkillRecord:
    state: str = "active"
    success_count: int = 0
    failure_count: int = 0
    last_state_change: float = 0.0
    last_check: float = 0.0
    last_check_ok: bool = True
    last_error: Optional[str] = None
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_state_change": self.last_state_change,
            "last_check": self.last_check,
            "last_check_ok": self.last_check_ok,
            "last_error": self.last_error,
            "history": self.history[-50:],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillRecord":
        return cls(
            state=data.get("state", "active"),
            success_count=int(data.get("success_count", 0)),
            failure_count=int(data.get("failure_count", 0)),
            last_state_change=float(data.get("last_state_change", 0.0)),
            last_check=float(data.get("last_check", 0.0)),
            last_check_ok=bool(data.get("last_check_ok", True)),
            last_error=data.get("last_error"),
            history=list(data.get("history", []) or []),
        )


class LifecycleManager:
    """File-backed manager of skill lifecycle state."""

    def __init__(self, lifecycle_file: Optional[Path] = None) -> None:
        self.path = Path(lifecycle_file) if lifecycle_file else LIFECYCLE_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._records: Dict[str, SkillRecord] = {}
        self._load()

    # ------------------------------------------------------------------ I/O
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        self._records = {
            name: SkillRecord.from_dict(rec or {})
            for name, rec in (data or {}).items()
            if isinstance(rec, dict)
        }

    def _save(self) -> None:
        snapshot = {name: rec.to_dict() for name, rec in self._records.items()}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------ API
    def get(self, name: str) -> SkillRecord:
        with self._lock:
            rec = self._records.get(name)
            if rec is None:
                rec = SkillRecord(last_state_change=time.time())
                self._records[name] = rec
                self._save()
            return rec

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {n: r.to_dict() for n, r in self._records.items()}

    def transition(self, name: str, new_state: str,
                   *, reason: Optional[str] = None,
                   actor: Optional[str] = None) -> SkillRecord:
        if new_state not in VALID_STATES:
            raise ValueError(f"invalid state: {new_state!r}")
        with self._lock:
            rec = self.get(name)
            old = rec.state
            rec.state = new_state
            rec.last_state_change = time.time()
            rec.history.append({
                "ts": rec.last_state_change,
                "from": old,
                "to": new_state,
                "reason": reason,
                "actor": actor,
            })
            Tracer.emit(
                "skill.lifecycle.transition",
                skill=name, old=old, new=new_state,
                reason=reason or "", actor=actor or "",
            )
            self._save()
            return rec

    def record_success(self, name: str) -> SkillRecord:
        with self._lock:
            rec = self.get(name)
            rec.success_count += 1
            self._save()
            return rec

    def record_failure(self, name: str, error: Optional[str] = None,
                       *, auto_install_deps: bool = True) -> SkillRecord:
        with self._lock:
            rec = self.get(name)
            rec.failure_count += 1
            rec.last_error = error

            # Best-effort auto-install when the failure looks dependency related.
            if auto_install_deps and error and _looks_like_missing_dep(error):
                installed = _try_auto_install_for_skill(name)
                if installed:
                    Tracer.emit("skill.lifecycle.auto_install",
                                skill=name, installed=installed,
                                error=error)

            # Auto-flag a skill that fails 3+ times in a row as failed.
            if rec.failure_count >= 3 and rec.state == "active":
                self.transition(name, "failed",
                                reason=f"3+ failures: {error or 'unknown'}",
                                actor="lifecycle.auto")
            else:
                self._save()
            return rec

    # ---------------------------------------------------------------- health
    def health_check(self, name: str, *, skill_dir: Optional[Path] = None) -> Dict[str, Any]:
        """Verify the skill's files are present and parseable.

        Returns a dict with ``ok``/``error`` + the resulting state. If the
        skill cannot be loaded we transition it to ``failed`` automatically.
        """
        with self._lock:
            rec = self.get(name)
            ok = True
            error: Optional[str] = None

            try:
                folder = self._resolve_dir(name, skill_dir)
            except FileNotFoundError as exc:
                ok = False
                error = str(exc)
                folder = None

            if folder is not None:
                if not (folder / "SKILL.md").exists():
                    ok = False
                    error = f"missing SKILL.md in {folder}"
                else:
                    meta = folder / "metadata.json"
                    if meta.exists():
                        try:
                            json.loads(meta.read_text(encoding="utf-8"))
                        except Exception as exc:
                            ok = False
                            error = f"metadata.json invalid: {exc}"

            rec.last_check = time.time()
            rec.last_check_ok = ok
            if not ok and rec.state in {"active", "updating"}:
                self.transition(name, "failed",
                                reason=error or "health-check failed",
                                actor="lifecycle.health")
            else:
                self._save()

            return {"name": name, "ok": ok, "error": error, "state": rec.state}

    # ------------------------------------------------------------- shortcuts
    def activate(self, name: str, **kw: Any) -> SkillRecord:
        return self.transition(name, "active", **kw)

    def deactivate(self, name: str, **kw: Any) -> SkillRecord:
        return self.transition(name, "inactive", **kw)

    def deprecate(self, name: str, **kw: Any) -> SkillRecord:
        return self.transition(name, "deprecated", **kw)

    def mark_updating(self, name: str, **kw: Any) -> SkillRecord:
        return self.transition(name, "updating", **kw)

    def repair(self, name: str, *, skill_dir: Optional[Path] = None,
               actor: Optional[str] = "admin") -> Dict[str, Any]:
        check = self.health_check(name, skill_dir=skill_dir)
        if check["ok"]:
            self.transition(name, "active", reason="repaired", actor=actor)
            check["state"] = "active"
        return check

    # ------------------------------------------------------------ internals
    @staticmethod
    def _resolve_dir(name: str, override: Optional[Path]) -> Path:
        if override:
            p = Path(override)
            if not p.exists():
                raise FileNotFoundError(f"skill dir not found: {p}")
            return p
        # Lazy import keeps the module independent of the indexer for tests.
        from .indexer import SkillIndexer
        info = SkillIndexer().get_skill_info(name)
        if not info:
            raise FileNotFoundError(f"skill not indexed: {name}")
        return Path(info["path"])


_DEFAULT: Optional[LifecycleManager] = None


def get_manager() -> LifecycleManager:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = LifecycleManager()
    return _DEFAULT


# ---------------------------------------------------------------------------
# Self-healing helpers
# ---------------------------------------------------------------------------
_DEP_HINTS = (
    "command not found", "no such file", "modulenotfounderror",
    "importerror", "not installed", "missing dependency",
    "package not found", "executable not found",
)


def _looks_like_missing_dep(error: str) -> bool:
    """Heuristic: does this error message look like a missing-dependency case?"""
    if not error:
        return False
    low = str(error).lower()
    return any(hint in low for hint in _DEP_HINTS)


def _try_auto_install_for_skill(name: str) -> List[str]:
    """Best-effort installation of the skill's declared dependencies.

    Returns the list of dependency names that were attempted (regardless of
    success). The actual install runs in the rootfs sandbox via
    :class:`ToolInstaller` when available; if not, this is a no-op.
    """
    try:
        from .indexer import SkillIndexer
        info = SkillIndexer().get_skill_info(name)
    except Exception:
        info = None
    deps: List[str] = []
    if isinstance(info, dict):
        meta = info.get("metadata") or {}
        for d in (meta.get("dependencies") or []):
            if isinstance(d, str):
                deps.append(d)
            elif isinstance(d, dict) and d.get("name"):
                deps.append(str(d["name"]))
    if not deps:
        return []

    try:
        from ..tool_installer import ToolInstaller
    except Exception:
        return []
    installer = ToolInstaller()
    try:
        result = installer.install_missing(names=deps)
    except Exception as exc:
        Tracer.emit("skill.auto_install.failed",
                    skill=name, deps=deps, error=str(exc))
        return []
    installed = [item.get("tool", "") for item in (result.get("installed") or [])]
    return installed or list(deps)


def first_boot_health_check(*, force: bool = False) -> Dict[str, Any]:
    """Run a lightweight health check across the entire indexed skill set.

    Called automatically by :class:`SkillIndexer` when ``.lifecycle.json``
    is missing or empty (see ``indexer.py``). Re-running is safe — already
    healthy skills stay active. Pass ``force=True`` to ignore the
    "already initialised" short-circuit.
    """
    manager = get_manager()
    if not force and manager.path.exists() and manager._records:
        return {"ok": True, "skipped": True,
                "reason": "lifecycle.json already populated"}

    try:
        from .indexer import SkillIndexer
        index = SkillIndexer().get_index()
    except Exception as exc:
        return {"ok": False, "error": f"indexer unavailable: {exc}"}

    checked = 0
    failed: List[str] = []
    for name, info in index.items():
        path = Path(info.get("path") or "")
        result = manager.health_check(name, skill_dir=path if path.exists() else None)
        checked += 1
        if not result.get("ok"):
            failed.append(name)
    Tracer.emit("skill.lifecycle.first_boot_health_check",
                checked=checked, failed=len(failed))
    return {"ok": True, "checked": checked,
            "failed_count": len(failed),
            "failed": failed[:50]}
