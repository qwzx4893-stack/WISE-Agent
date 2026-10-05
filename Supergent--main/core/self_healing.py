"""Intelligent self-healing layer.

The self-healing layer listens to several problem sources (user
reports, onboarding failures, repeated runtime failures, health
checks, Tracer error spikes) and tries multi-strategy repairs. It is
designed to never crash the host process — every strategy returns a
:class:`StrategyResult` and the manager decides whether to escalate.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .paths import CONFIG_DIR

LOG = logging.getLogger("agent_os.self_healing")
HISTORY_PATH: Path = CONFIG_DIR / "repair_history.json"
HISTORY_LIMIT = 100


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
CAT_MISSING_DEP = "missing_dep"
CAT_CORRUPTED_FILE = "corrupted_file"
CAT_NETWORK = "network"
CAT_SANDBOX = "sandbox"
CAT_CONFIG = "config"
CAT_UNKNOWN = "unknown"

#: Regex patterns the diagnose engine matches against the error text.
_PATTERNS: List[Tuple[str, re.Pattern]] = [
    (CAT_MISSING_DEP, re.compile(
        r"(?:command not found|No module named|ImportError|ModuleNotFoundError|"
        r"executable .* not found|cannot find -l|"
        r"is not installed|is not recognized as an internal)",
        re.IGNORECASE)),
    (CAT_NETWORK, re.compile(
        r"(?:Connection (?:refused|reset|aborted)|Temporary failure in name "
        r"resolution|getaddrinfo failed|ETIMEDOUT|EHOSTUNREACH|"
        r"timed? ?out|certificate verify failed|HTTP ?5\d\d|"
        r"requests\.exceptions|urllib\.error|max retries exceeded)",
        re.IGNORECASE)),
    (CAT_SANDBOX, re.compile(
        r"(?:proot[: ]|rootfs[: /]|/sandbox/rootfs|"
        r"PROOT_NO_SECCOMP|setrlimit|fakeroot)",
        re.IGNORECASE)),
    (CAT_CORRUPTED_FILE, re.compile(
        r"(?:JSONDecodeError|yaml\.YAMLError|SyntaxError|UnicodeDecodeError|"
        r"unable to parse|invalid format|truncated)",
        re.IGNORECASE)),
    (CAT_CONFIG, re.compile(
        r"(?:invalid mode|conflicting setting|bad configuration|"
        r"AGENT_OS_MODE|resources\.json|platform_mode\.json)",
        re.IGNORECASE)),
]


# ---------------------------------------------------------------------------
# Result & report types
# ---------------------------------------------------------------------------
@dataclass
class Diagnosis:
    target: str
    category: str = CAT_UNKNOWN
    error_text: str = ""
    confidence: float = 0.0
    hints: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StrategyResult:
    name: str
    success: bool
    detail: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RepairReport:
    repair_id: str
    target: str
    diagnosis: Diagnosis
    strategies_tried: List[StrategyResult] = field(default_factory=list)
    success: bool = False
    manual_steps: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repair_id": self.repair_id,
            "target": self.target,
            "diagnosis": self.diagnosis.to_dict(),
            "strategies_tried": [s.to_dict() for s in self.strategies_tried],
            "success": self.success,
            "manual_steps": list(self.manual_steps),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": (((self.finished_at or time.time())
                              - self.started_at) * 1000),
        }


# ---------------------------------------------------------------------------
# Tracer helper (best-effort, never raises)
# ---------------------------------------------------------------------------
def _emit(event: str, **kwargs: Any) -> None:
    try:
        from . import observability  # type: ignore
        Tracer = getattr(observability, "Tracer", None)
        if Tracer is not None:
            Tracer.emit(event, **kwargs)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
class SelfHealing:
    """Singleton coordinator. Holds in-flight repair tasks and history."""

    _instance: Optional["SelfHealing"] = None
    _lock = threading.RLock()

    def __new__(cls, *args: Any, **kwargs: Any) -> "SelfHealing":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialised = False  # type: ignore[attr-defined]
            return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialised", False):
            return
        self._tasks: Dict[str, RepairReport] = {}
        self._history: List[Dict[str, Any]] = []
        self._task_lock = threading.RLock()
        self._initialised = True
        self._load_history()

    # -------------------------------------------------------------- diagnose
    def diagnose(self, *, target: str = "all",
                 error_text: str = "") -> Diagnosis:
        """Classify a problem from an error string + target hint."""
        text = error_text or ""
        diag = Diagnosis(target=target, error_text=text[:1000])
        for category, pattern in _PATTERNS:
            if pattern.search(text):
                diag.category = category
                diag.confidence = 0.85
                break
        # Target-derived hints.
        if target.startswith("tool:") and diag.category == CAT_UNKNOWN:
            diag.category = CAT_MISSING_DEP
            diag.confidence = 0.5
            diag.hints.append("derived from target prefix 'tool:'")
        if target == "sandbox" and diag.category == CAT_UNKNOWN:
            diag.category = CAT_SANDBOX
            diag.confidence = 0.6
        if target == "network" and diag.category == CAT_UNKNOWN:
            diag.category = CAT_NETWORK
            diag.confidence = 0.6
        if target == "config" and diag.category == CAT_UNKNOWN:
            diag.category = CAT_CONFIG
            diag.confidence = 0.6
        _emit("self_healing.diagnose", target=target,
              category=diag.category, confidence=diag.confidence)
        return diag

    # -------------------------------------------------------------- repair
    def repair(self, *, target: str = "all",
               error_text: str = "") -> RepairReport:
        diag = self.diagnose(target=target, error_text=error_text)
        report = RepairReport(repair_id=uuid.uuid4().hex,
                               target=target, diagnosis=diag)
        with self._task_lock:
            self._tasks[report.repair_id] = report
        _emit("self_healing.repair.start",
              repair_id=report.repair_id, target=target, category=diag.category)

        strategies = self._strategies_for(diag.category, target)
        for strat_name, strat_fn in strategies:
            started = time.time()
            try:
                ok, detail = strat_fn(target=target, diagnosis=diag)
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            report.strategies_tried.append(StrategyResult(
                name=strat_name, success=bool(ok), detail=detail,
                duration_ms=(time.time() - started) * 1000,
            ))
            if ok:
                report.success = True
                break

        # If nothing worked, attach manual steps.
        if not report.success:
            report.manual_steps = self._manual_steps(diag, target)

        report.finished_at = time.time()
        _emit("self_healing.repair.end",
              repair_id=report.repair_id, success=report.success,
              category=diag.category)
        self._record_history(report)
        return report

    def get(self, repair_id: str) -> Optional[RepairReport]:
        return self._tasks.get(repair_id)

    def history(self) -> List[Dict[str, Any]]:
        return list(self._history)

    # --------------------------------------------------- strategy table
    def _strategies_for(self, category: str, target: str
                         ) -> List[Tuple[str, Callable[..., Tuple[bool, str]]]]:
        if category == CAT_MISSING_DEP:
            return [
                ("retry_install", self._retry_install),
                ("alternative_source", self._alt_source_install),
                ("suggest_alternative_tool", self._suggest_alt_tool),
            ]
        if category == CAT_CORRUPTED_FILE:
            return [
                ("restore_backup", self._restore_backup),
                ("regenerate_defaults", self._regen_defaults),
            ]
        if category == CAT_NETWORK:
            return [
                ("retry_with_backoff", self._retry_backoff),
                ("offline_fallback", self._offline_fallback),
            ]
        if category == CAT_SANDBOX:
            return [
                ("rebuild_sandbox", self._rebuild_sandbox),
                ("relax_resource_limits", self._relax_limits),
            ]
        if category == CAT_CONFIG:
            return [
                ("reset_to_defaults", self._reset_config),
            ]
        return [("noop", lambda **_: (False, "no strategy for category"))]

    # ------------------------------------------------------- strategies
    def _retry_install(self, *, target: str,
                        diagnosis: Diagnosis) -> Tuple[bool, str]:
        if not target.startswith("tool:"):
            return False, "target is not a tool"
        name = target.split(":", 1)[1]
        try:
            from .tool_installer import get_installer
            res = get_installer().install_missing(names=[name])
            if any(r.get("tool") == name for r in res.get("installed", [])):
                return True, f"installed {name}"
            return False, f"installer reported failure: {res}"
        except Exception as exc:
            return False, f"installer error: {exc}"

    def _alt_source_install(self, *, target: str,
                              diagnosis: Diagnosis) -> Tuple[bool, str]:
        if not target.startswith("tool:"):
            return False, "no alternative source for non-tool target"
        # The alternative-source path is a thin retry that we expect to
        # be exercised manually if a mirror is configured.
        import os
        if not os.environ.get("PIP_INDEX_URL") and not os.environ.get(
                "AGENT_OS_APT_MIRROR"):
            return False, "no mirror configured (PIP_INDEX_URL / "\
                          "AGENT_OS_APT_MIRROR not set)"
        return self._retry_install(target=target, diagnosis=diagnosis)

    def _suggest_alt_tool(self, *, target: str,
                            diagnosis: Diagnosis) -> Tuple[bool, str]:
        if not target.startswith("tool:"):
            return False, "no suggestion for non-tool target"
        name = target.split(":", 1)[1]
        try:
            from .tool_intelligence.ranker import rank_tools_for_task
            from .tool_installer import get_installer
            scanned = get_installer().scan()
            tools_meta: Dict[str, Dict[str, Any]] = {}
            for n, status in scanned.items():
                if n == name:
                    continue
                if not getattr(status, "found", False):
                    continue
                tools_meta[n] = {"description": getattr(status, "description",
                                                         ""),
                                 "category": getattr(status, "category", "")}
            ranked = rank_tools_for_task(name, tools_meta)
            ranked = [r for r in ranked if r.name != name]
            if ranked:
                top = ranked[0]
                diagnosis.hints.append(
                    f"alternative_tool={top.name} (score={top.score:.2f})")
                return True, f"suggested alternative: {top.name}"
            return False, "no alternative found"
        except Exception as exc:
            return False, f"ranker error: {exc}"

    def _restore_backup(self, *, target: str,
                          diagnosis: Diagnosis) -> Tuple[bool, str]:
        try:
            from .recovery import rollback_file  # type: ignore
            ok = rollback_file(target)
            return bool(ok), "rolled back" if ok else "no backup found"
        except Exception as exc:
            return False, f"recovery module unavailable: {exc}"

    def _regen_defaults(self, *, target: str,
                         diagnosis: Diagnosis) -> Tuple[bool, str]:
        if "resources.json" in target or target == "config":
            try:
                from .platform_manager import get_platform_manager
                from .resource_settings import get_store
                get_store().reset(mode=get_platform_manager().mode)
                return True, "resources.json regenerated"
            except Exception as exc:
                return False, f"reset failed: {exc}"
        return False, f"no default available for {target!r}"

    def _retry_backoff(self, *, target: str,
                         diagnosis: Diagnosis) -> Tuple[bool, str]:
        # The repair manager itself can't replay the failing call, but
        # marking the network as warmed up is useful: we make a tiny
        # well-known check that the upstream is reachable.
        import socket
        for delay in (0, 1, 2, 4):
            if delay:
                time.sleep(delay)
            try:
                socket.create_connection(("api.github.com", 443), timeout=5).close()
                return True, f"network reachable after {delay}s backoff"
            except OSError as exc:
                last = str(exc)
        return False, f"still unreachable: {last}"  # type: ignore[name-defined]

    def _offline_fallback(self, *, target: str,
                            diagnosis: Diagnosis) -> Tuple[bool, str]:
        diagnosis.hints.append("operate in offline mode; cached data only")
        return True, "offline mode advisory recorded"

    def _rebuild_sandbox(self, *, target: str,
                          diagnosis: Diagnosis) -> Tuple[bool, str]:
        try:
            from .onboarding import get_onboarding_manager
            res = get_onboarding_manager().install_single("rootfs")
            return res.get("status") == "installed", res.get("detail", "")
        except Exception as exc:
            return False, f"onboarding manager error: {exc}"

    def _relax_limits(self, *, target: str,
                       diagnosis: Diagnosis) -> Tuple[bool, str]:
        try:
            from .platform_manager import get_platform_manager
            from .resource_settings import (
                LITE_DEFAULTS, PRO_DEFAULTS, get_store,
            )
            mode = get_platform_manager().mode
            store = get_store()
            current = store.load(mode=mode).to_dict()
            relaxed = PRO_DEFAULTS if mode == "pro" else LITE_DEFAULTS
            for k in ("cpu_seconds", "memory_mb", "max_processes"):
                current[k] = max(int(current.get(k, 0) or 0),
                                  int(relaxed[k] or 0))
            current["mode_at_save"] = mode
            from .resource_settings import _coerce
            store.save(_coerce(current))
            return True, "limits relaxed to mode defaults"
        except Exception as exc:
            return False, f"relax failed: {exc}"

    def _reset_config(self, *, target: str,
                       diagnosis: Diagnosis) -> Tuple[bool, str]:
        return self._regen_defaults(target="config", diagnosis=diagnosis)

    # ------------------------------------------------------------ misc
    def _manual_steps(self, diag: Diagnosis, target: str) -> List[str]:
        if diag.category == CAT_MISSING_DEP:
            name = (target.split(":", 1)[1] if target.startswith("tool:")
                    else "<tool>")
            return [
                f"Try installing {name} manually (apt/pip/npm)",
                "Set PIP_INDEX_URL or AGENT_OS_APT_MIRROR for an alternative source",
            ]
        if diag.category == CAT_NETWORK:
            return [
                "Verify the host has working DNS and outbound HTTPS",
                "Check whether a corporate proxy needs HTTPS_PROXY env var",
            ]
        if diag.category == CAT_SANDBOX:
            return [
                "Re-run sudo bash sandbox/build_rootfs.sh manually",
                "Check that /proc, /dev and /sys are accessible inside the sandbox",
            ]
        if diag.category == CAT_CORRUPTED_FILE:
            return [
                "Restore the file from a known-good backup",
                "Delete the corrupted file so defaults are regenerated",
            ]
        if diag.category == CAT_CONFIG:
            return [
                "POST /admin/resources {reset:true} to reset resources.json",
                "Unset AGENT_OS_MODE and re-run /admin/platform to redetect",
            ]
        return ["No automatic remediation available — escalate to a human."]

    # ------------------------------------------------------ history I/O
    def _load_history(self) -> None:
        try:
            if HISTORY_PATH.exists():
                self._history = json.loads(HISTORY_PATH.read_text(
                    encoding="utf-8"))
        except Exception:
            self._history = []

    def _record_history(self, report: RepairReport) -> None:
        self._history.append(report.to_dict())
        if len(self._history) > HISTORY_LIMIT:
            self._history = self._history[-HISTORY_LIMIT:]
        try:
            HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            HISTORY_PATH.write_text(
                json.dumps(self._history, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            LOG.warning("history persist failed: %s", exc)


def get_self_healing() -> SelfHealing:
    return SelfHealing()


__all__ = [
    "Diagnosis", "StrategyResult", "RepairReport", "SelfHealing",
    "get_self_healing",
    "CAT_MISSING_DEP", "CAT_CORRUPTED_FILE", "CAT_NETWORK",
    "CAT_SANDBOX", "CAT_CONFIG", "CAT_UNKNOWN",
]
