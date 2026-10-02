"""Phase 9 Part 2.4 — Startup self-test.

Lightweight, fully-offline diagnostic that walks every Agent OS
subsystem and reports whether it is **functional**, **degraded**, or
**unavailable**. Intended to run:

* On first boot after install (so the onboarding screen can be
  dismissed with confidence).
* On demand via ``GET /admin/self-test`` (admin endpoint).
* In CI as a smoke test.

The contract is intentionally narrow: every check returns a
``CheckResult`` and the overall report aggregates them. Checks must
NEVER raise — failures are captured into the result, not propagated —
because a self-test that crashes is worse than one that says "I don't
know".

Each check is also designed to be safe in a "lite" environment: it
must not download anything, must not require credentials, and must
not exceed ~1 second of CPU/IO. We probe *availability* of optional
dependencies (apprise, llmlingua, …), not their runtime behaviour.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

OK = "ok"
DEGRADED = "degraded"
FAILED = "failed"


@dataclass
class CheckResult:
    """Outcome of a single self-test step."""

    name: str
    status: str               # one of OK / DEGRADED / FAILED
    message: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    actionable: Optional[str] = None  # human-readable next step on failure

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SelfTestReport:
    """Aggregated outcome of every check run by ``run_self_test``."""

    status: str               # OK / DEGRADED / FAILED
    checks: List[CheckResult]
    started_at: float
    duration_ms: int
    summary: Dict[str, int]   # {"ok": .., "degraded": .., "failed": ..}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "summary": self.summary,
            "checks": [c.to_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Individual checks. Each has the signature ``() -> CheckResult``.
# ---------------------------------------------------------------------------


def _timed(fn: Callable[[], CheckResult]) -> CheckResult:
    """Run ``fn`` and stamp ``duration_ms``; convert exceptions to FAILED."""
    t0 = time.time()
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001
        result = CheckResult(
            name=getattr(fn, "__name__", "unknown"),
            status=FAILED,
            message=f"check raised {type(exc).__name__}: {exc}",
            actionable="Inspect the traceback in agent logs and "
                       "re-run /admin/self-test once the cause is fixed.",
        )
    result.duration_ms = max(1, int((time.time() - t0) * 1000))
    return result


def check_paths() -> CheckResult:
    """Verify the AGENT_OS_ROOT directory tree exists and is writable."""
    from .paths import (AGENT_OS_ROOT, CONFIG_DIR, LOGS_DIR, MEMORY_DIR,
                        SESSIONS_DIR, WORKSPACE_DIR)

    missing: List[str] = []
    not_writable: List[str] = []
    for p in (AGENT_OS_ROOT, CONFIG_DIR, LOGS_DIR, MEMORY_DIR,
              SESSIONS_DIR, WORKSPACE_DIR):
        if not p.exists():
            missing.append(str(p))
            continue
        # Touch a probe file to verify writability.
        probe = p / ".self_test_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception:
            not_writable.append(str(p))

    if missing:
        return CheckResult(
            name="paths", status=DEGRADED,
            message=f"{len(missing)} runtime dir(s) missing",
            detail={"missing": missing},
            actionable="Run core.paths.ensure_runtime_dirs() or restart "
                       "the server (lifespan recreates them).",
        )
    if not_writable:
        return CheckResult(
            name="paths", status=FAILED,
            message=f"{len(not_writable)} runtime dir(s) not writable",
            detail={"not_writable": not_writable},
            actionable="Check filesystem permissions and disk space.",
        )
    return CheckResult(
        name="paths", status=OK,
        message="all runtime directories exist and are writable",
        detail={"agent_os_root": str(AGENT_OS_ROOT)},
    )


def check_sandbox() -> CheckResult:
    """Verify the proot/Debian rootfs is present and contains /bin/sh."""
    from .paths import SANDBOX_ROOT

    if not SANDBOX_ROOT.exists():
        return CheckResult(
            name="sandbox", status=DEGRADED,
            message="rootfs not built yet",
            detail={"path": str(SANDBOX_ROOT)},
            actionable="bash sandbox/build_rootfs.sh — or use "
                       "POST /admin/onboarding/start.",
        )
    sh = SANDBOX_ROOT / "bin" / "sh"
    if not sh.exists():
        return CheckResult(
            name="sandbox", status=DEGRADED,
            message="rootfs incomplete (missing /bin/sh)",
            detail={"path": str(SANDBOX_ROOT)},
            actionable="Re-run sandbox/build_rootfs.sh — the previous "
                       "build was interrupted.",
        )
    if not shutil.which("proot"):
        return CheckResult(
            name="sandbox", status=DEGRADED,
            message="proot binary not on PATH",
            actionable="Install proot or add its binary to PATH.",
        )
    return CheckResult(
        name="sandbox", status=OK,
        message="rootfs ready and proot on PATH",
        detail={"path": str(SANDBOX_ROOT)},
    )


def check_llm_providers() -> CheckResult:
    """At least one LLM provider must be reachable / have a key."""
    try:
        from .llm.keystore import KeyStore
        ks = KeyStore()
        keys = ks.list()
        enabled = [k for k in keys if k.get("enabled", True)
                   and (k.get("api_key") or "").strip()]
        providers = sorted({k.get("provider") or k.get("name") or ""
                            for k in enabled if (k.get("provider")
                                                  or k.get("name"))})
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="llm_providers", status=FAILED,
            message=f"keystore error: {exc}",
            actionable="Check core/llm/keystore.py and the secrets "
                       "store at config/secrets.bin.",
        )
    if not enabled:
        return CheckResult(
            name="llm_providers", status=DEGRADED,
            message="no LLM API key configured",
            actionable="Open Settings → Provider keys, or call "
                       "POST /admin/keys with a provider+key payload.",
        )
    return CheckResult(
        name="llm_providers", status=OK,
        message=f"{len(enabled)} key(s) configured "
                f"across {len(providers)} provider(s)",
        detail={"providers": providers, "key_count": len(enabled)},
    )


def check_tools() -> CheckResult:
    """Verify the tool registry has been populated."""
    try:
        from agent_core import tool_registry
        tools = tool_registry.list_tools()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="tools", status=FAILED,
            message=f"tool registry import failed: {exc}",
            actionable="Check agent_core.py imports and tool registration.",
        )
    if not tools:
        return CheckResult(
            name="tools", status=DEGRADED,
            message="no tools registered",
            actionable="Restart the server so lifespan re-runs "
                       "system_awareness.bootstrap().",
        )
    return CheckResult(
        name="tools", status=OK,
        message=f"{len(tools)} tools registered",
        detail={"count": len(tools)},
    )


def check_skills() -> CheckResult:
    """Verify SkillIndexer can enumerate skills on disk."""
    try:
        from .skills.indexer import SkillIndexer
        names = SkillIndexer().list_skills()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="skills", status=FAILED,
            message=f"SkillIndexer failed: {exc}",
            actionable="Check that skills/ exists and contains "
                       "SKILL.md files.",
        )
    if not names:
        return CheckResult(
            name="skills", status=DEGRADED,
            message="no skills indexed",
            actionable="Add SKILL.md files under skills/ or call "
                       "POST /admin/skills/import.",
        )
    return CheckResult(
        name="skills", status=OK,
        message=f"{len(names)} skills indexed",
        detail={"count": len(names)},
    )


def check_rag_sources() -> CheckResult:
    """Verify the RAG source registry loads without crashing.

    We don't make any network requests — we just confirm the registry
    can be constructed and lists at least one source.
    """
    try:
        from .rag.router import KnowledgeRouter
        router = KnowledgeRouter()
        sources = list(router.sources.keys())
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="rag_sources", status=DEGRADED,
            message=f"RAG layer unavailable: {exc}",
            actionable="Optional: install the rag dependencies "
                       "(see requirements-optimization.txt).",
        )
    if not sources:
        return CheckResult(
            name="rag_sources", status=DEGRADED,
            message="no RAG sources configured",
        )
    return CheckResult(
        name="rag_sources", status=OK,
        message=f"{len(sources)} RAG sources registered",
        detail={"count": len(sources), "names": sources},
    )


def check_channels() -> CheckResult:
    """Apprise-backed channel layer should at least import."""
    try:
        from .channels.unified import list_channels
        registry = list_channels()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="channels", status=DEGRADED,
            message=f"channels layer unavailable: {exc}",
            actionable="pip install apprise (or restart after "
                       "requirements.txt is reinstalled).",
        )
    configured = registry.get("configured", []) or []
    schemes = registry.get("available_schemes", []) or []
    apprise_available = bool(registry.get("apprise_available", False))
    if not apprise_available:
        return CheckResult(
            name="channels", status=DEGRADED,
            message="apprise not installed",
            actionable="pip install apprise (~5 MB, no native deps).",
        )
    # We don't require any channel to be *configured* — that's user
    # input — only that the library is loadable.
    return CheckResult(
        name="channels", status=OK,
        message=f"apprise available with {len(schemes)} schemes; "
                f"{len(configured)} channel(s) configured",
        detail={"configured_count": len(configured),
                "schemes_count": len(schemes),
                "apprise_available": apprise_available},
    )


def check_scheduler() -> CheckResult:
    """Cron scheduler must be import-safe; we don't start the loop."""
    try:
        from .scheduler import get_scheduler
        sched = get_scheduler()
        schedules = sched.list()
        running = bool(getattr(sched, "_thread", None)
                       and sched._thread.is_alive())
        return CheckResult(
            name="scheduler", status=OK,
            message=f"{len(schedules)} schedule(s) persisted",
            detail={"running": running, "count": len(schedules)},
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="scheduler", status=DEGRADED,
            message=f"scheduler unavailable: {exc}",
        )


def check_optional_compression() -> CheckResult:
    """LLMLingua / Un-LOCC are optional. Report which (if any) is installed."""
    available: List[str] = []
    for mod in ("llmlingua", "un_locc"):
        try:
            if importlib.util.find_spec(mod) is not None:
                available.append(mod)
        except Exception:
            pass
    if not available:
        return CheckResult(
            name="advanced_compression", status=DEGRADED,
            message="no advanced-compression backend installed",
            actionable="Optional: pip install -r "
                       "requirements-optimization.txt for ~70% prompt "
                       "compression on long contexts.",
        )
    return CheckResult(
        name="advanced_compression", status=OK,
        message=f"{len(available)} backend(s) available",
        detail={"backends": available},
    )


def check_workforce() -> CheckResult:
    """The Eigent-inspired workforce must import cleanly."""
    try:
        from .workforce import Workforce, RootPlanner  # noqa: F401
        return CheckResult(name="workforce", status=OK,
                           message="workforce module importable")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="workforce", status=FAILED,
            message=f"workforce import failed: {exc}",
            actionable="Inspect core/workforce.py — the planner depends "
                       "on the Tracer and the LLM router.",
        )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


_DEFAULT_CHECKS: List[Callable[[], CheckResult]] = [
    check_paths,
    check_sandbox,
    check_llm_providers,
    check_tools,
    check_skills,
    check_rag_sources,
    check_channels,
    check_scheduler,
    check_optional_compression,
    check_workforce,
]


def run_self_test(
    *,
    only: Optional[List[str]] = None,
    extra: Optional[List[Callable[[], CheckResult]]] = None,
) -> SelfTestReport:
    """Run every check (or the subset named in ``only``) and return a report.

    ``extra`` may include caller-supplied checks for in-house plugins;
    they share the same ``CheckResult`` contract.
    """
    started = time.time()
    selected: List[Callable[[], CheckResult]] = []
    for fn in _DEFAULT_CHECKS:
        if only and fn.__name__.removeprefix("check_") not in only:
            continue
        selected.append(fn)
    if extra:
        selected.extend(extra)

    results: List[CheckResult] = []
    for fn in selected:
        results.append(_timed(fn))

    summary = {"ok": 0, "degraded": 0, "failed": 0}
    for r in results:
        if r.status == OK:
            summary["ok"] += 1
        elif r.status == DEGRADED:
            summary["degraded"] += 1
        else:
            summary["failed"] += 1

    if summary["failed"]:
        overall = FAILED
    elif summary["degraded"]:
        overall = DEGRADED
    else:
        overall = OK

    return SelfTestReport(
        status=overall,
        checks=results,
        started_at=started,
        duration_ms=max(1, int((time.time() - started) * 1000)),
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Cached "first-run" helper for the onboarding screen.
# ---------------------------------------------------------------------------


def _report_path() -> Path:
    from .paths import LOGS_DIR
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / "self_test.json"


def cached_report() -> Optional[SelfTestReport]:
    """Return the most recent persisted report, if any."""
    p = _report_path()
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        checks = [CheckResult(**c) for c in raw.get("checks", [])]
        return SelfTestReport(
            status=raw["status"], checks=checks,
            started_at=raw["started_at"],
            duration_ms=raw["duration_ms"],
            summary=raw["summary"],
        )
    except Exception:
        return None


def persist_report(report: SelfTestReport) -> None:
    """Atomically save ``report`` so onboarding can read it on next boot."""
    p = _report_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, p)


__all__ = [
    "CheckResult", "SelfTestReport",
    "run_self_test", "cached_report", "persist_report",
    "check_paths", "check_sandbox", "check_llm_providers",
    "check_tools", "check_skills", "check_rag_sources",
    "check_channels", "check_scheduler",
    "check_optional_compression", "check_workforce",
    "OK", "DEGRADED", "FAILED",
]
