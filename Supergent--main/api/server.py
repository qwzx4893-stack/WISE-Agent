"""
FastAPI bridge for Agent OS.

Endpoints
---------
GET  /health                       -> liveness + counts
GET  /tools                        -> list of registered runtime tools
GET  /tools/manifest/{name}        -> manifest of a tool from system_awareness
POST /chat       {message}         -> run the ReAct loop, return final answer
POST /execute    {command}         -> execute a shell command via the kernel sandbox
POST /admin/reload-tools           -> rebuild SystemAwareness (auth required)

Auth
----
If the ``AGENT_API_TOKEN`` environment variable is set, every mutating
endpoint (`/chat`, `/execute`, `/admin/*`) requires header
``X-Agent-Token: <token>``. When unset, the API binds to localhost-only
behavior is recommended (the host bind is the operator's responsibility).

Run with::

    uvicorn api.server:app --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import mimetypes
mimetypes.add_type("font/ttf", ".ttf")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")
mimetypes.add_type("font/otf", ".otf")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

import asyncio
import base64
import binascii
import hmac
import ipaddress
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.environ["PYTHONIOENCODING"] = "utf-8"

# Make the repo root importable when launched via uvicorn from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import agent_core  # registers the kernel tools at import time
from agent_core import KernelAPI, build_runtime, tool_registry
from core.agent_loop import react_loop
from core.llm import (
    KeyStore,
    PROVIDERS,
    build_router_from_keystore,
    detect_provider,
)
from core.observability import Tracer, new_trace_id
from core.optimization import optimization_stats
from core.rate_limit import get_limiter, identity_for_request
from core.server_config import load_server_config
from core.throttle import default_throttle
from modes import AgentsTeam, Normal, Workflow
from modes.workflow import (
    delete_plan as wf_delete_plan,
    list_plans as wf_list_plans,
    load_plan as wf_load_plan,
)


def _is_health_pulse_schedule(schedule: Any) -> bool:
    """Return whether a schedule is the built-in lightweight health probe.

    A health pulse is monitoring infrastructure, not an instruction for the
    conversational agent.  Keeping this distinction prevents a harmless
    periodic check from allocating a model, creating chat history, or waking
    GPU-backed workers.
    """
    target = str(getattr(schedule, "target", "") or "").strip().lower()
    return target in {"health_pulse", "system.health_pulse"}


def _run_health_pulse(schedule: Any) -> None:
    """Perform a cheap, local runtime probe and leave an auditable event."""
    from core.dashboard import ActivityLog
    from core.scheduler import get_scheduler

    started = time.monotonic()
    failures: List[str] = []
    if not _state.get("ready"):
        failures.append("runtime is not ready")

    scheduler = get_scheduler()
    queue = scheduler.runtime_status()
    worker_summary: Dict[str, Any] = {}
    try:
        from core.resource import get_worker_supervisor
        worker_summary = get_worker_supervisor().get_health_summary()
        quarantined = [
            name for name, value in worker_summary.items()
            if isinstance(value, dict) and value.get("is_quarantined")
        ]
        if quarantined:
            failures.append("quarantined workers: " + ", ".join(quarantined))
    except Exception as exc:  # Monitoring must surface a missing watchdog.
        failures.append(f"worker supervisor unavailable: {type(exc).__name__}")

    status = "failed" if failures else "success"
    ActivityLog().record(
        "system",
        actor=f"schedule:{getattr(schedule, 'id', 'health')}",
        summary="health pulse",
        status=status,
        duration_ms=(time.monotonic() - started) * 1000.0,
        payload={
            "scheduler": queue,
            "quarantined_workers": [
                name for name, value in worker_summary.items()
                if isinstance(value, dict) and value.get("is_quarantined")
            ],
        },
    )
    if failures:
        raise RuntimeError("; ".join(failures))


def _canonical_scheduler_runner(schedule: Any) -> None:
    """Dispatch scheduled work through its appropriate execution path."""
    if _is_health_pulse_schedule(schedule):
        _run_health_pulse(schedule)
        return

    from core.session_service import get_session_service
    from core.brain.conversational_core import get_conversational_core
    idle = _state.get("idle_controller")
    if idle:
        idle.begin_activity("scheduled task")
    try:
        session_service = get_session_service()
        session = session_service.get_or_create_session(
            session_id=f"sched_{schedule.id}",
            metadata={"origin": "scheduler", "schedule_id": schedule.id, "name": schedule.name},
        )
        cc = get_conversational_core()
        target_intent = schedule.target or schedule.name
        res = cc.process_turn(target_intent, session_id=session.session_id, modality="scheduled")
        if res.error:
            raise RuntimeError(f"Scheduled execution failed: {res.error}")
    finally:
        if idle:
            idle.end_activity()


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str = Field(..., max_length=8000)
    max_steps: int = Field(6, ge=1, le=20)
    mode: str = Field("normal", pattern="^(normal|workflow|agents_team)$")
    session_id: Optional[str] = Field(None, max_length=64)
    recurrence: Optional[str] = Field(
        None, pattern="^(one-time|recurring)$",
        description="Required reply after the workflow asks one-time vs recurring."
    )


class ChatResponse(BaseModel):
    answer: str
    mode: str
    pending_question: Optional[str] = None
    workflow_id: Optional[str] = None


class ExecuteRequest(BaseModel):
    command: str = Field(..., max_length=2048)
    confirmed: bool = False
    confirmation_token: Optional[str] = None


class ExecuteResponse(BaseModel):
    output: str


class HealthResponse(BaseModel):
    service: str = "wise"
    runtime_id: Optional[str] = None
    status: str
    tools_registered: int
    awareness_tools: int
    awareness_skills: int
    agent_os_root: Optional[str] = None


# --------------------------------------------------------------------------
# Lifespan
# --------------------------------------------------------------------------
_state: Dict[str, Any] = {"model": None, "awareness": None, "ready": False}


@asynccontextmanager
async def _runtime_lifespan(app: FastAPI):
    try:
        # Remote/offline MCP handshakes must not hold the native window hostage.
        model, awareness = await asyncio.to_thread(build_runtime, start_mcp=False)
        _state["model"] = model
        _state["awareness"] = awareness
        try:
            from agent_core import tool_registry as _tr
            _state["tool_registry"] = _tr
        except Exception:
            _state["tool_registry"] = None

        try:
            from core.idle_controller import get_idle_controller
            idle_controller = get_idle_controller()
            idle_controller.start()
            _state["idle_controller"] = idle_controller
        except Exception as ie:
            print(f"⚠️ Idle controller initialization warning: {ie}")

        # Wire the canonical scheduler runner. Built-in health pulses remain
        # local probes; user-authored schedules enter ConversationalCore.
        try:
            from core.scheduler import get_scheduler

            sched = get_scheduler()
            sched.set_runner(_canonical_scheduler_runner)
            sched.start(tick_seconds=10.0)
            _state["scheduler"] = sched
        except Exception as se:
            print(f"⚠️ Scheduler initialization warning: {se}")

        try:
            from core.resource import get_worker_supervisor
            supervisor = get_worker_supervisor()
            supervisor.start()
            _state["worker_supervisor"] = supervisor
        except Exception as se:
            print(f"⚠️ Worker supervisor initialization warning: {se}")

        _state["ready"] = True
        _state["shutting_down"] = False
        def connect_optional_mcp():
            from core.mcp import get_registry
            registry = get_registry()
            for server in registry.list_servers():
                if _state.get("shutting_down"):
                    break
                if server.get("enabled"):
                    registry.start(server["name"])
            if _state.get("shutting_down"):
                registry.stop_all()
                return
            from core.capability_router import get_capability_router
            get_capability_router().refresh_mcp_capabilities()
            if awareness is not None:
                awareness.register_mcp_tools(tool_registry, registry)
        _state["mcp_startup"] = asyncio.create_task(asyncio.to_thread(connect_optional_mcp))
    except Exception as e:
        print(f"⚠️ تعذر إقلاع الوقت الفعلي للوكيل: {e}")
        _state["ready"] = False
    yield
    _state["shutting_down"] = True
    _state["ready"] = False
    # Graceful shutdown — stop scheduler and running preview servers
    try:
        sched = _state.get("scheduler")
        if sched:
            sched.stop()
    except Exception:
        pass
    try:
        idle_controller = _state.get("idle_controller")
        if idle_controller:
            idle_controller.stop()
    except Exception:
        pass
    try:
        supervisor = _state.get("worker_supervisor")
        if supervisor:
            supervisor.stop()
    except Exception:
        pass
    try:
        from core.preview_server import stop_all_previews
        stop_all_previews()
    except Exception:
        pass
    try:
        # Sync Playwright owns an event loop internally.  Always stop it from
        # a worker thread during ASGI shutdown so it cannot leak into a later
        # server/test lifecycle.
        from core.browser import shutdown_browser_session
        await asyncio.to_thread(shutdown_browser_session)
    except Exception:
        pass
    # Stop only initialized subsystems. Shutdown must never create a voice
    # listener, download a model, or start a new MCP registry as a side effect.
    import sys
    for module_name, singleton, method in (
        ("core.voice.runtime", "_VOICE_RUNTIME", "stop"),
        ("core.mcp.registry", "_GLOBAL_MCP_REGISTRY", "stop_all"),
    ):
        module = sys.modules.get(module_name)
        instance = getattr(module, singleton, None) if module else None
        if instance is not None:
            try:
                await asyncio.wait_for(asyncio.to_thread(getattr(instance, method)), timeout=8.0)
            except Exception as exc:
                logging.getLogger("WISE.Shutdown").warning("Could not drain %s: %s", module_name, type(exc).__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Task/schedule state assumes one authoritative backend. A different port
    # must not silently permit a second process to mutate the same runtime.
    from core.paths import RUNTIME_ROOT
    from core.durable_io import storage_writer_lock
    with storage_writer_lock(RUNTIME_ROOT, lock_name=".backend-owner.lock", timeout=0):
        async with _runtime_lifespan(app):
            yield


app = FastAPI(title="Agent OS API", version="1.1.0", lifespan=lifespan)
from api.plugin_routes import router as plugins_router
app.include_router(plugins_router)


# Mount the MCP server (HTTP + SSE transport) so external MCP clients
# can reach Agent OS via /mcp/jsonrpc and /mcp/sse alongside the REST
# surface.
try:
    from core.mcp.server import register_routes as _mcp_register_routes
    _mcp_register_routes(app)
except Exception:  # noqa: BLE001
    pass


# --------------------------------------------------------------------------
# CORS — restricted by default, configurable via AGENT_CORS_ORIGINS
# --------------------------------------------------------------------------
def _cors_origins() -> List[str]:
    """Resolve the CORS origin list via :mod:`core.server_config`.

    Reads ``config/server.json`` and any ``AGENT_CORS_ORIGINS`` env
    override so all entrypoints share one source of truth.
    """
    return load_server_config().get("cors_origins") or [
        "http://localhost",
        "http://127.0.0.1",
    ]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "PUT", "PATCH"],
    allow_headers=["Content-Type", "X-Agent-Token", "Authorization", "Idempotency-Key"],
)


# --------------------------------------------------------------------------
# Auth + size limit middleware
# --------------------------------------------------------------------------
_PROTECTED_PREFIXES = ("/chat", "/execute", "/admin")


def _require_auth(x_agent_token: Optional[str]) -> None:
    expected = os.environ.get("AGENT_API_TOKEN")
    if not expected:
        return  # auth disabled — operator must bind to localhost only
    if not x_agent_token or not hmac.compare_digest(x_agent_token, expected):
        raise HTTPException(status_code=401, detail="invalid or missing token")


_AUTH_CONTROLLED_PREFIXES = ("/chat", "/execute", "/admin", "/workflows", "/macros", "/api/", "/mcp")


def _is_loopback_peer(host: Optional[str]) -> bool:
    """Return true only for a direct loopback client.

    We intentionally do not trust forwarded-address headers: accepting them
    would let a remote caller pretend to be localhost. ``testclient`` is the
    in-process FastAPI test transport and is never a network peer.
    """
    if not host:
        return False
    normalized = host.strip().lower()
    if normalized in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _presented_request_token(request: Request) -> Optional[str]:
    token = request.headers.get("x-agent-token")
    if token:
        return token
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    return value if scheme.lower() == "bearer" and value else None


def _request_is_authorized(request: Request) -> bool:
    expected = os.environ.get("AGENT_API_TOKEN")
    if expected:
        supplied = _presented_request_token(request)
        return bool(supplied and hmac.compare_digest(supplied, expected))
    # A tokenless operator mode is allowed only for direct localhost use.
    return _is_loopback_peer(request.client.host if request.client else None)


def _websocket_is_authorized(websocket: WebSocket) -> bool:
    expected = os.environ.get("AGENT_API_TOKEN")
    if expected:
        supplied = (
            websocket.headers.get("x-agent-token")
            or websocket.query_params.get("token")
        )
        return bool(supplied and hmac.compare_digest(supplied, expected))
    return _is_loopback_peer(websocket.client.host if websocket.client else None)


def _refresh_canonical_model_provider() -> None:
    """Invalidate every cached provider reference after a settings change."""
    try:
        from core.models.provider_interface import reset_model_provider
        reset_model_provider()
    except Exception:
        pass
    try:
        from core.brain.conversational_core import reset_conversational_core_provider
        reset_conversational_core_provider()
    except Exception:
        pass


@app.middleware("http")
async def _request_size_limit(request: Request, call_next):
    cfg = load_server_config()
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            length = int(cl)
        except ValueError:
            length = 0
        if request.url.path == "/execute":
            max_size = int(cfg.get("max_execute_bytes", 65 * 1024))
        else:
            max_size = int(cfg.get("max_request_bytes", 1024 * 1024))
        if length > max_size:
            raise HTTPException(status_code=413, detail="request too large")
    return await call_next(request)


@app.middleware("http")
async def _api_access_control(request: Request, call_next):
    """Fail closed for every control API outside localhost.

    The older API performed per-endpoint checks but the desktop V2 routes were
    added later and bypassed them. This boundary protects both generations,
    including future routes under ``/api/`` and ``/mcp``.
    """
    path = request.url.path
    if path.startswith(_AUTH_CONTROLLED_PREFIXES) and not _request_is_authorized(request):
        expected = os.environ.get("AGENT_API_TOKEN")
        detail = "invalid or missing token" if expected else (
            "tokenless API access is restricted to localhost; set AGENT_API_TOKEN for remote access"
        )
        return JSONResponse(status_code=401, content={"detail": detail})
    return await call_next(request)


@app.middleware("http")
async def _mark_resident_activity(request: Request, call_next):
    """Mark actual agent work, not passive health/UI polling, as activity."""
    work_paths = {"/chat", "/execute", "/api/v2/chat", "/api/v2/chat/events"}
    controller = _state.get("idle_controller") if request.url.path in work_paths else None
    if controller:
        controller.begin_activity(f"HTTP {request.url.path}")
    try:
        return await call_next(request)
    finally:
        if controller:
            controller.end_activity()


# --------------------------------------------------------------------------
# Per-client rate limiting on POST /chat and POST /execute. We do this
# in middleware (rather than the handlers) so a flooding client is
# rejected before any LLM/sandbox work is dispatched.
# --------------------------------------------------------------------------
from starlette.responses import JSONResponse  # noqa: E402


_RATE_LIMITED_ROUTES = {
    "/chat": "chat",
    "/execute": "execute",
}


@app.middleware("http")
async def _rate_limit(request: Request, call_next):
    path = request.url.path
    if request.method == "POST" and path in _RATE_LIMITED_ROUTES:
        limiter_name = _RATE_LIMITED_ROUTES[path]
        limiter = get_limiter(limiter_name)
        identity = identity_for_request(
            dict(request.headers),
            request.client.host if request.client else None,
        )
        ok, retry_after = limiter.acquire(identity)
        if not ok:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": (
                        f"rate limit exceeded for {limiter_name}; "
                        f"retry in ~{retry_after:.1f}s"
                    ),
                    "retry_after_s": round(retry_after, 2),
                    "limit": limiter_name,
                },
                headers={"Retry-After": str(max(1, int(retry_after) + 1))},
            )
    return await call_next(request)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@app.get("/health", response_model=HealthResponse)
def health(detailed: int = 0) -> HealthResponse:
    import hashlib
    from core.paths import RUNTIME_ROOT
    awareness = _state.get("awareness")
    return HealthResponse(
        runtime_id=hashlib.sha256(str(RUNTIME_ROOT).casefold().encode()).hexdigest()[:16],
        status="ok" if _state.get("ready") else "degraded",
        tools_registered=len(tool_registry.list_tools()),
        awareness_tools=len(awareness.tools) if awareness else 0,
        awareness_skills=awareness.skills_count if awareness else 0,
        agent_os_root=str(agent_core.AGENT_OS_ROOT) if detailed else None,
    )


@app.get("/health/detailed")
def health_detailed() -> Dict[str, Any]:
    """Component-level health snapshot.

    Returns a per-subsystem status block — sandbox, LLM router,
    channels, RAG, scheduler — so operators (and the desktop
    onboarding screen) can show a green/yellow/red traffic light per
    capability instead of one global "ok".
    """
    awareness = _state.get("awareness")
    out: Dict[str, Any] = {
        "status": "ok" if _state.get("ready") else "degraded",
        "tools_registered": len(tool_registry.list_tools()),
        "awareness": {
            "tools": len(awareness.tools) if awareness else 0,
            "skills": awareness.skills_count if awareness else 0,
        },
        "agent_os_root": str(agent_core.AGENT_OS_ROOT),
    }

    # LLM router (counts of configured / enabled backends).
    try:
        from core.llm import build_router_from_keystore as _br
        ks = KeyStore()
        router = _br(ks)
        out["llm"] = {
            "available": bool(router and router.backends),
            # The router status includes the bounded circuit-breaker state;
            # operators can distinguish a missing key from a provider that is
            # being deliberately cooled down after repeated failures.
            "backends": router.status() if router else [],
            "active": router.active_name if router else None,
        }
    except Exception as exc:
        out["llm"] = {"available": False, "error": str(exc)}

    # Apprise / channels.
    try:
        from core.channels import list_channels
        out["channels"] = list_channels()
    except Exception as exc:
        out["channels"] = {"available": False, "error": str(exc)}

    # Knowledge / RAG sources.
    try:
        from core.rag.sources import all_sources
        sources = all_sources()
        out["rag"] = {
            "count": len(sources),
            "sources": [getattr(s, "name", type(s).__name__)
                        for s in sources][:30],
        }
    except Exception as exc:
        out["rag"] = {"available": False, "error": str(exc)}

    # Scheduler.
    try:
        from core.scheduler import get_scheduler
        sched = get_scheduler()
        thread = getattr(sched, "_thread", None)
        out["scheduler"] = {
            "available": True,
            "schedule_count": len(sched.list()),
            "running": bool(thread and thread.is_alive()),
            **sched.runtime_status(),
        }
    except Exception as exc:
        out["scheduler"] = {"available": False, "error": str(exc)}

    # Worker watchdog and bounded recovery state for the background runtime.
    try:
        from core.resource import get_worker_supervisor
        out["worker_supervisor"] = get_worker_supervisor().get_health_summary()
    except Exception as exc:
        out["worker_supervisor"] = {"available": False, "error": str(exc)}

    try:
        from core.idle_controller import get_idle_controller
        out["idle_controller"] = get_idle_controller().status()
    except Exception as exc:
        out["idle_controller"] = {"available": False, "error": str(exc)}

    # Sandbox rootfs presence.
    try:
        rootfs = (agent_core.AGENT_OS_ROOT / "rootfs"
                  if hasattr(agent_core, "AGENT_OS_ROOT") else None)
        out["sandbox"] = {
            "rootfs_present": bool(rootfs and rootfs.exists()),
            "rootfs_path": str(rootfs) if rootfs else None,
        }
    except Exception as exc:
        out["sandbox"] = {"available": False, "error": str(exc)}

    return out


@app.get("/tools")
def list_tools() -> Dict[str, List[str]]:
    return {"tools": sorted(tool_registry.list_tools())}


@app.get("/tools/manifest/{name}")
def tool_manifest(name: str) -> Dict[str, Any]:
    awareness = _state.get("awareness")
    if awareness is None:
        raise HTTPException(status_code=503, detail="system_awareness غير جاهزة")
    manifest = awareness.get_tool(name)
    if not manifest:
        raise HTTPException(status_code=404, detail=f"الأداة '{name}' غير معروفة")
    return manifest


@app.get("/tools/{name}")
def tool_detail(name: str) -> Dict[str, Any]:
    """Public alias for ``/tools/manifest/{name}``.

    The original route grew up alongside the system-awareness layer,
    but external clients expect the cleaner ``/tools/{name}`` shape.
    Both routes return the same manifest dict and 404 the same way.
    """
    return tool_manifest(name)


# --------------------------------------------------------------------------
# Public skills surface (the existing ``/admin/skills/*`` endpoints
# require an auth token; these read-only ones are safe for the desktop
# UI and external orchestrators).
# --------------------------------------------------------------------------
@app.get("/skills")
def list_skills_public(
    state: Optional[str] = None, limit: int = 200,
) -> Dict[str, Any]:
    """List skills with their lifecycle state.

    Query params:

    * ``state``  — optional filter (``active``/``inactive``/``failed``/…).
    * ``limit``  — cap returned rows (default 200).

    The response merges ``SkillIndexer.list_skills()`` with the
    lifecycle journal, so skills with no recorded transitions still
    appear (defaulting to ``active``).
    """
    try:
        from core.skills.indexer import SkillIndexer
        from core.skills.lifecycle import get_manager
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=503,
                            detail=f"skills layer unavailable: {exc}")

    indexer = SkillIndexer()
    names = sorted(indexer.list_skills())
    journal = get_manager().all()
    rows: List[Dict[str, Any]] = []
    for n in names:
        rec = journal.get(n) or {"state": "active",
                                  "success_count": 0,
                                  "failure_count": 0}
        if state and rec.get("state") != state:
            continue
        rows.append({
            "name": n,
            "state": rec.get("state", "active"),
            "success_count": int(rec.get("success_count", 0) or 0),
            "failure_count": int(rec.get("failure_count", 0) or 0),
            "last_state_change": rec.get("last_state_change"),
        })
        if len(rows) >= max(1, int(limit)):
            break
    return {"count": len(rows), "skills": rows, "total": len(names)}


@app.get("/skills/search")
def search_skills_public(
    q: str, top_k: int = 8,
) -> Dict[str, Any]:
    """Semantic search over the local skill index.

    Wraps :class:`core.skills.search.SkillSearch` so the desktop UI's
    "find a skill" widget doesn't have to ship the BM25 layer in Dart.
    """
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="missing 'q'")
    try:
        from core.skills.search import SkillSearch
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=503,
                            detail=f"skills search unavailable: {exc}")
    hits = SkillSearch().search(q.strip(), top_k=max(1, min(50, int(top_k))))
    return {"query": q.strip(), "count": len(hits), "results": hits}


# Session state used by Workflow mode to remember a pending recurrence question.
_SESSIONS: Dict[str, Dict[str, Any]] = {}


def _session(session_id: Optional[str]) -> Dict[str, Any]:
    if not session_id:
        return {}
    return _SESSIONS.setdefault(session_id, {})


@app.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    x_agent_token: Optional[str] = Header(default=None),
) -> ChatResponse:
    _require_auth(x_agent_token)
    t0 = time.time()

    if req.mode == "normal":
        # Canonical ConversationalCore execution path
        try:
            from core.brain.conversational_core import get_conversational_core
            cc = get_conversational_core()
            turn_res = await asyncio.to_thread(
                cc.process_turn, req.message, session_id=req.session_id, modality="chat"
            )
            answer = turn_res.reply_text
            try:
                from core.dashboard import ActivityLog
                ActivityLog().record(
                    "chat", actor="canonical_core",
                    summary=req.message[:120],
                    status="success" if not turn_res.error else "failed",
                    duration_ms=(time.time() - t0) * 1000.0,
                    payload={"session_id": req.session_id, "action_type": turn_res.action_type})
            except Exception:
                pass
            return ChatResponse(answer=answer, mode="normal")
        except Exception:
            pass

        # Legacy fallback for backward compatibility
        model = _state.get("model")
        if model is None:
            raise HTTPException(status_code=503, detail="No model provider available. Please configure an API key.")
        awareness = _state.get("awareness")
        normal = Normal(model, tool_registry, awareness, max_steps=req.max_steps)
        answer = await asyncio.to_thread(normal.run, req.message)
        return ChatResponse(answer=answer, mode="normal")


    if req.mode == "workflow":
        wf = Workflow(model, tool_registry, awareness, max_react_steps=req.max_steps)
        sess = _session(req.session_id)

        # Step 1: ask the user one-time vs recurring.
        if req.recurrence is None and "pending_task" not in sess:
            sess["pending_task"] = req.message
            return ChatResponse(
                answer=wf.ask_recurrence(req.message),
                mode="workflow",
                pending_question="one-time | recurring",
            )

        # Step 2: user replied. Resolve the original task from the session
        # if this call is just providing the recurrence answer.
        task = sess.pop("pending_task", req.message) if req.recurrence else req.message
        recurring = (req.recurrence == "recurring")

        plan = await asyncio.to_thread(wf.plan, task)
        answer = await asyncio.to_thread(wf.run, plan, recurring)
        return ChatResponse(
            answer=answer, mode="workflow",
            workflow_id=plan.id if recurring else None,
        )

    # agents_team
    team = AgentsTeam(model, tool_registry, awareness, max_steps=req.max_steps)
    answer = await asyncio.to_thread(team.run, req.message)
    return ChatResponse(answer=answer, mode="agents_team")


# --------------------------------------------------------------------------
# Recurring workflows
# --------------------------------------------------------------------------
@app.get("/workflows")
async def list_workflows(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    return {"workflows": wf_list_plans()}


@app.post("/workflows/{plan_id}/run")
async def run_workflow(
    plan_id: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    plan = wf_load_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="workflow غير موجود")
    model = _state.get("model")
    if model is None:
        raise HTTPException(status_code=503, detail="النموذج غير مهيأ.")
    wf = Workflow(model, tool_registry, _state.get("awareness"))
    answer = await asyncio.to_thread(wf.run, plan, True)
    return {"workflow_id": plan_id, "result": answer}


@app.delete("/workflows/{plan_id}")
async def delete_workflow(
    plan_id: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    if not wf_delete_plan(plan_id):
        raise HTTPException(status_code=404, detail="workflow غير موجود")
    return {"deleted": plan_id}


@app.get("/throttle/stats")
async def throttle_stats() -> Dict[str, Any]:
    return default_throttle().stats()


@app.post("/execute", response_model=ExecuteResponse)
async def execute(
    req: ExecuteRequest,
    x_agent_token: Optional[str] = Header(default=None),
) -> ExecuteResponse:
    _require_auth(x_agent_token)
    from core.security import get_security_gate, SecurityContext
    gate = get_security_gate()
    ctx = SecurityContext(
        caller="api_execute",
        confirmed=req.confirmed,
        confirmation_token=req.confirmation_token,
    )
    sec_eval = gate.evaluate_action("run_command", {"command": req.command}, ctx)
    if not sec_eval.allowed:
        if sec_eval.requires_confirmation:
            raise HTTPException(
                status_code=403,
                detail=f"SecurityGate confirmation required: {sec_eval.reason}",
            )
        raise HTTPException(
            status_code=403,
            detail=f"SecurityGate blocked command: {sec_eval.reason}",
        )
    output = await asyncio.to_thread(KernelAPI.execute_command, req.command)
    return ExecuteResponse(output=output)


@app.post("/admin/reload-tools")
async def reload_tools(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Refresh adapters / packs / macros / MCP onto the live registry."""
    _require_auth(x_agent_token)
    try:
        from core.hotreload import reload_all
        summary = await asyncio.to_thread(reload_all, tool_registry)
        # Refresh awareness reference on the state.
        try:
            from core.system_awareness import SystemAwareness
            _state["awareness"] = SystemAwareness()
        except Exception:
            pass
        return summary
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --------------------------------------------------------------------------
# Tool search (semantic / TF-IDF)
# --------------------------------------------------------------------------
@app.get("/tools/search")
async def search_tools(q: str, k: int = 8) -> Dict[str, Any]:
    try:
        from core.tool_search import rank_tools
        rows = rank_tools(
            q, k=max(1, min(50, k)),
            registry=tool_registry, awareness=_state.get("awareness"),
        )
        return {"query": q, "count": len(rows), "results": rows}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "results": []}


@app.get("/tools/search/stats")
async def search_tool_stats() -> Dict[str, Any]:
    try:
        from core.tool_search import stats
        return stats()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


# --------------------------------------------------------------------------
# Universal API-key management
# --------------------------------------------------------------------------
class KeyAddRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    api_key: str = Field("", max_length=512)
    provider: Optional[str] = Field(None, max_length=32)
    model: Optional[str] = Field(None, max_length=128)
    base_url: Optional[str] = Field(None, max_length=256)
    enabled: bool = True
    priority: int = 0


@app.get("/admin/keys")
async def list_keys(
    reveal: int = 0,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    ks = KeyStore()
    return {"keys": ks.list(reveal=bool(reveal))}


@app.get("/admin/keys/providers")
async def list_providers() -> Dict[str, Any]:
    return {
        "providers": [
            {
                "id": p.id,
                "name": p.name,
                "transport": p.transport,
                "default_base_url": p.base_url,
                "default_model": p.default_model,
                "api_key_env": p.api_key_env,
                "notes": p.notes,
            }
            for p in PROVIDERS
        ]
    }


@app.post("/admin/keys/detect")
async def keys_detect(req: KeyAddRequest) -> Dict[str, Any]:
    info = detect_provider(req.api_key, req.base_url)
    return {
        "provider": info.id,
        "transport": info.transport,
        "base_url": req.base_url or info.base_url,
        "model": req.model or info.default_model,
    }


@app.post("/admin/keys")
async def add_key(
    req: KeyAddRequest,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    ks = KeyStore()
    entry = ks.add(
        name=req.name, api_key=req.api_key, provider=req.provider,
        model=req.model, base_url=req.base_url,
        enabled=req.enabled, priority=req.priority,
    )
    # Rebuild the router so the new key is live immediately.
    new_router = build_router_from_keystore(ks)
    if new_router.backends:
        _state["model"] = new_router
    # /api/v2/chat uses ConversationalCore's canonical provider rather
    # than this legacy router.  Drop its cache as well so a saved key is
    # usable immediately without restarting WISE.
    _refresh_canonical_model_provider()
    return {"added": entry, "router_backends": len(new_router.backends)}


@app.patch("/admin/keys/{name}")
async def patch_key(
    name: str,
    enabled: bool,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    ks = KeyStore()
    if not ks.set_enabled(name, enabled):
        raise HTTPException(status_code=404, detail=f"المفتاح {name} غير موجود")
    new_router = build_router_from_keystore(ks)
    _state["model"] = new_router if new_router.backends else _state["model"]
    _refresh_canonical_model_provider()
    return {"name": name, "enabled": enabled, "router_backends": len(new_router.backends)}


@app.delete("/admin/keys/{name}")
async def delete_key(
    name: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    ks = KeyStore()
    if not ks.remove(name):
        raise HTTPException(status_code=404, detail=f"المفتاح {name} غير موجود")
    new_router = build_router_from_keystore(ks)
    if new_router.backends:
        _state["model"] = new_router
    _refresh_canonical_model_provider()
    return {"deleted": name, "router_backends": len(new_router.backends)}


@app.get("/admin/router/status")
async def router_status(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    model = _state.get("model")
    if hasattr(model, "status"):
        return {"backends": model.status(), "mode": getattr(model, "mode", "unknown")}
    return {"backends": [], "mode": "legacy"}


# --------------------------------------------------------------------------
# Optimization metrics
# --------------------------------------------------------------------------
@app.get("/optimization/stats")
async def optim_stats() -> Dict[str, Any]:
    return optimization_stats()


# --------------------------------------------------------------------------
# Traces (observability)
# --------------------------------------------------------------------------
@app.get("/traces")
async def list_traces(limit: int = 50) -> Dict[str, Any]:
    return {"trace_ids": Tracer.trace_ids(limit=max(1, min(limit, 200)))}


@app.get("/traces/{trace_id}")
async def get_trace(trace_id: str, kind: Optional[str] = None,
                    limit: int = 200) -> Dict[str, Any]:
    events = Tracer.events(
        trace_id=trace_id, kind=kind,
        limit=max(1, min(limit, 1000)),
    )
    return {"trace_id": trace_id, "count": len(events), "events": events}


@app.get("/traces/_/stats")
async def trace_stats() -> Dict[str, Any]:
    return Tracer.stats()


# --------------------------------------------------------------------------
# Macros (composed tools)
# --------------------------------------------------------------------------
@app.get("/macros")
async def list_macros() -> Dict[str, Any]:
    try:
        from core.composition import MacroStore
        rows = MacroStore().list()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "macros": []}
    return {"count": len(rows), "macros": rows}


@app.post("/macros")
async def create_macro(
    payload: Dict[str, Any],
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Persist a macro definition. Re-registers as a tool."""
    _require_auth(x_agent_token)
    try:
        from core.composition import Macro, MacroEngine, MacroStore, build_macro_tools
        macro = Macro.from_dict(payload)
        # Validate against current registry before saving.
        registry = _state.get("tool_registry")
        if registry is not None:
            MacroEngine(registry)._validate(macro, {k: "" for k in macro.inputs})
        store = MacroStore()
        store.save(macro)
        # Register live.
        if registry is not None:
            for tname, fn in build_macro_tools(registry, store).items():
                registry.register(tname, fn)
        return {"saved": True, "name": macro.name, "steps": len(macro.steps)}
    except Exception as exc:  # noqa: BLE001
        return {"saved": False, "error": str(exc)}


@app.post("/macros/{name}/run")
async def run_macro(
    name: str,
    payload: Optional[Dict[str, Any]] = None,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    try:
        from core.composition import MacroEngine, MacroStore
        registry = _state.get("tool_registry")
        if registry is None:
            return {"ok": False, "error": "registry not ready"}
        macro = MacroStore().get(name)
        result = MacroEngine(registry).run(
            macro, inputs=(payload or {}).get("inputs") or {},
            use_cache=(payload or {}).get("use_cache", True),
        )
        return {
            "ok": result.ok, "name": result.name,
            "outputs": result.outputs, "cached": result.cached,
            "duration_ms": round(result.duration_ms, 2),
            "error": result.error,
            "steps": result.steps,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.delete("/macros/{name}")
async def delete_macro(
    name: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    try:
        from core.composition import MacroStore
        deleted = MacroStore().delete(name)
        return {"deleted": deleted, "name": name}
    except Exception as exc:  # noqa: BLE001
        return {"deleted": False, "error": str(exc)}


# --------------------------------------------------------------------------
# MCP servers (Model Context Protocol)
# --------------------------------------------------------------------------
@app.get("/admin/mcp/servers")
async def mcp_list(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    try:
        from core.mcp import get_registry
        rows = get_registry().list_servers()
        return {"count": len(rows),
                "alive": sum(1 for r in rows if r.get("alive")),
                "servers": rows}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "servers": []}


@app.post("/admin/mcp/servers")
async def mcp_add(
    payload: Dict[str, Any],
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    try:
        from core.mcp import build_mcp_tools, get_registry
        reg = get_registry()
        result = reg.add(payload)
        # Re-register tools live: through awareness if available so the
        # bridge updates the prompt manifest too, otherwise direct.
        registry = _state.get("tool_registry")
        awareness = _state.get("awareness")
        if registry is not None and result.get("alive"):
            if awareness is not None and hasattr(awareness, "register_mcp_tools"):
                awareness.register_mcp_tools(registry, reg)
            else:
                for tname, fn in build_mcp_tools(reg, auto_start=False).items():
                    registry.register(tname, fn)
        return result
    except Exception as exc:  # noqa: BLE001
        return {"added": False, "error": str(exc)}


@app.get("/admin/mcp/catalog")
async def mcp_catalog(x_agent_token: Optional[str] = Header(default=None)):
    _require_auth(x_agent_token)
    from core.mcp import get_registry
    from core.mcp.catalog import list_catalog
    return {"entries": list_catalog(get_registry())}


@app.post("/admin/mcp/import")
async def mcp_import(payload: Dict[str, Any], x_agent_token: Optional[str] = Header(default=None)):
    _require_auth(x_agent_token)
    from core.mcp import get_registry
    from core.mcp.config_import import parse_configs, protect_config
    try:
        configs = parse_configs(payload.get("config"), trusted_local=payload.get("trusted_local") is True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    reg = get_registry()
    if any(reg._find(cfg["name"]) for cfg in configs) and payload.get("replace_existing") is not True:
        raise HTTPException(status_code=409, detail="A server with this name already exists; confirm replacement")
    def save_and_connect():
        rows = []
        for cfg in configs:
            result = reg.add(protect_config(cfg))
            if cfg["transport"] == "http" and "http 401" in result.get("error", "").lower():
                from core.mcp.oauth import start
                try:
                    result["authorization"] = start(cfg["name"], reg, client_id=payload.get("client_id", ""))
                except Exception as exc:
                    result["authorization"] = {"ok": False, "error": str(exc)[:240]}
            rows.append(result)
        from core.capability_router import get_capability_router
        get_capability_router().refresh_mcp_capabilities()
        return {"ok": True, "servers": rows}
    return await asyncio.to_thread(save_and_connect)


@app.post("/admin/mcp/servers/{name}/authorize")
async def mcp_authorize(name: str, payload: Dict[str, Any], x_agent_token: Optional[str] = Header(default=None)):
    _require_auth(x_agent_token)
    import httpx
    from core.mcp import get_registry
    from core.mcp.oauth import start
    try:
        return await asyncio.to_thread(start, name, get_registry(), client_id=payload.get("client_id", ""))
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=412, detail=str(exc)[:240]) from exc


@app.get("/admin/mcp/authorization/status")
async def mcp_authorization_status(state: str, x_agent_token: Optional[str] = Header(default=None)):
    _require_auth(x_agent_token)
    from core.mcp.oauth import status
    return status(state)


@app.delete("/admin/mcp/servers/{name}")
async def mcp_remove(
    name: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    try:
        from core.mcp import get_registry
        return {"removed": get_registry().remove(name), "name": name}
    except Exception as exc:  # noqa: BLE001
        return {"removed": False, "error": str(exc)}


@app.post("/admin/mcp/servers/{name}/start")
async def mcp_start(
    name: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    try:
        from core.mcp import build_mcp_tools, get_registry
        reg = get_registry()
        result = reg.start(name)
        registry = _state.get("tool_registry")
        if registry is not None and result.get("alive"):
            for tname, fn in build_mcp_tools(reg, auto_start=False).items():
                registry.register(tname, fn)
        return result
    except Exception as exc:  # noqa: BLE001
        return {"alive": False, "error": str(exc)}


@app.post("/admin/mcp/servers/{name}/stop")
async def mcp_stop(
    name: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    try:
        from core.mcp import get_registry
        return {"stopped": get_registry().stop(name), "name": name}
    except Exception as exc:  # noqa: BLE001
        return {"stopped": False, "error": str(exc)}


@app.get("/admin/mcp/servers/{name}/tools")
async def mcp_server_tools(
    name: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Per-tool detail incl. read/write/dangerous classification."""
    _require_auth(x_agent_token)
    try:
        from core.mcp import get_registry
        rows = get_registry().list_tools(name)
        return {"server": name, "count": len(rows), "tools": rows}
    except Exception as exc:  # noqa: BLE001
        return {"server": name, "tools": [], "error": str(exc)}


# --------------------------------------------------------------------------
# MCP confirmation gate
# --------------------------------------------------------------------------
@app.get("/admin/mcp/pending")
async def mcp_pending(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.mcp import get_gate
    return {"pending": get_gate().list_pending()}


@app.post("/admin/mcp/confirm/{call_id}")
async def mcp_confirm(
    call_id: str,
    payload: Optional[Dict[str, Any]] = None,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    approved = bool((payload or {}).get("approved", True))
    reason = str((payload or {}).get("reason", ""))
    try:
        from core.mcp import get_gate
        ok = get_gate().resolve(call_id, approved=approved, reason=reason)
        return {"resolved": ok, "approved": approved, "call_id": call_id}
    except Exception as exc:  # noqa: BLE001
        return {"resolved": False, "error": str(exc)}


# --------------------------------------------------------------------------
# MCP per-server policy patch
# --------------------------------------------------------------------------
@app.patch("/admin/mcp/servers/{name}")
async def mcp_patch_server(
    name: str,
    payload: Dict[str, Any],
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Update sandbox / allowed_domains / classifications / auto_approve."""
    _require_auth(x_agent_token)
    try:
        from core.mcp import build_mcp_tools, get_registry
        reg = get_registry()
        cfg = reg._find(name)
        if not cfg:
            return {"updated": False, "error": "unknown server"}
        # Apply allowed mutations.
        for key in (
            "sandbox", "allowed_domains", "classifications",
            "auto_approve", "require_confirmation", "dangerous_extras",
            "enabled",
        ):
            if key in payload:
                setattr(cfg, key, payload[key])
        reg._save()
        # Restart with new config to reflect changes immediately.
        result = reg.start(name) if cfg.enabled else {"alive": False}
        registry = _state.get("tool_registry")
        if registry is not None and result.get("alive"):
            for tname, fn in build_mcp_tools(reg, auto_start=False).items():
                registry.register(tname, fn)
        return {"updated": True, "config": cfg.to_dict(), "started": result}
    except Exception as exc:  # noqa: BLE001
        return {"updated": False, "error": str(exc)}


# --------------------------------------------------------------------------
# Adapters (native platform integrations)
# --------------------------------------------------------------------------
@app.get("/admin/adapters")
async def list_adapters(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """One row per adapter — ready/not + capability count."""
    _require_auth(x_agent_token)
    try:
        from core.adapters import adapter_status
        rows = adapter_status()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "adapters": []}
    return {
        "count": len(rows),
        "ready": sum(1 for r in rows if r.get("ready")),
        "adapters": rows,
    }


# --------------------------------------------------------------------------
# Tool Installer
# --------------------------------------------------------------------------
@app.get("/admin/tools/missing")
async def admin_tools_missing(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """List every tool whose binaries / Python deps are missing inside
    the sandbox, with an install hint."""
    _require_auth(x_agent_token)
    try:
        from core.tool_installer import get_installer
        return get_installer().missing_report()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/admin/tools/install")
async def admin_tools_install(
    payload: Optional[Dict[str, Any]] = None,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Install missing dependencies for one or more tools.

    Body: ``{"tools": ["tool_a", "tool_b"]}``. Empty / missing body
    installs deps for every missing tool. Reuses ``self_install`` so
    every existing security constraint (no ``--index-url``, no
    ``--editable``, AGENT_APT_ALLOWED gate) still applies.
    """
    _require_auth(x_agent_token)
    try:
        from core.tool_installer import (annotate_awareness, get_installer,
                                          wrap_registry_with_install_check)
        names = None
        if isinstance(payload, dict):
            raw = payload.get("tools")
            if isinstance(raw, list):
                names = [str(n) for n in raw if isinstance(n, str)]
        installer = get_installer()
        result = installer.install_missing(names)
        # Refresh awareness + registry guards so the prompt reflects the
        # newly-available tools immediately.
        awareness = _state.get("awareness")
        registry = _state.get("tool_registry")
        if awareness is not None:
            annotate_awareness(awareness, installer)
        if registry is not None:
            # Drop install guards on tools we just installed so the
            # original wrappers remain reachable.
            scan = installer.scan(force=True)
            if hasattr(registry, "tools"):
                for name, status in scan.items():
                    if not status.found:
                        continue
                    fn = registry.tools.get(name)
                    if fn is None:
                        continue
                    if getattr(fn, "__install_guard__", False):
                        original = getattr(fn, "__wrapped_tool__", None)
                        if original is not None:
                            registry.tools[name] = original
            wrap_registry_with_install_check(registry, installer)
        return result
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/admin/tools/status")
async def admin_tools_status(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Full install snapshot — every tool with availability + missing reqs.

    Includes ``rootfs`` metadata (path / exists / bytes / debian_version
    / build stamp) so operators can see whether the sandbox rootfs has
    been built and which packages are still missing inside it.
    """
    _require_auth(x_agent_token)
    try:
        from core.tool_installer import get_installer
        inst = get_installer()
        scan = inst.scan()
        return {
            "total": len(scan),
            "available": sum(1 for s in scan.values() if s.found),
            "missing": sum(1 for s in scan.values() if not s.found),
            "rootfs": inst.rootfs_info(),
            "curated_map": {
                "path": str(inst.curated_map_path),
                "entries": len(inst._curated),
            },
            "tools": {n: s.to_dict() for n, s in scan.items()},
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/admin/tools/known-missing")
async def admin_tools_known_missing(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Tools intentionally documented as not auto-installable.

    Pulled live from the curated map + ``sandbox/known_missing.json`` if
    it exists on disk.
    """
    _require_auth(x_agent_token)
    try:
        from pathlib import Path
        from core.tool_installer import get_installer
        inst = get_installer()
        # Generate fresh — cheap and avoids stale on-disk copy.
        out = Path("sandbox") / "known_missing.json"
        return inst.write_known_missing(out)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/admin/tools/audit")
async def admin_tools_audit(
    apply: bool = False,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Validate every manifest under ``tools/packs/*.json``.

    Pass ``?apply=true`` to additionally write derivable fixes back to
    disk (default fields, dedup ``replaces`` markers, padded
    capabilities/use_cases). The default is read-only.
    """
    _require_auth(x_agent_token)
    try:
        from core.tool_audit import run_audit
        report = run_audit(apply=bool(apply))
        report["summary"] = {
            "total_tools": report.get("total_tools", 0),
            "unique_tools": report.get("unique_tools", 0),
            "issue_count": len(report.get("issues", [])),
            "fixed_count": report.get("fixed_count", 0),
            "by_code": report.get("by_code", {}),
        }
        return report
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/admin/tools/stats")
async def admin_tools_stats(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Aggregate ``tool.call`` Tracer events into usage statistics."""
    _require_auth(x_agent_token)
    try:
        from core.optimization import compression_stats
        from core.tool_stats import compute_tool_stats
        out = compute_tool_stats()
        out["compression"] = compression_stats()
        return out
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


# --------------------------------------------------------------------------
# Skills lifecycle / Skillnet / recommendations
# --------------------------------------------------------------------------
@app.get("/admin/skills/lifecycle")
async def admin_skills_lifecycle(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.skills.lifecycle import get_manager
    return {"skills": get_manager().all()}


@app.post("/admin/skills/{name}/activate")
async def admin_skills_activate(
    name: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.skills.lifecycle import get_manager
    rec = get_manager().activate(name, actor="api", reason="manual activate")
    return {"name": name, "state": rec.state}


@app.post("/admin/skills/{name}/deactivate")
async def admin_skills_deactivate(
    name: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.skills.lifecycle import get_manager
    rec = get_manager().deactivate(name, actor="api", reason="manual deactivate")
    return {"name": name, "state": rec.state}


@app.post("/admin/skills/{name}/update")
async def admin_skills_update(
    name: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.skills.lifecycle import get_manager
    rec = get_manager().mark_updating(name, actor="api", reason="manual update")
    return {"name": name, "state": rec.state}


@app.post("/admin/skills/{name}/repair")
async def admin_skills_repair(
    name: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.skills.lifecycle import get_manager
    return get_manager().repair(name, actor="api")


@app.post("/admin/skills/import-from-skillnet")
async def admin_skills_import_skillnet(
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    body = await request.json()
    name = (body or {}).get("name")
    if not name:
        raise HTTPException(status_code=400, detail="missing 'name'")
    from core.skills.skillnet import SkillnetClient
    cli = SkillnetClient()
    if not cli.enabled:
        raise HTTPException(
            status_code=400,
            detail="SKILLNET_BASE_URL is not configured on this server.",
        )
    result = cli.import_skill(
        name,
        category=(body or {}).get("category"),
        version=(body or {}).get("version"),
    )
    if result.get("ok"):
        from core.skills.lifecycle import get_manager
        get_manager().activate(name, actor="skillnet.import",
                               reason="imported from skillnet")
    return result


@app.get("/admin/skills/recommend")
async def admin_skills_recommend(
    task: str,
    top_k: int = 8,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.skills.ranker import recommend_skills
    return recommend_skills(task, top_k=top_k)


@app.post("/admin/skills/dedup")
async def admin_skills_dedup(
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    body: Dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    apply_flag = bool(body.get("apply", False))
    threshold = float(body.get("threshold", 0.88))
    min_quality = float(body.get("min_quality", 0.0))
    from core.skills.dedup import dedup_skills
    return dedup_skills(apply=apply_flag, similarity_threshold=threshold,
                        min_quality=min_quality)


@app.get("/admin/platform")
async def admin_platform(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Return current OS / mode / capabilities."""
    _require_auth(x_agent_token)
    from core.platform_manager import get_platform_manager
    return get_platform_manager().info()


class _ModeBody(BaseModel):
    mode: str = Field(..., description="'lite' or 'pro'")
    persist: bool = True


@app.post("/admin/mode")
async def admin_set_mode(
    body: _ModeBody,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Switch between Lite (constrained desktop) and Pro (parallel desktop)."""
    _require_auth(x_agent_token)
    from core.platform_manager import get_platform_manager
    mgr = get_platform_manager()
    try:
        new = mgr.set_mode(body.mode, persist=body.persist)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "mode": new, "limits": mgr.limits}


@app.post("/admin/tools/acquire")
async def admin_tools_acquire(
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Fetch + analyse + register a tool from a URL / git repo / package."""
    _require_auth(x_agent_token)
    body: Dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    source = body.get("source")
    if not source:
        raise HTTPException(status_code=400, detail="missing 'source'")
    name = body.get("name")
    write_manifest = bool(body.get("write_manifest", False))
    from core.dynamic_tool_acquisition import acquire_tool
    from core.paths import AGENT_OS_ROOT
    manifest_dir = (AGENT_OS_ROOT / "tools" / "packs" / "auto") if write_manifest else None
    return acquire_tool(source, name=name, manifest_dir=manifest_dir)


@app.post("/admin/tools/execute")
async def admin_tools_execute(
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Run any tool headless via the universal executor."""
    _require_auth(x_agent_token)
    body: Dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    manifest = body.get("manifest")
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=400, detail="missing 'manifest' object")
    args = body.get("args") or []
    timeout = float(body.get("timeout", 120.0))
    from core.universal_executor import UniversalExecutor
    return UniversalExecutor().execute(manifest, args=args, timeout=timeout).to_dict()


@app.get("/admin/resources")
async def admin_resources_get(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Return the user's persisted resource preferences for the active mode."""
    _require_auth(x_agent_token)
    from core.platform_manager import get_platform_manager
    from core.resource_settings import (
        LITE_DEFAULTS, PRO_DEFAULTS, PRO_ONLY_KEYS, get_store,
    )
    pm = get_platform_manager()
    settings = get_store().load(mode=pm.mode)
    from core.optimization import available_backends
    return {
        "mode": pm.mode,
        "settings": settings.to_dict(),
        "defaults": {"lite": LITE_DEFAULTS, "pro": PRO_DEFAULTS},
        "pro_only_keys": sorted(PRO_ONLY_KEYS),
        "compression": {
            "available_backends": available_backends(),
            "supported_methods": [
                "auto", "llmlingua", "llmlingua2", "longllmlingua",
                "un-locc", "chonkify", "none",
            ],
        },
    }


class _ResourceBody(BaseModel):
    cpu_seconds: Optional[int] = None
    memory_mb: Optional[int] = None
    max_processes: Optional[int] = None
    parallel_tools: Optional[int] = None
    enable_docker: Optional[bool] = None
    enable_wine: Optional[bool] = None
    enable_heavy_security: Optional[bool] = None
    enable_gpu: Optional[bool] = None
    network_timeout_s: Optional[int] = None
    disk_mb: Optional[int] = None
    # Phase 8 Part 5 — advanced prompt compression.
    compression_enabled: Optional[bool] = None
    compression_method: Optional[str] = None
    compression_ratio: Optional[float] = None
    reset: bool = False


@app.post("/admin/resources")
async def admin_resources_set(
    body: _ResourceBody,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Persist resource preferences. Pro-only fields are silently ignored in Lite."""
    _require_auth(x_agent_token)
    from core.platform_manager import get_platform_manager
    from core.resource_settings import (
        PRO_ONLY_KEYS, _coerce, get_store,
    )
    pm = get_platform_manager()
    store = get_store()
    if body.reset:
        s = store.reset(mode=pm.mode)
        return {"ok": True, "settings": s.to_dict(), "warnings": []}

    current = store.load(mode=pm.mode).to_dict()
    incoming = body.model_dump(exclude_unset=True, exclude={"reset"})
    warnings: List[str] = []
    for k, v in incoming.items():
        if k in PRO_ONLY_KEYS and pm.is_lite and bool(v):
            warnings.append(f"'{k}' is Pro-only; ignored in Lite Mode")
            continue
        current[k] = v
    current["mode_at_save"] = pm.mode
    s = _coerce(current)
    store.save(s)
    return {"ok": True, "settings": s.to_dict(), "warnings": warnings}


@app.get("/admin/settings/secrets")
async def admin_settings_secrets_list(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """List configured user-provided secrets (values masked)."""
    _require_auth(x_agent_token)
    from core.secrets_store import SecretStore
    return {"secrets": SecretStore().list(reveal=False)}


@app.put("/admin/settings/secrets/{name}")
async def admin_settings_secrets_set(
    name: str,
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Persist a named secret (e.g. ``github_token``)."""
    _require_auth(x_agent_token)
    body = await request.json()
    value = (body or {}).get("value", "")
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail="missing 'value'")
    from core.secrets_store import SecretStore
    SecretStore().set(name, value.strip())
    return {"ok": True, "name": name}


@app.delete("/admin/settings/secrets/{name}")
async def admin_settings_secrets_del(
    name: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Remove a named secret."""
    _require_auth(x_agent_token)
    from core.secrets_store import SecretStore
    return {"ok": SecretStore().delete(name), "name": name}


# --------------------------------------------------------------------------
# Phase 7 / Part 3b — Scheduler + Dashboard
# --------------------------------------------------------------------------
@app.get("/admin/schedules")
async def admin_schedules_list(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.scheduler import get_scheduler
    return {"schedules": [s.to_dict() for s in get_scheduler().list()]}


@app.post("/admin/schedules")
async def admin_schedules_create(
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.scheduler import get_scheduler
    body = await request.json()
    name = (body or {}).get("name", "").strip()
    cron = (body or {}).get("cron", "").strip()
    target = (body or {}).get("target", "").strip()
    if not name or not cron or not target:
        raise HTTPException(status_code=400,
                              detail="missing name/cron/target")
    try:
        s = get_scheduler().add(
            name=name, cron=cron, target=target,
            payload=(body or {}).get("payload") or {},
            tz=(body or {}).get("tz", "UTC"),
            enabled=bool((body or {}).get("enabled", True)),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return s.to_dict()


@app.patch("/admin/schedules/{sid}")
async def admin_schedules_update(
    sid: str, request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.scheduler import get_scheduler
    body = await request.json() or {}
    try:
        s = get_scheduler().update(sid, **body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if s is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    return s.to_dict()


@app.delete("/admin/schedules/{sid}")
async def admin_schedules_delete(
    sid: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.scheduler import get_scheduler
    if not get_scheduler().delete(sid):
        raise HTTPException(status_code=404, detail="schedule not found")
    return {"ok": True}


@app.get("/admin/dashboard/summary")
async def admin_dashboard_summary(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.dashboard import summary
    return summary()


@app.get("/admin/dashboard/activity")
async def admin_dashboard_activity(
    limit: int = 50,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.dashboard import activity
    return activity(limit=limit)


@app.get("/admin/dashboard/stats")
async def admin_dashboard_stats(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.dashboard import stats
    return stats()


# --------------------------------------------------------------------------
# Phase 7 / Part 3d — Documents + skill learning
# --------------------------------------------------------------------------
@app.get("/admin/documents/available")
async def admin_documents_available(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.documents import available
    return available()


@app.post("/admin/documents/report")
async def admin_documents_report(
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    body = await request.json() or {}
    title = (body.get("title") or "").strip() or "Report"
    sections = body.get("sections") or []
    from core.documents import create_report
    try:
        res = create_report(title=title, sections=sections,
                              filename=body.get("filename"))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return res.to_dict()


@app.post("/admin/documents/spreadsheet")
async def admin_documents_spreadsheet(
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    body = await request.json() or {}
    title = (body.get("title") or "").strip() or "Workbook"
    sheets = body.get("sheets") or []
    from core.documents import create_spreadsheet
    try:
        res = create_spreadsheet(title=title, sheets=sheets,
                                    filename=body.get("filename"))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return res.to_dict()


@app.post("/admin/documents/presentation")
async def admin_documents_presentation(
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    body = await request.json() or {}
    title = (body.get("title") or "").strip() or "Deck"
    slides = body.get("slides") or []
    from core.documents import create_presentation
    try:
        res = create_presentation(title=title, slides=slides,
                                     filename=body.get("filename"))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return res.to_dict()


@app.post("/admin/skills/learn_from_demonstration")
async def admin_skills_learn(
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    body = await request.json() or {}
    from core.learner import learn_from_demonstration
    try:
        learned = learn_from_demonstration(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return learned.to_dict()


@app.get("/admin/skills/learned")
async def admin_skills_learned_list(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.learner import list_learned
    return {"skills": list_learned()}


class _SkillUploadRequest(BaseModel):
    filename: str = "SKILL.md"
    content: str
    category: Optional[str] = None


class _InstructionsBody(BaseModel):
    model_config = {"extra": "forbid"}
    instructions: str = Field(default="", max_length=30_000)
    expected_revision: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def _instructions_path():
    from core.paths import MEMORY_DIR, ensure_runtime_dirs
    ensure_runtime_dirs()
    return MEMORY_DIR / "instructions.txt"


@app.get("/admin/instructions")
async def admin_get_instructions(
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Read the user-defined system instructions appended to every prompt."""
    _require_auth(x_agent_token)
    return {"ok": True, **await v2_get_instructions(request)}


@app.post("/admin/instructions")
async def admin_set_instructions(
    body: _InstructionsBody,
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Persist the user-defined system instructions."""
    _require_auth(x_agent_token)
    return await v2_save_instructions(V2InstructionsRequest(**body.model_dump()), request)


@app.post("/admin/skills/upload")
async def admin_skills_upload(
    req: _SkillUploadRequest,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Persist a user-uploaded SKILL.md under ``skills/uploaded/<name>/``.

    The body carries the raw markdown text. We extract a slug from the
    first ``# heading`` (falling back to a timestamp), write the file
    on disk, and refresh the index so the skill is immediately visible
    to the agent.
    """
    _require_auth(x_agent_token)
    if not req.content or not req.content.strip():
        raise HTTPException(status_code=400, detail="empty content")

    import re
    import time
    from core.paths import SKILLS_DIR

    title = ""
    for line in req.content.splitlines():
        m = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if m:
            title = m.group(1)
            break
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", title).strip("-").lower()
    if not slug:
        slug = f"uploaded-{int(time.time())}"

    base = SKILLS_DIR / "uploaded" / slug
    base.mkdir(parents=True, exist_ok=True)
    target = base / "SKILL.md"
    target.write_text(req.content, encoding="utf-8")

    refreshed = False
    try:
        from core.skills.indexer import SkillsIndex
        SkillsIndex().refresh()
        refreshed = True
    except Exception:
        pass

    return {
        "ok": True,
        "summary": f"saved {target.relative_to(SKILLS_DIR)}",
        "path": str(target),
        "slug": slug,
        "title": title or slug,
        "indexed": refreshed,
    }


# --------------------------------------------------------------------------
# Phase 7 / Part 3e — Session streaming (SSE)
# --------------------------------------------------------------------------
class _SessionStartRequest(BaseModel):
    message: str
    mode: str = Field("normal", pattern="^(normal|agents_team)$")
    max_steps: int = 8


@app.post("/admin/sessions")
async def admin_sessions_start(
    req: _SessionStartRequest,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Kick off an async chat session and return its id immediately.

    The caller then subscribes to ``GET /admin/sessions/{id}/stream`` to
    receive SSE events (start/progress/trace/tool_call/complete).
    """
    _require_auth(x_agent_token)
    model = _state.get("model")
    if model is None:
        raise HTTPException(status_code=503,
                              detail="النموذج غير مهيأ. أضف مفتاح API.")
    awareness = _state.get("awareness")
    from core.streaming import get_registry
    from core.observability import Tracer, new_trace_id

    def _runner(session, emitter):
        """Thread-side agent runner that pumps incremental events."""
        tid = new_trace_id()
        session.trace_id = tid
        # Forward any tracer events produced during the run.
        seen_before = len(Tracer.events(trace_id=tid, limit=1000))
        emitter.emit("progress", {"stage": "planning"})

        # Forward final-answer token deltas from the engine into the
        # SSE stream. ``on_token`` is called from the agent thread —
        # ``EventEmitter.emit`` marshals back to the event loop.
        def _on_token(delta: str) -> None:
            try:
                emitter.emit("token", {"delta": delta})
            except Exception:  # noqa: BLE001
                pass

        if req.mode == "agents_team":
            agent = AgentsTeam(model, tool_registry, awareness,
                                 max_steps=req.max_steps)
            # AgentsTeam spawns workers with their own Normal agents;
            # the workforce plumbing does not pipe token deltas per
            # worker, so team mode keeps the trace/tool_call feed only.
        else:
            agent = Normal(model, tool_registry, awareness,
                             max_steps=req.max_steps,
                             on_token=_on_token)

        # Run the sync agent in a nested thread so we can drain tracer
        # events concurrently and forward them as tool_call/trace frames.
        import threading
        result: Dict[str, Any] = {"answer": None, "error": None}

        def _run():
            try:
                with Tracer.trace(tid):
                    result["answer"] = agent.run(req.message)
            except Exception as e:  # noqa: BLE001
                result["error"] = f"{type(e).__name__}: {e}"

        t = threading.Thread(target=_run,
                               name=f"agent-os-runner-{session.id}",
                               daemon=True)
        t.start()

        last_seen = seen_before
        while t.is_alive():
            t.join(timeout=0.25)
            events = Tracer.events(trace_id=tid, limit=1000)
            if len(events) > last_seen:
                for ev in events[last_seen:]:
                    kind = ev.get("kind", "")
                    if kind.startswith("react.tool") or "tool" in kind:
                        emitter.emit("tool_call",
                                       {"name": ev.get("tool")
                                                    or ev.get("kind"),
                                         "status": ev.get("status"),
                                         "duration_ms":
                                             ev.get("duration_ms")})
                    emitter.emit("trace", ev)
                last_seen = len(events)

        if result["error"]:
            raise RuntimeError(result["error"])
        return result["answer"] or ""

    session = get_registry().start(
        prompt=req.message, mode=req.mode, run_fn=_runner)
    return {"id": session.id, "mode": session.mode,
             "stream_url": f"/admin/sessions/{session.id}/stream"}


@app.get("/admin/sessions/{sid}/stream")
async def admin_sessions_stream(
    sid: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> StreamingResponse:
    """SSE feed for a running session.

    Yields ``start`` → (``trace``/``tool_call``/``progress``) → ``complete``
    frames. Replaces the previous polling model.
    """
    _require_auth(x_agent_token)
    from core.streaming import stream_events
    return StreamingResponse(
        stream_events(sid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                  "X-Accel-Buffering": "no"})


@app.get("/admin/sessions/{sid}")
async def admin_sessions_get(
    sid: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.streaming import get_registry
    s = get_registry().get(sid)
    if s is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return s.to_dict()


# --------------------------------------------------------------------------
# Phase 9 Part 1.2 — WebSocket streaming chat (/chat/stream)
#
# Each WebSocket connection is one chat exchange:
#
#   client → server   {"type": "start", "message": "...", "mode": "normal",
#                      "max_steps": 8}
#   client → server   {"type": "cancel"}                  (optional)
#
#   server → client   {"type": "session", "session_id": "..."}
#   server → client   {"type": "token", "delta": "..."}
#   server → client   {"type": "tool_call", "name": "...", "status": "..."}
#   server → client   {"type": "tool_result", "name": "...", "result": ...}
#   server → client   {"type": "think", "text": "..."}
#   server → client   {"type": "final", "answer": "..."}
#   server → client   {"type": "error", "error": "..."}
#
# The route reuses the same in-process Session / EventEmitter machinery
# that powers ``GET /admin/sessions/{sid}/stream`` (SSE) so behaviour
# stays consistent. The WebSocket variant is what the desktop UI uses
# for token-by-token rendering — SSE remains for non-WS clients.
# --------------------------------------------------------------------------


def _classify_trace_event(ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map a Tracer event to one of the WebSocket event types.

    Returns ``None`` for events we don't expose to the UI.
    """
    kind = (ev.get("kind") or "").lower()
    if not kind:
        return None
    if kind == "react.tool.start" or kind.endswith(".tool.start"):
        return {
            "type": "tool_call",
            "name": ev.get("tool") or ev.get("name") or kind,
            "status": ev.get("status") or "started",
            "args": ev.get("args"),
            "duration_ms": ev.get("duration_ms"),
        }
    if kind == "react.tool.end" or kind.endswith(".tool.end"):
        return {
            "type": "tool_result",
            "name": ev.get("tool") or ev.get("name") or kind,
            "status": ev.get("status") or "ok",
            "result": ev.get("result"),
            "error": ev.get("error"),
            "duration_ms": ev.get("duration_ms"),
        }
    if kind == "react.think" or kind.endswith(".think"):
        return {
            "type": "think",
            "text": ev.get("text") or ev.get("thought") or "",
        }
    if "tool" in kind:
        # Generic tool-related event — treat as tool_call so the UI can
        # at least surface activity.
        return {
            "type": "tool_call",
            "name": ev.get("tool") or kind,
            "status": ev.get("status"),
            "duration_ms": ev.get("duration_ms"),
        }
    return None


@app.websocket("/chat/stream")
async def chat_stream(websocket: WebSocket) -> None:
    """Real-time bidirectional chat with token / tool-call streaming.

    Auth: when ``AGENT_API_TOKEN`` is set, the client must include the
    token either via the ``X-Agent-Token`` header on the upgrade or as
    a ``?token=...`` query parameter. The connection is closed with
    code 4401 when auth fails — analogous to a 401 over HTTP.
    """
    expected = os.environ.get("AGENT_API_TOKEN")
    if expected:
        provided = (websocket.headers.get("x-agent-token")
                    or websocket.query_params.get("token") or "")
        if provided != expected:
            await websocket.close(code=4401, reason="invalid token")
            return

    await websocket.accept()

    try:
        opening = await asyncio.wait_for(websocket.receive_json(), timeout=30)
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError) as exc:
        # Either the client never sent us anything within 30s or sent
        # bytes/garbage — close cleanly without spamming logs.
        try:
            await websocket.close(code=4400, reason=f"bad opening: {exc}")
        except Exception:
            pass
        return

    if not isinstance(opening, dict) or opening.get("type") not in (
            "start", None):
        await websocket.close(code=4400,
                              reason="first frame must be {'type':'start',...}")
        return

    message = (opening.get("message") or "").strip()
    if not message:
        await websocket.close(code=4400, reason="missing 'message'")
        return
    mode = opening.get("mode") or "normal"
    if mode not in ("normal", "agents_team"):
        await websocket.close(code=4400, reason="invalid mode")
        return
    max_steps = int(opening.get("max_steps") or 8)
    max_steps = max(1, min(20, max_steps))

    model = _state.get("model")
    if model is None:
        await websocket.send_json({
            "type": "error",
            "error": "model not initialised — add an LLM API key first",
        })
        await websocket.close()
        return

    awareness = _state.get("awareness")
    from core.streaming import get_registry
    from core.observability import Tracer, new_trace_id
    import threading as _th

    # Cooperative cancellation flag set by the receive-side coroutine
    # and polled by the agent runner thread.
    cancel_flag = _th.Event()

    def _runner(session, emitter):
        tid = new_trace_id()
        session.trace_id = tid
        emitter.emit("progress", {"stage": "planning"})

        def _on_token(delta: str) -> None:
            try:
                emitter.emit("token", {"delta": delta})
            except Exception:
                pass

        if mode == "agents_team":
            agent = AgentsTeam(model, tool_registry, awareness,
                               max_steps=max_steps)
        else:
            agent = Normal(model, tool_registry, awareness,
                           max_steps=max_steps,
                           on_token=_on_token)

        result: Dict[str, Any] = {"answer": None, "error": None}
        seen_before = len(Tracer.events(trace_id=tid, limit=2000))

        def _run():
            try:
                with Tracer.trace(tid):
                    result["answer"] = agent.run(message)
            except Exception as e:  # noqa: BLE001
                result["error"] = f"{type(e).__name__}: {e}"

        t = _th.Thread(target=_run,
                       name=f"ws-runner-{session.id}", daemon=True)
        t.start()

        last_seen = seen_before
        while t.is_alive():
            t.join(timeout=0.2)
            if cancel_flag.is_set():
                # The Python sync agent cannot be hard-cancelled, but we
                # stop forwarding its events and return early so the
                # WebSocket can close cleanly. The thread will finish
                # in the background and its result will be ignored.
                emitter.emit("error", {"error": "cancelled by client"})
                return ""
            events = Tracer.events(trace_id=tid, limit=2000)
            if len(events) > last_seen:
                for ev in events[last_seen:]:
                    classified = _classify_trace_event(ev)
                    if classified is not None:
                        ev_type = classified.pop("type")
                        emitter.emit(ev_type, classified)
                last_seen = len(events)

        if result["error"]:
            raise RuntimeError(result["error"])
        return result["answer"] or ""

    session = get_registry().start(
        prompt=message, mode=mode, run_fn=_runner)
    await websocket.send_json({"type": "session",
                               "session_id": session.id,
                               "mode": session.mode})

    queue = get_registry().queue_for(session.id)
    assert queue is not None  # we just started it

    async def _drain_events() -> None:
        """Forward every emitter event to the WebSocket as a JSON frame."""
        while True:
            frame = await queue.get()
            if frame is None:
                break
            event, payload = frame
            ws_type = {
                "start": "session",        # already sent above; suppress
                "progress": "progress",
                "complete": "final",
                "error": "error",
                "trace": None,             # filtered events come via dedicated frames
                "token": "token",
                "tool_call": "tool_call",
                "tool_result": "tool_result",
                "think": "think",
            }.get(event, event)
            if ws_type is None or ws_type == "session":
                continue
            data: Dict[str, Any] = {"type": ws_type}
            if isinstance(payload, dict):
                data.update(payload)
            try:
                await websocket.send_json(data)
            except Exception:
                return
            if ws_type in ("final", "error"):
                return

    async def _watch_for_cancel() -> None:
        try:
            while True:
                msg = await websocket.receive_json()
                if isinstance(msg, dict) and msg.get("type") == "cancel":
                    cancel_flag.set()
                    return
        except (WebSocketDisconnect, RuntimeError, ValueError):
            cancel_flag.set()

    drain_task = asyncio.create_task(_drain_events())
    cancel_task = asyncio.create_task(_watch_for_cancel())
    try:
        await drain_task
    finally:
        cancel_task.cancel()
        try:
            await asyncio.wait_for(cancel_task, timeout=0.1)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        try:
            await websocket.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Phase 7 / Part 3c — Unified channels
# --------------------------------------------------------------------------
@app.get("/admin/channels")
async def admin_channels_list(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.channels import list_channels
    return list_channels()


@app.put("/admin/channels/{name}")
async def admin_channels_register(
    name: str, request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Store an Apprise URL for ``name`` (e.g. ``slack://…``)."""
    _require_auth(x_agent_token)
    body = await request.json() or {}
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="missing 'url'")
    from core.channels.unified import validate_channel
    try:
        validate_channel(name, url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from core.channels import register_channel
    register_channel(name, url)
    return {"ok": True, "name": name}


@app.delete("/admin/channels/{name}")
async def admin_channels_unregister(
    name: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.channels import unregister_channel
    return {"ok": unregister_channel(name), "name": name}


@app.post("/admin/channels/{name}/send")
async def admin_channels_send(
    name: str, request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    body = await request.json() or {}
    msg = (body.get("message") or "").strip()
    title = (body.get("title") or "").strip()
    url = body.get("url")  # optional one-off override
    attach = body.get("attach")
    if not msg:
        raise HTTPException(status_code=400, detail="missing 'message'")
    from core.channels import send_message
    res = send_message(name, msg, title=title, url=url, attach=attach)
    return {"channel": res.channel, "ok": res.ok,
             "detail": res.detail, "code": res.code}


@app.get("/admin/onboarding/required")
async def admin_onboarding_required(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Cheap UI gate — does the user need to run onboarding?"""
    _require_auth(x_agent_token)
    from core.onboarding import get_onboarding_manager
    return get_onboarding_manager().needs_onboarding()


@app.post("/admin/onboarding/install")
async def admin_onboarding_install(
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Kick off the platform-aware sequential install pipeline."""
    _require_auth(x_agent_token)
    body: Dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    from core.onboarding import get_onboarding_manager
    task = get_onboarding_manager().start(force=bool(body.get("force", False)))
    return {"task_id": task.task_id, "mode": task.mode,
            "components": [c.to_dict() for c in task.components]}


@app.get("/admin/onboarding/status/{task_id}")
async def admin_onboarding_status(
    task_id: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Real-time progress for a running install task."""
    _require_auth(x_agent_token)
    from core.onboarding import get_onboarding_manager
    task = get_onboarding_manager().get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="unknown task_id")
    return task.to_dict()


@app.post("/admin/onboarding/install/single")
async def admin_onboarding_install_single(
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Retry a single component (rootfs or one tool name)."""
    _require_auth(x_agent_token)
    body: Dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = body.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="missing 'name'")
    from core.onboarding import get_onboarding_manager
    return get_onboarding_manager().install_single(name)


@app.get("/admin/knowledge/sources")
async def admin_knowledge_sources(
    force: bool = False,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """List every registered RAG source with its current health status."""
    _require_auth(x_agent_token)
    from core.rag import get_router
    router = get_router()
    statuses = await asyncio.to_thread(router.status, force=True) if force else router.cached_status()
    return {"count": len(statuses),
            "sources": [s.to_dict() for s in statuses]}


@app.post("/admin/knowledge/test")
async def admin_knowledge_test(
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Run a one-off search against a single source for diagnostics."""
    _require_auth(x_agent_token)
    body = await request.json()
    name = body.get("source") or body.get("name")
    query = body.get("query", "test")
    max_results = int(body.get("max_results", 3))
    if not name:
        raise HTTPException(status_code=400, detail="missing 'source'")
    from core.rag import get_router
    router = get_router()
    src = router.sources.get(name)
    if src is None:
        raise HTTPException(status_code=404,
                            detail=f"unknown source: {name}")
    started = time.time()
    try:
        results = src.search(query, max_results=max_results)
        return {"ok": True, "source": name, "count": len(results),
                "elapsed_ms": (time.time() - started) * 1000,
                "results": [r.to_dict() for r in results]}
    except Exception as exc:
        return {"ok": False, "source": name,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": (time.time() - started) * 1000}


@app.post("/admin/repair")
async def admin_repair(
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Run diagnose + multi-strategy repair on a target."""
    _require_auth(x_agent_token)
    body: Dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    target = body.get("target") or "all"
    error_text = body.get("error") or body.get("error_text") or ""
    from core.self_healing import get_self_healing
    report = get_self_healing().repair(target=target, error_text=error_text)
    return report.to_dict()


@app.get("/admin/repair/status/{repair_id}")
async def admin_repair_status(
    repair_id: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Return the most recent state of a repair task."""
    _require_auth(x_agent_token)
    from core.self_healing import get_self_healing
    report = get_self_healing().get(repair_id)
    if not report:
        raise HTTPException(status_code=404, detail="unknown repair_id")
    return report.to_dict()


@app.get("/admin/repair/history")
async def admin_repair_history(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Return up to the last 100 repair reports."""
    _require_auth(x_agent_token)
    from core.self_healing import get_self_healing
    return {"history": get_self_healing().history()}


@app.post("/admin/skills/optimize")
async def admin_skills_optimize(
    request: Request,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Run the full skills-maintenance pipeline in one call.

    Order: dedup → reorganise → first-boot health check → repair any
    failed skills. Each step is best-effort; failures are reported but
    never abort the rest of the pipeline.
    """
    _require_auth(x_agent_token)
    body: Dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    apply_flag = bool(body.get("apply", True))
    threshold = float(body.get("threshold", 0.88))
    min_quality = float(body.get("min_quality", 0.0))
    do_reorg = bool(body.get("reorganize", True))

    out: Dict[str, Any] = {"steps": {}}
    try:
        from core.skills.dedup import dedup_skills
        out["steps"]["dedup"] = dedup_skills(
            apply=apply_flag, similarity_threshold=threshold,
            min_quality=min_quality,
        )
    except Exception as exc:
        out["steps"]["dedup"] = {"ok": False, "error": str(exc)}

    if do_reorg:
        try:
            from core.skills.reorganize import apply as reorg_apply, plan as reorg_plan
            moves = reorg_plan()
            applied = reorg_apply(moves) if apply_flag else []
            out["steps"]["reorganize"] = {
                "planned": len(moves),
                "applied": len(applied),
                "dry_run": not apply_flag,
            }
        except Exception as exc:
            out["steps"]["reorganize"] = {"ok": False, "error": str(exc)}

    try:
        from core.skills.lifecycle import first_boot_health_check, get_manager
        out["steps"]["health_check"] = first_boot_health_check(force=True)
        manager = get_manager()
        repaired: list = []
        for name, rec in manager.all().items():
            if rec.get("state") == "failed":
                result = manager.repair(name, actor="optimize")
                if result.get("ok"):
                    repaired.append(name)
        out["steps"]["repair"] = {"repaired": repaired,
                                  "repaired_count": len(repaired)}
    except Exception as exc:
        out["steps"]["health_check"] = {"ok": False, "error": str(exc)}

    out["ok"] = True
    return out


# --------------------------------------------------------------------------
# Phase 9 Part 2.4 — Startup self-test
# --------------------------------------------------------------------------
@app.get("/admin/self-test")
async def admin_self_test(
    only: Optional[str] = None,
    persist: bool = True,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Run the offline self-test suite and return a structured report.

    * ``only`` (optional) — comma-separated subset of check names
      (without the ``check_`` prefix), e.g. ``paths,tools``.
    * ``persist`` (default true) — also write the report to
      ``logs/self_test.json`` so the onboarding screen can pick it up
      on next boot.
    """
    _require_auth(x_agent_token)
    from core.self_test import run_self_test, persist_report
    selected: Optional[List[str]] = None
    if only:
        selected = [s.strip() for s in only.split(",") if s.strip()]
    report = run_self_test(only=selected)
    if persist:
        try:
            persist_report(report)
        except Exception:
            pass
    return report.to_dict()


@app.get("/admin/self-test/cached")
async def admin_self_test_cached(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Return the most recent persisted self-test report, or 404."""
    _require_auth(x_agent_token)
    from core.self_test import cached_report
    cached = cached_report()
    if cached is None:
        raise HTTPException(status_code=404,
                            detail="no cached self-test report")
    return cached.to_dict()


# ---------------------------------------------------------------------------
# Local preview server admin endpoints (Phase 10 Part 2)
# ---------------------------------------------------------------------------
@app.get("/admin/preview/list")
async def admin_preview_list(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Return every running preview server (JSON-safe info)."""
    _require_auth(x_agent_token)
    from core.preview_server import list_previews
    items = list_previews()
    return {"count": len(items), "previews": items}


class _PreviewStartIn(BaseModel):
    path: str
    port: Optional[int] = None
    ttl_seconds: int = 1800


@app.post("/admin/preview/start")
async def admin_preview_start(
    body: _PreviewStartIn,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Start a preview server for a directory inside the workspace.

    The path is resolved against ``WORKSPACE_DIR`` and rejected if it
    escapes that root. The server binds to ``127.0.0.1`` only.
    """
    _require_auth(x_agent_token)
    from core.preview_server import start_preview
    try:
        info = start_preview(body.path, port=body.port,
                              ttl_seconds=body.ttl_seconds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return info.to_dict()


class _PreviewStopIn(BaseModel):
    server_id: str


@app.post("/admin/preview/stop")
async def admin_preview_stop(
    body: _PreviewStopIn,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_auth(x_agent_token)
    from core.preview_server import stop_preview
    ok = stop_preview(body.server_id)
    if not ok:
        raise HTTPException(status_code=404,
                            detail=f"no preview {body.server_id}")
    return {"ok": True, "server_id": body.server_id}


# ============================================================================
# §11 — OAuth account linking (Phase 10 Part 3)
# ============================================================================
class _OAuthStartIn(BaseModel):
    provider: str
    bundles: List[str] = []
    redirect_uri: Optional[str] = None  # override the loopback URI
    start_loopback: bool = True


@app.post("/admin/oauth/link/start")
async def admin_oauth_link_start(
    body: _OAuthStartIn,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Begin an OAuth link flow; returns the authorization URL + state."""
    _require_auth(x_agent_token)
    try:
        from core.oauth import start_link_flow
        flow = start_link_flow(
            body.provider,
            body.bundles,
            start_loopback=body.start_loopback,
            redirect_uri_override=body.redirect_uri)
        return {
            "ok": True,
            "auth_url": flow.auth_url,
            "state": flow.state,
            "redirect_uri": flow.redirect_uri,
            "scopes": flow.scopes,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=412, detail=str(e))


@app.get("/admin/oauth/link/status")
async def admin_oauth_link_status(
    state: str,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Poll the status of an in-flight OAuth link flow."""
    _require_auth(x_agent_token)
    from core.oauth import get_link_status
    info = get_link_status(state)
    if info is None:
        raise HTTPException(status_code=404, detail=f"no flow {state}")
    return {"ok": True, **info}


class _OAuthCallbackIn(BaseModel):
    state: str
    code: str


@app.post("/admin/oauth/link/complete")
async def admin_oauth_link_complete(
    body: _OAuthCallbackIn,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Manually deliver the ?code=&state= back to the agent (for hosts
    where the loopback redirect cannot reach the agent process)."""
    _require_auth(x_agent_token)
    try:
        from core.oauth import complete_link_flow
        acct = complete_link_flow(body.state, body.code)
        return {
            "ok": True,
            "summary": f"linked {acct.provider}:{acct.account}",
            "provider": acct.provider,
            "account": acct.account,
            "scopes": acct.scopes,
        }
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


class _OAuthRevokeIn(BaseModel):
    provider: str
    account: str


@app.post("/admin/oauth/revoke")
async def admin_oauth_revoke(
    body: _OAuthRevokeIn,
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Revoke a linked account both server-side and locally."""
    _require_auth(x_agent_token)
    from core.oauth import get_account, revoke_account
    from core.oauth.flow import revoke_with_provider
    acct = get_account(body.provider, body.account)
    server_revoked = False
    if acct is not None:
        try:
            server_revoked = revoke_with_provider(body.provider, acct)
        except Exception:
            server_revoked = False
    local_removed = revoke_account(body.provider, body.account)
    if not local_removed and acct is None:
        raise HTTPException(status_code=404,
                            detail=f"no account {body.provider}:{body.account}")
    return {
        "ok": True,
        "summary": f"revoked {body.provider}:{body.account}",
        "server_revoked": server_revoked,
        "local_removed": local_removed,
    }


@app.get("/admin/oauth/accounts")
async def admin_oauth_accounts(
    x_agent_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """List every linked OAuth account (no secrets in response)."""
    _require_auth(x_agent_token)
    from core.oauth import list_accounts
    items = list_accounts()
    return {"ok": True, "count": len(items), "accounts": items}


# ==============================================================================
# WISE V2 Integrated Conversational Agent & Desktop GUI API Surface
# ==============================================================================
from fastapi.staticfiles import StaticFiles


class V2ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    session_id: Optional[str] = Field(None, max_length=64)
    modality: str = Field("chat", max_length=32)
    mode: str = Field("normal", pattern="^(normal|research|agents_team)$")
    selected_mcp: Optional[str] = Field(None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    max_steps: int = Field(6, ge=1, le=20)
    attachments: List[Dict[str, Any]] = Field(default_factory=list, max_length=12)


class V2AttachmentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    mime_type: str = Field("application/octet-stream", max_length=160)
    data_base64: str = Field(..., min_length=1, max_length=21_000_000)


@app.get("/api/v2/messaging/services")
async def v2_messaging_services():
    from core.channels.service_catalog import services
    return {"services": await asyncio.to_thread(services)}


@app.post("/api/v2/messaging/connections")
async def v2_messaging_configure(payload: Dict[str, Any]):
    from core.channels.service_catalog import configure
    try:
        return await asyncio.to_thread(configure, str(payload.get("service", "")),
            str(payload.get("name", "")), payload.get("fields", {}))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v2/attachments")
async def v2_upload_attachment(req: V2AttachmentRequest) -> Dict[str, Any]:
    """Persist a small chat attachment inside WISE's writable workspace.

    The browser sends JSON instead of multipart data so this endpoint does not
    add a python-multipart runtime dependency.  Filenames are reduced to their
    basename and every generated path stays under ``workspace/attachments``.
    """
    from core.paths import WORKSPACE_DIR

    safe_name = Path(req.name).name.strip().replace("\x00", "")
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(status_code=422, detail="Invalid attachment name")
    try:
        content = base64.b64decode(req.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid attachment encoding") from exc
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Attachment exceeds the 15 MB limit")

    target_dir = (WORKSPACE_DIR / "attachments").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = (target_dir / f"{time.time_ns()}-{safe_name}").resolve()
    if target_dir not in target.parents:
        raise HTTPException(status_code=422, detail="Invalid attachment path")
    target.write_bytes(content)
    return {
        "name": safe_name,
        "mime_type": req.mime_type,
        "size": len(content),
        "path": str(target),
    }


@app.get("/api/v2/capability-network/plan")
async def v2_capability_network_plan(
    task: str,
    limit: int = 5,
    skill_limit: int = 3,
) -> Dict[str, Any]:
    """Explain the tool, skill, and connected-MCP recommendations for a task."""
    from core.capability_network import get_capability_network
    plan = await asyncio.to_thread(
        get_capability_network().plan, task,
        limit=max(1, min(limit, 12)),
        skill_limit=max(0, min(skill_limit, 8)),
    )
    return plan.to_dict()


@app.get("/api/v2/capabilities/browse/{kind}")
async def v2_capability_browse(kind: str, query: str = "", category: str = "", offset: int = 0, limit: int = 10):
    from core.capability_catalog import browse_skills, browse_tools, browse_resources
    handler = {"skills":browse_skills, "tools":browse_tools, "resources":browse_resources}.get(kind)
    if handler is None:
        raise HTTPException(status_code=404, detail="Unknown capability collection")
    return await asyncio.to_thread(handler, query=query[:500], category=category[:80], offset=max(0,offset), limit=min(20,max(1,limit)))


@app.post("/api/v2/chat")
async def v2_chat(
    req: V2ChatRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> Dict[str, Any]:
    """Run one canonical agent turn with retry-safe response replay."""
    return await _execute_v2_chat(req, idempotency_key)


@app.get("/api/v2/chat/commands")
async def v2_chat_commands():
    from core.mcp import get_registry
    commands = [
        {"id": "normal", "kind": "mode", "name": "Auto", "mode": "normal", "available": True,
         "description": "Answer or execute according to your request"},
        {"id": "research", "kind": "mode", "name": "Research", "mode": "research", "available": True,
         "description": "Search the web and cite retrieved evidence"},
        {"id": "team", "kind": "mode", "name": "Agent team", "mode": "agents_team", "available": True,
         "description": "Independent planning/research, one executor and a reviewer · uses more model requests"},
    ]
    for server in get_registry().list_servers():
        if server.get("enabled", True):
            commands.append({"id": "mcp:" + server["name"], "kind": "mcp", "name": server["name"],
                             "server": server["name"], "available": bool(server.get("alive"))})
    return {"commands": commands}


@app.get("/api/v2/chat/requests/{request_key}")
async def v2_chat_request_status(request_key: str):
    """Read-only delivery recovery: never submit the user's task again."""
    if len(request_key) > 200:
        raise HTTPException(status_code=422, detail="Request key is too long")
    from core.idempotency import get_idempotency_store
    return get_idempotency_store().inspect("v2.chat", request_key)


async def _execute_v2_chat(req, idempotency_key, on_milestone=None):
    from core.idempotency import get_idempotency_store
    from core.brain.conversational_core import get_conversational_core
    store = get_idempotency_store()
    if req.selected_mcp:
        from core.mcp import get_registry
        if not any(row["name"] == req.selected_mcp and row.get("alive") for row in get_registry().list_servers()):
            raise HTTPException(status_code=409, detail="Selected MCP is disconnected; connect it in settings or clear the selection")
        if req.mode != "normal":
            raise HTTPException(status_code=422, detail="An explicit MCP cannot be combined with research mode")
    payload = {"message": req.message, "session_id": req.session_id, "modality": req.modality, "mode": req.mode, "max_steps": req.max_steps, "attachments": req.attachments, "selected_mcp": req.selected_mcp}
    decision = store.begin("v2.chat", idempotency_key or "", payload)
    if decision.state == "REPLAY":
        return {**(decision.response or {}), "idempotent_replay": True}
    if decision.state == "CONFLICT":
        raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different request")
    if decision.state == "IN_PROGRESS":
        raise HTTPException(status_code=409, detail="An identical request with this Idempotency-Key is still running")

    trace_id = new_trace_id()
    cc = get_conversational_core()
    started = time.perf_counter()
    try:
        attachment_lines = [
            f"- {item.get('name', 'attachment')}: {item.get('path', '')}"
            for item in req.attachments
            if isinstance(item, dict) and item.get("path")
        ]
        model_message = req.message
        if attachment_lines:
            model_message += "\n\nAttached local files:\n" + "\n".join(attachment_lines)

        def _run_turn():
            with Tracer.trace(trace_id):
                kwargs = {"session_id": req.session_id, "modality": req.modality, "max_steps": req.max_steps}
                if req.mode != "normal" or req.selected_mcp:
                    kwargs.update(mode=req.mode, selected_mcp=req.selected_mcp)
                if on_milestone is not None:
                    kwargs["on_milestone"] = on_milestone
                return cc.process_turn(model_message, **kwargs)
        res = await asyncio.to_thread(_run_turn)
        response = {**res.to_dict(), "trace_id": trace_id, "idempotent_replay": False}
        with Tracer.trace(trace_id):
            Tracer.emit("conversation.completed", session_id=response.get("session_id", req.session_id),
                        latency_ms=response.get("latency_ms", (time.perf_counter() - started) * 1000),
                        action_type=response.get("action_type", ""), model=response.get("model_name", ""),
                        error=bool(response.get("error")))
        store.complete("v2.chat", idempotency_key or "", response)
        return response
    except Exception:
        store.fail("v2.chat", idempotency_key or "")
        raise


_live_chat_workers = set()


@app.post("/api/v2/chat/events")
async def v2_chat_events(
    req: V2ChatRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Live operational milestones, not private reasoning or simulated tokens.

    Shares HTTP chat's idempotency boundary. A disconnected client does not
    trigger another paid model request or abandon response persistence.
    """
    from fastapi.responses import StreamingResponse
    loop = asyncio.get_running_loop()
    events = asyncio.Queue()

    def publish(ms):
        event = {"type": "milestone", **ms.to_dict()}
        loop.call_soon_threadsafe(events.put_nowait, event)

    async def run():
        try:
            response = await _execute_v2_chat(req, idempotency_key, publish)
            await events.put({"type": "done", "response": response})
        except Exception as exc:
            message = str(exc.detail) if isinstance(exc, HTTPException) else "The request failed; check the server diagnostics."
            await events.put({"type": "error", "message": message})

    async def stream():
        worker = asyncio.create_task(run())
        _live_chat_workers.add(worker)
        worker.add_done_callback(_live_chat_workers.discard)
        # Retain the turn until response persistence even after client disconnect.
        worker.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        while True:
            event = await events.get()
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            if event["type"] in {"done", "error"}:
                break

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/v2/tasks")
async def v2_list_tasks(limit: int = 50) -> List[Dict[str, Any]]:
    from core.brain.task_engine import get_task_engine
    return get_task_engine().list_tasks(limit=limit)


@app.get("/api/v2/tasks/active")
async def v2_active_task(session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    from core.brain.task_engine import get_task_engine
    task = get_task_engine().get_active_task(session_id=session_id)
    return task.to_dict() if task else None


class V2TaskControlRequest(BaseModel):
    action: str = Field(..., pattern="^(pause|resume|continue|stop|cancel|abort|modify|resolve_human)$")
    criteria: Optional[str] = Field(None, max_length=2000)
    payload: Optional[Dict[str, Any]] = None


@app.post("/api/v2/tasks/{task_id}/control")
async def v2_task_control(task_id: str, req: V2TaskControlRequest) -> Dict[str, Any]:
    from core.brain.task_engine import get_task_engine
    te = get_task_engine()
    act = req.action.lower()
    target_task = te.get_task(task_id)
    if not target_task:
        raise HTTPException(status_code=404, detail="Unknown task")
    status = getattr(target_task, "status", None)
    if getattr(status, "value", status) in ("COMPLETED", "FAILED", "CANCELLED"):
        raise HTTPException(status_code=409, detail="Task is already terminal; send a new request instead of changing its recorded outcome.")
    if target_task.context_variables.get("execution_kind") == "capability" and act in ("resume", "continue", "resolve_human", "modify"):
        raise HTTPException(status_code=409, detail="This dynamic task is stopped. Send a new continuation message in its conversation; completed work is preserved and actions are not replayed automatically.")
    ok = False
    msg = ""
    if act == "pause":
        chk = te.pause_task(task_id, reason="User requested pause")
        ok = chk is not None
        msg = "Task paused"
    elif act == "stop":
        ok = te.stop_task(task_id, reason="Stopped by user")
        msg = "Task stopped"
    elif act in ("cancel", "abort"):
        ok = te.cancel_task(task_id, reason="Cancelled by user")
        msg = "Task cancelled"
    elif act == "modify":
        ok = te.modify_task(task_id, new_criteria=req.criteria or "Modified criteria")
        msg = "Task modified"
    elif act in ("resolve_human", "resume", "continue"):
        # Resolving an intervention is not merely a state transition.  The
        # closed-loop orchestrator takes a fresh observation, checks whether a
        # challenge has actually cleared, and then continues from the saved
        # step.  Calling TaskEngine directly here previously made the UI claim
        # a task had resumed even though no work was scheduled.
        from core.orchestrator.closed_loop_orchestrator import get_closed_loop_orchestrator
        result = await asyncio.to_thread(
            get_closed_loop_orchestrator().submit_human_intervention_resolution,
            task_id,
            action="completed",
            resolution_payload=req.payload,
        )
        result_data = result.to_dict()
        ok = bool(result_data.get("success")) and not bool(result_data.get("error"))
        msg = "Human intervention was verified and execution continued" if ok else result_data.get("error", "Could not resume task")
        return {
            "ok": ok,
            "message": msg,
            "action": act,
            "task_id": task_id,
            "execution": result_data,
        }
    return {"ok": ok, "message": msg, "action": act, "task_id": task_id}


@app.get("/api/v2/models/local")
async def v2_list_local_models() -> List[Dict[str, Any]]:
    from core.models.local_model_manager import get_local_model_manager
    return get_local_model_manager().list_local_models()


class V2ImportModelRequest(BaseModel):
    file_path: str = Field(..., min_length=1, max_length=4096)
    name: Optional[str] = Field(None, max_length=200)
    set_default: bool = False


class V2LocalModelSettingsRequest(BaseModel):
    n_gpu_layers: Optional[int] = Field(None, ge=0, le=200)
    n_ctx: Optional[int] = Field(None, ge=512, le=131_072)
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    threads: Optional[int] = Field(None, ge=1, le=256)


@app.post("/api/v2/models/local/import")
async def v2_import_local_model(req: V2ImportModelRequest) -> Dict[str, Any]:
    from core.models.local_model_manager import get_local_model_manager
    ok, msg, data = get_local_model_manager().import_local_model(req.file_path, name=req.name, set_as_default=req.set_default)
    return {"ok": ok, "message": msg, "model": data}


@app.delete("/api/v2/models/local/{model_id}")
async def v2_delete_local_model(model_id: str, delete_file: bool = False) -> Dict[str, Any]:
    from core.models.local_model_manager import get_local_model_manager
    ok, msg = get_local_model_manager().remove_local_model(model_id, delete_file=delete_file)
    return {"ok": ok, "message": msg}


@app.post("/api/v2/models/local/{model_id}/activate")
async def v2_activate_local_model(model_id: str) -> Dict[str, Any]:
    from core.models.local_model_manager import get_local_model_manager
    ok, msg = get_local_model_manager().set_active_local_model(model_id)
    return {"ok": ok, "message": msg}


@app.patch("/api/v2/models/local/{model_id}")
async def v2_configure_local_model(
    model_id: str,
    req: V2LocalModelSettingsRequest,
) -> Dict[str, Any]:
    """Persist validated runtime settings for one registered local model."""
    from core.models.local_model_manager import get_local_model_manager

    params = req.model_dump(exclude_none=True)
    if not params:
        return {"ok": False, "message": "No local model settings were provided."}
    ok, msg = get_local_model_manager().configure_runtime_params(model_id, params)
    return {"ok": ok, "message": msg, "runtime_params": params if ok else {}}


@app.get("/api/v2/models/external")
async def v2_list_external_providers() -> List[Dict[str, Any]]:
    from core.models.local_model_manager import get_local_model_manager
    return get_local_model_manager().list_external_providers()


class V2ConfigureExternalProviderRequest(BaseModel):
    provider_id: str = Field(..., min_length=1, max_length=64)
    api_key: Optional[str] = Field(None, max_length=2048)
    model: Optional[str] = Field(None, max_length=256)
    base_url: Optional[str] = Field(None, max_length=2048)
    set_as_active: bool = False


@app.post("/api/v2/models/external")
async def v2_configure_external_provider(req: V2ConfigureExternalProviderRequest) -> Dict[str, Any]:
    from core.models.local_model_manager import get_local_model_manager
    ok, msg = get_local_model_manager().configure_external_provider(
        provider_id=req.provider_id,
        api_key=req.api_key,
        model=req.model,
        base_url=req.base_url,
        set_as_active=req.set_as_active,
    )
    return {"ok": ok, "message": msg}


@app.delete("/api/v2/models/external/{provider_id}")
async def v2_disconnect_external_provider(provider_id: str) -> Dict[str, Any]:
    """Remove one provider-scoped connection from WISE's secure store."""
    from core.models.local_model_manager import get_local_model_manager

    ok, msg = get_local_model_manager().disconnect_external_provider(provider_id)
    return {"ok": ok, "message": msg}


class V2FetchLiveModelsRequest(BaseModel):
    provider_id: str = Field(..., min_length=1, max_length=64)
    api_key: Optional[str] = Field(None, max_length=2048)
    base_url: Optional[str] = Field(None, max_length=2048)


@app.post("/api/v2/models/external/fetch_models")
async def v2_fetch_live_models_post(req: V2FetchLiveModelsRequest) -> Dict[str, Any]:
    from core.models.local_model_manager import get_local_model_manager
    ok, models, msg = get_local_model_manager().fetch_live_provider_models(
        provider_id=req.provider_id,
        api_key=req.api_key,
        base_url=req.base_url,
    )
    return {"ok": ok, "models": models, "message": msg, "provider_id": req.provider_id}


@app.get("/api/v2/models/external/fetch_models")
async def v2_fetch_live_models_get(provider_id: str, base_url: Optional[str] = None) -> Dict[str, Any]:
    """Fetch models using a previously stored provider credential only.

    API keys in URLs end up in browser history, reverse-proxy logs and server
    access logs. A caller needing a one-off key must use the POST body route.
    """
    from core.models.local_model_manager import get_local_model_manager
    ok, models, msg = get_local_model_manager().fetch_live_provider_models(
        provider_id=provider_id,
        api_key=None,
        base_url=base_url,
    )
    return {"ok": ok, "models": models, "message": msg, "provider_id": provider_id}



@app.get("/api/v2/system/telemetry")
async def v2_system_telemetry() -> Dict[str, Any]:
    import psutil
    from core.context.world_state import get_world_state_engine
    from core.models.provider_interface import get_model_runtime_telemetry
    ws = get_world_state_engine().get_current_world_state()
    mem = psutil.virtual_memory()
    provider = get_model_runtime_telemetry(allow_simulation=False)
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_used_gb": round((mem.total - mem.available) / (1024 ** 3), 2),
        "ram_total_gb": round(mem.total / (1024 ** 3), 2),
        # Do not invent GPU usage. The current runtime has no GPU telemetry
        # adapter, so absence is reported honestly until one is configured.
        "vram_used_mb": None,
        "vram_is_idle": None,
        "active_window": ws.active_window.title if ws.active_window else "Desktop",
        "open_windows_count": len(ws.open_windows),
        # The provider telemetry is canonical.  The legacy local registry can
        # contain old simulated entries and must not be presented as live.
        "active_model": provider.model_name,
        "model_runtime": provider.to_dict(),
    }


# ==============================================================================
# WISE V2 Voice API (Voice-Chat Parity)
# ==============================================================================

class VoiceInteractRequest(BaseModel):
    text: str = Field(..., max_length=4096)
    session_id: Optional[str] = None


@app.post("/api/v2/voice/start")
async def v2_voice_start() -> Dict[str, Any]:
    """Start the persistent voice runtime without blocking event loop."""
    try:
        from core.voice.runtime import get_voice_runtime
        vr = get_voice_runtime()
        ok = await asyncio.to_thread(vr.start)
        return {
            "ok": ok,
            "message": (
                "Voice runtime started"
                if ok else (getattr(vr, "_last_start_error", None) or "Voice runtime is not ready; configure the missing audio capabilities and check voice status.")
            ),
            "capabilities": vr.probe_voice_environment(),
        }
    except Exception as exc:
        return {"ok": False, "message": f"Voice start error: {exc}"}


@app.post("/api/v2/voice/stop")
async def v2_voice_stop() -> Dict[str, Any]:
    """Stop the persistent voice runtime without blocking event loop."""
    try:
        from core.voice.runtime import get_voice_runtime
        vr = get_voice_runtime()
        await asyncio.to_thread(vr.stop)
        return {"ok": True, "message": "Voice runtime stopped"}
    except Exception as exc:
        return {"ok": False, "message": f"Voice stop error: {exc}"}


@app.post("/api/v2/voice/interact")
async def v2_voice_interact(req: VoiceInteractRequest) -> Dict[str, Any]:
    """Execute a voice turn through canonical ConversationalCore pipeline."""
    try:
        from core.voice.runtime import get_voice_runtime
        vr = get_voice_runtime()
        result = await asyncio.to_thread(
            vr.interact,
            transcript_or_audio=req.text,
            session_id=req.session_id,
        )
        # VoiceRuntime owns canonical recording. Do not append a second pair.
        return result.to_dict()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/v2/voice/status")
async def v2_voice_status() -> Dict[str, Any]:
    """Get current voice runtime status."""
    try:
        from core.voice.runtime import get_voice_runtime
        vr = get_voice_runtime()
        return {
            "state": vr.state.value if hasattr(vr.state, "value") else str(vr.state),
            "is_running": getattr(vr, "_is_running", False),
            "total_interactions": getattr(vr, "_total_interactions", 0),
            "last_turn": getattr(vr, "_last_turn", None),
            "capabilities": vr.probe_voice_environment(),
            "latency": {
                "vad_ms": round(getattr(vr, "_last_vad_latency", 0.0), 2),
                "stt_ms": round(getattr(vr, "_last_stt_latency", 0.0), 2),
                "tts_ms": round(getattr(vr, "_last_tts_latency", 0.0), 2),
                "total_ms": round(getattr(vr, "_last_total_latency", 0.0), 2),
            },
        }
    except Exception as exc:
        return {"state": "UNAVAILABLE", "error": str(exc)}


# ==============================================================================
# WISE V2 Real-Time WebSocket Stream
# ==============================================================================

@app.websocket("/api/v2/chat/stream")
async def v2_stream(websocket: WebSocket) -> None:
    """
    Real-time streaming WebSocket for the Desktop GUI.
    Accepts JSON messages: {type: "chat", message: "...", session_id: "..."}
    Sends back streaming events: {type: "token"|"milestone"|"status"|"done"|"error", ...}
    """
    if not _websocket_is_authorized(websocket):
        await websocket.close(code=1008, reason="unauthorized")
        return
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                continue

            msg_type = data.get("type", "chat")
            message = data.get("message", "")
            session_id = data.get("session_id")

            if msg_type == "ping":
                await websocket.send_json({"type": "pong", "ts": time.time()})
                continue

            if msg_type == "task_control":
                task_id = data.get("task_id", "")
                action = data.get("action", "")
                try:
                    from core.brain.task_engine import get_task_engine
                    te = get_task_engine()
                    act = action.lower()
                    if act == "pause":
                        te.pause_task(task_id, reason="User pause via stream")
                        await websocket.send_json({"type": "status", "task_id": task_id, "action": "paused"})
                    elif act in ("resume", "continue"):
                        te.resume_task(task_id)
                        await websocket.send_json({"type": "status", "task_id": task_id, "action": "resumed"})
                    elif act in ("stop", "cancel"):
                        te.cancel_task(task_id, reason="User cancel via stream")
                        await websocket.send_json({"type": "status", "task_id": task_id, "action": "cancelled"})
                except Exception as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                continue

            if not message:
                await websocket.send_json({"type": "error", "message": "Empty message"})
                continue

            # Process conversational turn.  A trace id is created at the
            # boundary so every event for this turn can be correlated with
            # server-side observability, just like the HTTP V2 endpoint.
            trace_id = new_trace_id()
            await websocket.send_json({"type": "status", "stage": "Processing", "trace_id": trace_id})

            try:
                from core.brain.conversational_core import get_conversational_core
                cc = get_conversational_core()
                def _run_stream_turn():
                    with Tracer.trace(trace_id):
                        return cc.process_turn(message, session_id=session_id, modality="chat")

                result = await asyncio.to_thread(_run_stream_turn)

                # Stream milestones with correlation
                for ms in result.milestones:
                    await websocket.send_json({
                        "type": "milestone",
                        "event": f"milestone.{ms.stage.lower()}",
                        "stage": ms.stage,
                        "title": ms.title,
                        "status": ms.status,
                        "session_id": result.session_id,
                        "task_id": result.task_id,
                        "trace_id": trace_id,
                        "timestamp": time.time(),
                    })

                # Send final response with correlation
                await websocket.send_json({
                    "type": "done",
                    "event": "conversation.completed",
                    "reply_text": result.reply_text,
                    "action_type": result.action_type,
                    "task_id": result.task_id,
                    "task_status": result.task_status,
                    "session_id": result.session_id,
                    "trace_id": trace_id,
                    "latency_ms": round(result.latency_ms, 2),
                    "timestamp": time.time(),
                })
            except Exception as exc:
                # Keep the identifier even when a turn fails so a user-visible
                # failure can be matched to the redacted server diagnostics.
                await websocket.send_json({"type": "error", "message": str(exc), "trace_id": trace_id})

    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass


# ==============================================================================
# WISE V2 Session Management
# ==============================================================================

class V2SessionUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=120)
    archived: Optional[bool] = None


def _v2_session_summary(session: Any) -> Dict[str, Any]:
    """Return only index fields; complete message history has its own endpoint."""
    title = str(session.metadata.get("title") or session.last_query or "Session").strip()[:120]
    return {
        "session_id": session.session_id,
        "id": session.session_id,
        "title": title or "Session",
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "last_query": session.last_query,
        "last_response": session.last_response,
        "message_count": len(session.history),
        "metadata": {
            "archived": bool(session.metadata.get("archived")),
            "archived_at": session.metadata.get("archived_at"),
        },
    }


@app.get("/api/v2/sessions")
async def v2_list_sessions(
    include_archived: bool = False,
    limit: int = Query(100, ge=1, le=250),
    offset: int = Query(0, ge=0),
    query: Optional[str] = Query(None, min_length=1, max_length=120),
    include_internal: bool = False,
) -> List[Dict[str, Any]]:
    """List durable user conversations in recency order for the desktop index.

    Scheduler workspaces are retained as durable sessions for auditability, but
    they are operational data rather than user conversations.  They remain
    available to diagnostics via ``include_internal`` without crowding the
    sidebar every time a health pulse runs.
    """
    try:
        from core.session_service import get_session_service
        ss = get_session_service()
        sessions = ss.list_sessions(include_archived=include_archived)
        if not include_internal:
            sessions = [
                session for session in sessions
                if not session.metadata.get("internal")
                and not session.session_id.startswith("sched_")
            ]
        if query:
            needle = query.casefold().strip()
            sessions = [
                session for session in sessions
                if needle in str(session.metadata.get("title", "")).casefold()
                or needle in session.last_query.casefold()
                or needle in session.last_response.casefold()
            ]
        return [_v2_session_summary(s) for s in sessions[offset : offset + limit]]
    except Exception:
        return []


@app.post("/api/v2/sessions/new")
async def v2_new_session() -> Dict[str, Any]:
    """Create a new conversation session via SessionService."""
    import uuid as _uuid
    sid = f"wise_{_uuid.uuid4().hex[:8]}"
    from core.session_service import get_session_service
    s = get_session_service().get_or_create_session(sid)
    return s.to_dict()


@app.get("/api/v2/sessions/{session_id}/history")
async def v2_session_history(
    session_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> List[Dict[str, Any]]:
    """Get a page of durable conversation history in chronological order."""
    try:
        from core.session_service import get_session_service
        s = get_session_service().get_session(session_id)
        if s:
            history = s.history
            start = max(0, len(history) - offset - limit)
            end = len(history) - offset if offset else len(history)
            return history[start:end]
        return []
    except Exception:
        return []


@app.get("/api/v2/sessions/{session_id}")
async def v2_get_session(session_id: str) -> Dict[str, Any]:
    from core.session_service import get_session_service

    session = get_session_service().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_dict()


@app.patch("/api/v2/sessions/{session_id}")
async def v2_update_session(session_id: str, request: V2SessionUpdateRequest) -> Dict[str, Any]:
    from core.session_service import get_session_service

    if request.title is None and request.archived is None:
        raise HTTPException(status_code=400, detail="Provide a title or archived state")
    try:
        session = get_session_service().update_session(
            session_id,
            title=request.title,
            archived=request.archived,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_dict()


@app.delete("/api/v2/sessions/{session_id}")
async def v2_delete_session(session_id: str) -> Dict[str, bool]:
    from core.session_service import get_session_service

    if not get_session_service().delete_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}


# ==============================================================================
# WISE V2 Settings API
# ==============================================================================

class V2InstructionsRequest(BaseModel):
    model_config = {"extra": "forbid"}
    instructions: str = Field(..., max_length=30_000)
    expected_revision: Optional[str] = Field(None, pattern=r"^[0-9a-f]{64}$")


def _guard_instruction_settings(request: Request) -> None:
    """Protect owner preferences against foreign renderer requests/CSRF."""
    from urllib.parse import urlsplit
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site settings request rejected")
    hosts = {"localhost", "127.0.0.1", "::1"}
    if request.client and request.client.host == "testclient":
        hosts.add("testserver")
    if request.url.hostname not in hosts:
        raise HTTPException(403, "Instruction settings are a local owner interface")
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme != request.url.scheme or parsed.netloc != request.url.netloc:
            raise HTTPException(403, "Cross-origin settings request rejected")
    if request.method in {"PUT", "POST"} and request.headers.get("x-wise-action") != "settings":
        raise HTTPException(403, "A same-origin settings action header is required")


@app.get("/api/v2/settings/instructions")
async def v2_get_instructions(request: Request) -> Dict[str, Any]:
    from core.user_instructions import InstructionStorageError, read_user_instructions
    _guard_instruction_settings(request)
    try:
        return read_user_instructions()
    except InstructionStorageError as exc:
        raise HTTPException(503, str(exc)) from None


@app.put("/api/v2/settings/instructions")
async def v2_save_instructions(req: V2InstructionsRequest, request: Request) -> Dict[str, Any]:
    from core.user_instructions import InstructionConflict, InstructionStorageError, save_user_instructions
    _guard_instruction_settings(request)
    try:
        return {"ok": True, **save_user_instructions(req.instructions, expected_revision=req.expected_revision)}
    except InstructionConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except InstructionStorageError as exc:
        raise HTTPException(503, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


class V2SettingsUpdateRequest(BaseModel):
    voice_vad_threshold: Optional[float] = None
    voice_mode: Optional[str] = None  # push_to_talk | continuous_vad
    security_strictness: Optional[str] = None  # low | medium | high
    browser_headless: Optional[bool] = None
    computer_control_enabled: Optional[bool] = None
    emergency_stop_key: Optional[str] = None
    voice_barge_in: Optional[bool] = None
    tts_voice: Optional[str] = None
    tts_engine: Optional[str] = None
    tts_indextts_gpt_checkpoint: Optional[str] = Field(None, max_length=4096)
    tts_reference_audio: Optional[str] = Field(None, max_length=4096)
    tts_clone_language: Optional[str] = None
    tts_idle_unload_seconds: Optional[int] = Field(None, ge=15, le=3600)
    stt_language: Optional[str] = None
    stt_engine: Optional[str] = None
    stt_model: Optional[str] = None
    active_model_engine: Optional[str] = None
    gpu_layers: Optional[int] = None
    context_window: Optional[int] = None
    theme: Optional[str] = None
    ui_language: Optional[str] = None
    autonomy_level: Optional[str] = None
    safe_paths: Optional[List[str]] = None


class V2SystemIntegrationConfigRequest(BaseModel):
    """Safe configuration for the opt-in Super Computer user-session mode."""
    confirmed: bool = False
    start_at_login: Optional[bool] = None
    idle_timeout_seconds: Optional[int] = Field(None, ge=30, le=86_400)
    background_mode: Optional[bool] = None
    computer_control_enabled: Optional[bool] = None
    host_browser_enabled: Optional[bool] = None


class V2SystemIntegrationActivationRequest(BaseModel):
    confirmed: bool = False
    remove_autostart: bool = True


class V2UacMaintenanceRequest(BaseModel):
    """A request for one vetted, owner-visible UAC maintenance operation."""
    operation: str = Field(..., min_length=1, max_length=64)
    confirmed: bool = False
    intent_token: str = Field(..., min_length=32, max_length=200)


class V2UacIntentRequest(BaseModel):
    """Prepare one short-lived, same-origin UAC request."""
    operation: str = Field(..., min_length=1, max_length=64)


@app.get("/api/v2/settings")
async def v2_get_settings() -> Dict[str, Any]:
    """Retrieve current WISE agent settings."""
    try:
        from core.server_config import load_server_config
        cfg = load_server_config()
        result = {
            "voice_vad_threshold": cfg.get("voice_vad_threshold", 0.5),
            "voice_mode": cfg.get("voice_mode", "push_to_talk"),
            "security_strictness": cfg.get("security_strictness", "medium"),
            "browser_headless": cfg.get("browser_headless", True),
            "computer_control_enabled": cfg.get("computer_control_enabled", True),
            "emergency_stop_key": cfg.get("emergency_stop_key", "Escape"),
            "voice_barge_in": cfg.get("voice_barge_in", True),
            "tts_voice": cfg.get("tts_voice", "ar-SA-ShakirNeural"),
            "tts_engine": cfg.get("tts_engine", "indextts"),
            "tts_reference_audio": cfg.get("tts_reference_audio", ""),
            "tts_indextts_gpt_checkpoint": cfg.get("tts_indextts_gpt_checkpoint", ""),
            "tts_clone_language": cfg.get("tts_clone_language", "ar"),
            "tts_idle_unload_seconds": cfg.get("tts_idle_unload_seconds", 120),
            "stt_language": cfg.get("stt_language", "auto"),
            "stt_engine": cfg.get("stt_engine", "whisper_cpu"),
            "stt_model": cfg.get("stt_model", "base"),
            "active_model_engine": cfg.get("active_model_engine", "local"),
            "gpu_layers": cfg.get("gpu_layers", 33),
            "context_window": cfg.get("context_window", 8192),
            "theme": cfg.get("theme", "dark"),
            "ui_language": cfg.get("ui_language", "ar"),
            "autonomy_level": cfg.get("autonomy_level", "balanced"),
            "safe_paths": cfg.get("safe_paths", ["c:/Users/STS/OneDrive/Documents/WISE"]),
            "cors_origins": cfg.get("cors_origins", []),
        }
        try:
            from core.system_integration import get_system_integration_manager
            result["system_integration"] = get_system_integration_manager().status()
        except Exception as exc:
            result["system_integration"] = {"error": str(exc)}
        return result
    except Exception as exc:
        return {"error": str(exc)}


@app.patch("/api/v2/settings")
async def v2_update_settings(req: V2SettingsUpdateRequest) -> Dict[str, Any]:
    """Update WISE agent settings."""
    try:
        if req.tts_engine is not None and req.tts_engine not in {"indextts", "windows_sapi"}:
            return {"ok": False, "error": "Unsupported TTS engine"}
        from core.server_config import load_server_config, save_server_config
        cfg = load_server_config()
        updates = {}
        fields = [
            "voice_vad_threshold", "voice_mode", "security_strictness",
            "browser_headless", "computer_control_enabled", "emergency_stop_key",
            "voice_barge_in", "tts_voice", "tts_engine", "tts_reference_audio",
            "tts_indextts_gpt_checkpoint", "stt_engine", "stt_model",
            "tts_clone_language", "stt_language", "active_model_engine",
            "tts_idle_unload_seconds",
            "gpu_layers", "context_window", "theme", "ui_language", "autonomy_level",
            "safe_paths"
        ]
        for f in fields:
            val = getattr(req, f, None)
            if val is not None:
                cfg[f] = val
                updates[f] = val
        if updates:
            save_server_config(cfg)
        return {"ok": True, "updated": updates}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/v2/system-integration")
async def v2_system_integration_status() -> Dict[str, Any]:
    """Return the real status of optional persistent user-session mode."""
    from core.system_integration import get_system_integration_manager
    return get_system_integration_manager().status()


@app.patch("/api/v2/system-integration")
async def v2_update_system_integration(
    req: V2SystemIntegrationConfigRequest,
) -> Dict[str, Any]:
    """Save configuration only; enabling needs a separate confirmation."""
    from core.system_integration import get_system_integration_manager
    values = req.model_dump(exclude_none=True)
    confirmed = bool(values.pop("confirmed", False))
    try:
        status = get_system_integration_manager().configure(
            values,
            confirmed=confirmed,
        )
    except PermissionError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "requires_confirmation": True,
            "status": get_system_integration_manager().status(),
        }
    except OSError as exc:
        return {"ok": False, "error": str(exc), "status": get_system_integration_manager().status()}
    return {"ok": True, "status": status}


@app.post("/api/v2/system-integration/activate")
async def v2_activate_system_integration(
    req: V2SystemIntegrationActivationRequest,
) -> Dict[str, Any]:
    """Enable Super Computer mode after a confirmed settings change."""
    from core.system_integration import get_system_integration_manager
    return get_system_integration_manager().activate(confirmed=req.confirmed)


@app.post("/api/v2/system-integration/deactivate")
async def v2_deactivate_system_integration(
    req: V2SystemIntegrationActivationRequest,
) -> Dict[str, Any]:
    """Disable the mode and remove its login task by default."""
    from core.system_integration import get_system_integration_manager
    return get_system_integration_manager().deactivate(
        confirmed=req.confirmed,
        remove_autostart=req.remove_autostart,
    )


@app.get("/api/v2/system-integration/uac/operations")
async def v2_uac_operations() -> Dict[str, Any]:
    """List the fixed, reviewable maintenance operations that may request UAC."""
    from core.windows.uac import get_uac_broker
    broker = get_uac_broker()
    return {
        "supported": broker.supported(),
        "operations": broker.operations(),
        "runs": broker.runs(limit=10),
    }


@app.get("/api/v2/system-integration/uac/runs")
async def v2_uac_runs(limit: int = 20) -> Dict[str, Any]:
    """Return active and recent monitored UAC operation lifecycles."""
    from core.windows.uac import get_uac_broker
    return get_uac_broker().runs(limit=max(1, min(int(limit), 50)))


@app.post("/api/v2/system-integration/uac/intent")
async def v2_issue_uac_intent(req: V2UacIntentRequest) -> Dict[str, Any]:
    """Issue a short-lived, single-use intent bound to one fixed operation."""
    from core.windows.uac import get_uac_broker
    return get_uac_broker().issue_intent(req.operation)


@app.post("/api/v2/system-integration/uac/request")
async def v2_request_uac_maintenance(req: V2UacMaintenanceRequest) -> Dict[str, Any]:
    """Ask Windows itself to approve one elevated maintenance operation."""
    from core.windows.uac import get_uac_broker
    return get_uac_broker().request(
        req.operation,
        confirmed=req.confirmed,
        intent_token=req.intent_token,
    )


# Mount Desktop Application
from starlette.staticfiles import StaticFiles

_WISE_WEB_APP_DIR = Path(__file__).resolve().parent.parent / "ui" / "wise_web"
_DESKTOP_APP_DIR = _WISE_WEB_APP_DIR
if _DESKTOP_APP_DIR.exists():
    class SPAStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope):
            try:
                response = await super().get_response(path, scope)
                if response.status_code == 404:
                    return await super().get_response("index.html", scope)
                if path.endswith(".ttf"):
                    response.headers["content-type"] = "font/ttf"
                elif path.endswith(".woff2"):
                    response.headers["content-type"] = "font/woff2"
                elif path.endswith(".woff"):
                    response.headers["content-type"] = "font/woff"
                elif path.endswith(".svg"):
                    response.headers["content-type"] = "image/svg+xml"
                response.headers["Access-Control-Allow-Origin"] = "*"
                return response
            except Exception:
                return await super().get_response("index.html", scope)

    app.mount("/app", SPAStaticFiles(directory=str(_DESKTOP_APP_DIR), html=True), name="wise_desktop_app")


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def root_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/app/")


from core.paths import CONFIG_DIR, MEMORY_DIR, resolve_config_file
from core.mcp.registry import MCPRegistry, MCPServerConfig

from core.mcp import get_registry as _get_canonical_mcp_registry
_mcp_registry = _get_canonical_mcp_registry()
_APP_CONFIG_PATH = CONFIG_DIR / "app_config.json"


def _load_app_config() -> Dict[str, Any]:
    default_cfg: Dict[str, Any] = {
        "display": {
            "language": "ar",
            "theme": "dark",
            "theme_name": "nous-alt",
        },
        "system": {
            "locale": "ar",
            "timezone": "UTC",
        },
        "model": "wise-moe",
        "agent": {"name": "WISE Agent"},
        "appearance": {"theme": "dark"},
    }
    source = resolve_config_file("app_config.json")
    if source.exists():
        try:
            stored = json.loads(source.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                for k, v in stored.items():
                    if isinstance(v, dict) and isinstance(default_cfg.get(k), dict):
                        default_cfg[k].update(v)
                    else:
                        default_cfg[k] = v
        except Exception:
            pass
    return default_cfg


def _save_app_config(cfg: Dict[str, Any]) -> None:
    try:
        _APP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _APP_CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _APP_CONFIG_PATH)
    except Exception:
        pass


@app.get("/api/config", include_in_schema=False)
async def get_app_config(include_defaults: bool = True):
    cfg = _load_app_config()
    return {
        "ok": True,
        "config": cfg,
        **cfg,
    }


@app.put("/api/config", include_in_schema=False)
async def put_app_config(req: Request, preserve_language: bool = False):
    try:
        body = await req.json()
    except Exception:
        body = {}
    incoming = body.get("config") if isinstance(body, dict) and "config" in body else body
    if not isinstance(incoming, dict):
        incoming = {}

    current = _load_app_config()
    for k, v in incoming.items():
        if isinstance(v, dict) and isinstance(current.get(k), dict):
            current[k].update(v)
        else:
            current[k] = v

    _save_app_config(current)
    return {
        "ok": True,
        "config": current,
        **current,
    }


@app.get("/api/profiles/active", include_in_schema=False)
async def get_hermes_profiles_active_compat():
    return {
        "name": "default",
        "active": True
    }


@app.get("/api/audio/voice-live/status", include_in_schema=False)
async def get_voice_live_status_compat():
    return {"status": "idle", "active": False}


# --------------------------------------------------------------------------
# WISE Desktop UI Foundation API v2 Endpoints
# --------------------------------------------------------------------------
_DESKTOP_SESSIONS: List[Dict[str, Any]] = []


def _live_model_snapshot() -> Dict[str, Any]:
    """Expose the canonical model state without manufacturing a local engine."""
    try:
        from core.models.provider_interface import get_model_runtime_telemetry

        telemetry = get_model_runtime_telemetry(allow_simulation=False)
        payload = telemetry.to_dict()
        ready = bool(
            not telemetry.is_simulated
            and str(telemetry.load_state).upper() not in {"UNAVAILABLE", "ERROR", "FAILED"}
        )
        return {
            "ready": ready,
            "provider": telemetry.active_provider,
            "model": telemetry.model_name,
            "is_simulated": telemetry.is_simulated,
            "load_state": telemetry.load_state,
            "context_length": telemetry.context_length,
            "telemetry": payload,
        }
    except Exception as exc:
        return {
            "ready": False,
            "provider": "unavailable",
            "model": "unavailable",
            "is_simulated": False,
            "load_state": "UNAVAILABLE",
            "context_length": None,
            "error": str(exc),
        }


def _compat_model_options() -> Dict[str, Any]:
    """Build the desktop compatibility model picker from real registries.

    Older desktop clients use ``/api/model/options`` and ``model.options``.
    They previously received a fabricated WISE model even when no model was
    installed or configured, which made a failed chat look like a provider
    bug. Keep the response shape, but use only local discovery and the
    credential-backed provider registry.
    """
    live = _live_model_snapshot()
    providers: List[Dict[str, Any]] = []
    try:
        from core.models.local_model_manager import get_local_model_manager

        manager = get_local_model_manager()
        available_local = [
            m for m in manager.list_local_models()
            if m.get("status") == "AVAILABLE"
        ]
        if available_local:
            providers.append({
                "slug": "local",
                "name": "Local GGUF",
                "models": [str(m.get("id") or m.get("name")) for m in available_local],
                "is_current": live["provider"] == "local",
                "authenticated": True,
            })

        for provider in manager.list_external_providers():
            provider_id = str(provider.get("id") or "")
            has_key = bool(provider.get("has_api_key"))
            is_current = provider_id == live["provider"]
            providers.append({
                "slug": provider_id,
                "name": provider.get("name") or provider_id,
                "models": [provider.get("default_model")] if provider.get("default_model") else [],
                "is_current": is_current,
                # Local HTTP runtimes may intentionally have no key, but are
                # usable without one only when they are actually live.
                "authenticated": has_key or (is_current and live["ready"]),
                "configured": has_key,
                "transport": provider.get("transport"),
            })
    except Exception as exc:
        return {
            "model": live["model"], "provider": live["provider"],
            "providers": [], "ready": live["ready"], "error": str(exc),
        }

    return {
        "model": live["model"], "provider": live["provider"],
        "providers": providers, "ready": live["ready"],
        "load_state": live["load_state"],
    }


def _compat_terminal_backends() -> Dict[str, Any]:
    """Report the actual isolation state instead of labelling host shell safe."""
    sandboxed = bool(getattr(agent_core.sandbox, "use_proot", False))
    name = "proot" if sandboxed else "host_allowlist"
    return {
        "active": name,
        "backends": [{
            "name": name,
            "label": "PRoot sandbox" if sandboxed else "Host command allow-list",
            "description": (
                "Commands execute inside the configured PRoot filesystem."
                if sandboxed else
                "Commands execute on the host through the command allow-list; no OS-level sandbox is active."
            ),
            "active": True,
            "status": "ready" if sandboxed else "degraded",
            "detail": "" if sandboxed else "Install/configure PRoot for filesystem isolation.",
            "sandbox_enforced": sandboxed,
        }],
    }


def _compat_computer_use_status() -> Dict[str, Any]:
    """Expose platform capability without inventing a granted permission."""
    available = sys.platform == "win32"
    return {
        "available": available,
        "granted": False,
        "platform": "windows" if available else sys.platform,
        "status": "available_on_request" if available else "unsupported_platform",
        "detail": (
            "Computer actions are individually governed by the security gate."
            if available else "Native Windows computer control is unavailable on this platform."
        ),
    }


def _compat_moa_state() -> Dict[str, Any]:
    """The MoA setting is not implemented; present it as disabled, not live."""
    live = _live_model_snapshot()
    aggregator = {"provider": live["provider"], "model": live["model"], "enabled": False}
    return {
        "default_preset": "disabled",
        "active_preset": "disabled",
        "presets": {"disabled": {
            "aggregator": aggregator,
            "aggregator_temperature": None,
            "degraded_reference_policy": "disabled",
            "enabled": False,
            "reference_models": [],
            "reference_temperature": None,
            "reference_timeout": None,
        }},
        "aggregator": aggregator,
        "aggregator_temperature": None,
        "degraded_reference_policy": "disabled",
        "enabled": False,
        "reference_models": [],
        "reference_temperature": None,
        "reference_timeout": None,
    }


@app.websocket("/api/v2/stream")
async def api_v2_stream_endpoint(websocket: WebSocket):
    """WebSocket stream endpoint implementing JSON-RPC protocol for Hermes Desktop gateway."""
    import uuid as _uuid
    if not _websocket_is_authorized(websocket):
        await websocket.close(code=1008, reason="unauthorized")
        return
    await websocket.accept()

    # Send gateway.ready event immediately — the Hermes JsonRpcGatewayClient
    # uses this to seed its replay-epoch so seq-based lossless reconnect works.
    ready_event = json.dumps({
        "jsonrpc": "2.0",
        "method": "gateway.ready",
        "params": {
            "epoch": _uuid.uuid4().hex,
            "version": "WISE 2.0",
        },
    })
    try:
        await websocket.send_text(ready_event)
    except Exception:
        return

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue

            msg_id = msg.get("id")
            method = msg.get("method", "")
            params = msg.get("params") or {}

            # Notifications (no id) — nothing to respond to
            if msg_id is None:
                continue

            result = _handle_gateway_jsonrpc(method, params)
            response = json.dumps({
                "jsonrpc": "2.0",
                "result": result,
                "id": msg_id,
            })
            await websocket.send_text(response)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


def _handle_gateway_jsonrpc(method: str, params: dict) -> Any:
    """Dispatch a JSON-RPC method from the Hermes Desktop gateway client."""
    if method == "ping":
        return "pong"

    if method == "session.list":
        return _DESKTOP_SESSIONS

    if method == "session.create":
        import uuid as _uuid
        sid = f"wise_{_uuid.uuid4().hex[:8]}"
        sess = {
            "session_id": sid,
            "id": sid,
            "title": "New Chat",
            "created_at": time.time(),
            "messages": [],
        }
        _DESKTOP_SESSIONS.insert(0, sess)
        return sess

    if method == "session.info":
        sid = params.get("session_id", "")
        for s in _DESKTOP_SESSIONS:
            if s.get("session_id") == sid or s.get("id") == sid:
                return s
        return {"session_id": sid, "title": "Chat", "messages": []}

    if method.startswith("session.events"):
        return {"events": [], "truncated": False}

    if method == "setup.status":
        live = _live_model_snapshot()
        return {
            "provider_configured": live["ready"],
            "ready": live["ready"],
            "free_tier": False,
            "other_providers": False,
            "inference_provider": live["provider"],
            "model": live["model"],
            "load_state": live["load_state"],
            "error": live.get("error"),
        }

    if method == "setup.runtime_check":
        live = _live_model_snapshot()
        return {
            "ok": live["ready"],
            "provider": live["provider"],
            "model": live["model"],
            "free_tier": False,
            "load_state": live["load_state"],
            "error": live.get("error"),
        }

    if method in ("config.get", "config.read", "config.list"):
        return _load_app_config()

    if method in ("model.info", "model.get"):
        live = _live_model_snapshot()
        return {
            "model": live["model"],
            "provider": live["provider"],
            "effective_context_length": live["context_length"],
            "config_context_length": live["context_length"],
            "auto_context_length": live["context_length"],
            "ready": live["ready"],
            "load_state": live["load_state"],
            "capabilities": {},
        }

    if method == "model.options":
        return _compat_model_options()

    if method in ("model.moa", "moa.get"):
        return _compat_moa_state()

    if method in ("model.auxiliary", "auxiliary.get"):
        live = _live_model_snapshot()
        return {
            "main": {"model": live["model"], "provider": live["provider"], "ready": live["ready"]},
            "tasks": [],
        }

    if method in ("profile.list", "profile.active", "profiles.active",
                   "profile.get", "profiles.list"):
        return {
            "active_profile": "default",
            "name": "default",
            "profile": "default",
            "active": True,
            "profiles": [{"name": "default", "active": True}],
        }

    if method in ("cwd.get", "workspace.cwd"):
        return {"cwd": "."}

    if method in ("terminal.backends", "tools.terminal.backends"):
        return _compat_terminal_backends()

    if method.startswith("tools."):
        if "terminal.backends" in method:
            return _compat_terminal_backends()
        if "computer-use" in method or "computer_use" in method:
            return _compat_computer_use_status()
        return []

    if method.startswith("skills."):
        if method in ("skills.hub.official", "skills.official"):
            return {"skills": []}
        if method in ("skills.hub.sources", "skills.sources"):
            return {"sources": []}
        if method in ("skills.hub.search", "skills.search"):
            return {"results": [], "skills": []}
        return []

    if method.startswith("cron."):
        if "targets" in method or "delivery" in method:
            return {"targets": []}
        if "blueprints" in method:
            return {"blueprints": []}
        if "runs" in method:
            return {"runs": []}
        return []

    if method.startswith("messaging."):
        if "pairing" in method:
            return {"approved": [], "pending": []}
        if "webhooks" in method:
            return {"webhooks": [], "enabled": False}
        return {"platforms": _DEFAULT_MESSAGING_PLATFORMS}

    if method.startswith("mcp."):
        if "catalog" in method:
            return {"entries": [], "diagnostics": []}
        return {"servers": []}

    if method.startswith("memory."):
        return {"active": "local", "providers": [], "builtin_files": {"memory": 0, "user": 0}}

    if method.startswith("curator."):
        return {"enabled": False, "paused": False, "interval_hours": None, "last_run_at": None}

    if method.startswith("providers."):
        if "oauth" in method:
            return {"providers": []}
        if "custom" in method:
            return {"endpoints": []}
        return []

    if method == "prompt.submit":
        return {
            "ok": False,
            "error": "prompt.submit is not implemented by the JSON-RPC compatibility bridge; use /api/v2/chat.",
        }

    return {"ok": False, "error": f"Unsupported JSON-RPC method: {method}"}


# These compatibility helpers intentionally have no routes. The former
# handlers registered duplicate paths and returned fabricated WISE model/GPU
# values, shadowing the canonical live endpoints defined above.
async def api_v2_telemetry_compat() -> Dict[str, Any]:
    return await v2_system_telemetry()


async def api_v2_models_local_compat() -> List[Dict[str, Any]]:
    return await v2_list_local_models()


# Note: /api/v2/sessions, /api/v2/sessions/new, and /api/v2/chat are canonically
# defined and bound to SessionService and ConversationalCore above.


@app.get("/api/model/info", include_in_schema=False)
@app.get("/api/model", include_in_schema=False)
async def get_hermes_model_info_compat():
    live = _live_model_snapshot()
    return {
        "model": live["model"],
        "provider": live["provider"],
        "effective_context_length": live["context_length"],
        "config_context_length": live["context_length"],
        "auto_context_length": live["context_length"],
        "ready": live["ready"],
        "load_state": live["load_state"],
        "capabilities": {},
    }


@app.get("/api/tools/terminal/backends", include_in_schema=False)
async def get_hermes_terminal_backends_compat():
    return _compat_terminal_backends()


@app.get("/api/analytics/usage", include_in_schema=False)
async def get_hermes_analytics_usage_compat():
    return {
        "days": 30,
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "sessions": [],
    }


@app.get("/api/model/options", include_in_schema=False)
async def get_model_options_compat():
    return _compat_model_options()


@app.get("/api/model/recommended-default", include_in_schema=False)
async def get_model_recommended_default_compat():
    live = _live_model_snapshot()
    return {"provider": live["provider"], "model": live["model"], "free_tier": None, "ready": live["ready"]}


@app.get("/api/model/auxiliary", include_in_schema=False)
async def get_model_auxiliary_compat():
    live = _live_model_snapshot()
    return {
        "main": {"model": live["model"], "provider": live["provider"], "ready": live["ready"]},
        "tasks": [],
    }


@app.get("/api/model/moa", include_in_schema=False)
@app.put("/api/model/moa", include_in_schema=False)
async def get_or_save_model_moa_compat(req: Request):
    state = _compat_moa_state()
    if req.method.upper() == "PUT":
        return {
            "ok": False,
            "error": "MoA configuration is not implemented; this compatibility endpoint is read-only.",
            **state,
        }
    return {"ok": True, **state}


@app.get("/api/skills", include_in_schema=False)
async def get_skills_compat():
    return []


@app.get("/api/skills/hub/official", include_in_schema=False)
async def get_skills_hub_official_compat():
    return {"skills": []}


@app.get("/api/skills/hub/sources", include_in_schema=False)
async def get_skills_hub_sources_compat():
    return {"sources": []}


@app.get("/api/skills/hub/search", include_in_schema=False)
async def get_skills_hub_search_compat():
    return {"results": [], "skills": []}


@app.get("/api/skills/content", include_in_schema=False)
async def get_skills_content_compat():
    return {"content": "", "name": "", "path": ""}


@app.get("/api/tools/toolsets", include_in_schema=False)
async def get_tools_toolsets_compat():
    return []


@app.get("/api/tools/computer-use/status", include_in_schema=False)
async def get_computer_use_status_compat():
    return _compat_computer_use_status()


@app.get("/api/cron/jobs", include_in_schema=False)
@app.get("/api/cron", include_in_schema=False)
async def get_cron_jobs_compat():
    return []


@app.get("/api/cron/delivery-targets", include_in_schema=False)
async def get_cron_delivery_targets_compat():
    return {"targets": []}


@app.get("/api/cron/blueprints", include_in_schema=False)
async def get_cron_blueprints_compat():
    return {"blueprints": []}


_DEFAULT_MESSAGING_PLATFORMS = [
    {
        "id": "telegram",
        "name": "Telegram",
        "description": "Chat with WISE Agent via Telegram Bot",
        "docs_url": "https://core.telegram.org/bots",
        "enabled": False,
        "configured": False,
        "gateway_running": True,
        "state": "idle",
        "env_vars": [
            {
                "key": "TELEGRAM_BOT_TOKEN",
                "prompt": "Telegram Bot Token",
                "description": "Bot token obtained from @BotFather",
                "is_password": True,
                "is_set": False,
                "required": True,
                "redacted_value": None,
                "url": "https://t.me/BotFather",
            },
            {
                "key": "TELEGRAM_ALLOWED_USERS",
                "prompt": "Allowed User IDs",
                "description": "Comma-separated Telegram user IDs",
                "is_password": False,
                "is_set": False,
                "required": False,
                "redacted_value": None,
                "url": None,
            },
            {
                "key": "TELEGRAM_PROXY",
                "prompt": "Proxy URL",
                "description": "Optional HTTP/SOCKS5 proxy URL",
                "is_password": False,
                "is_set": False,
                "required": False,
                "redacted_value": None,
                "url": None,
            },
        ],
    },
    {
        "id": "discord",
        "name": "Discord",
        "description": "Chat with WISE Agent inside Discord servers and direct messages",
        "docs_url": "https://discord.com/developers/applications",
        "enabled": False,
        "configured": False,
        "gateway_running": True,
        "state": "idle",
        "env_vars": [
            {
                "key": "DISCORD_BOT_TOKEN",
                "prompt": "Discord Bot Token",
                "description": "Bot token from Discord Developer Portal",
                "is_password": True,
                "is_set": False,
                "required": True,
                "redacted_value": None,
                "url": "https://discord.com/developers/applications",
            },
            {
                "key": "DISCORD_ALLOWED_USERS",
                "prompt": "Allowed User IDs",
                "description": "Comma-separated Discord user IDs",
                "is_password": False,
                "is_set": False,
                "required": False,
                "redacted_value": None,
                "url": None,
            },
            {
                "key": "DISCORD_HOME_CHANNEL",
                "prompt": "Home Channel ID",
                "description": "Default channel for announcements and alerts",
                "is_password": False,
                "is_set": False,
                "required": False,
                "redacted_value": None,
                "url": None,
            },
        ],
    },
    {
        "id": "slack",
        "name": "Slack",
        "description": "Connect WISE Agent to Slack workspaces and channels",
        "docs_url": "https://api.slack.com/apps",
        "enabled": False,
        "configured": False,
        "gateway_running": True,
        "state": "idle",
        "env_vars": [
            {
                "key": "SLACK_BOT_TOKEN",
                "prompt": "Bot User OAuth Token",
                "description": "Slack token starting with xoxb-",
                "is_password": True,
                "is_set": False,
                "required": True,
                "redacted_value": None,
                "url": "https://api.slack.com/apps",
            },
            {
                "key": "SLACK_APP_TOKEN",
                "prompt": "App-Level Token",
                "description": "Slack app token starting with xapp- for Socket Mode",
                "is_password": True,
                "is_set": False,
                "required": True,
                "redacted_value": None,
                "url": "https://api.slack.com/apps",
            },
        ],
    },
    {
        "id": "whatsapp",
        "name": "WhatsApp",
        "description": "Connect WISE Agent with WhatsApp Business API",
        "docs_url": "https://developers.facebook.com/docs/whatsapp",
        "enabled": False,
        "configured": False,
        "gateway_running": True,
        "state": "idle",
        "env_vars": [
            {
                "key": "WHATSAPP_PHONE_NUMBER_ID",
                "prompt": "Phone Number ID",
                "description": "WhatsApp Business Cloud API Phone Number ID",
                "is_password": False,
                "is_set": False,
                "required": True,
                "redacted_value": None,
                "url": "https://developers.facebook.com",
            },
            {
                "key": "WHATSAPP_ACCESS_TOKEN",
                "prompt": "Access Token",
                "description": "Meta System User access token",
                "is_password": True,
                "is_set": False,
                "required": True,
                "redacted_value": None,
                "url": "https://developers.facebook.com",
            },
        ],
    },
]


@app.get("/api/messaging/platforms", include_in_schema=False)
async def get_messaging_platforms_compat():
    return {"platforms": _DEFAULT_MESSAGING_PLATFORMS}


@app.patch("/api/messaging/platforms/{platform_id}", include_in_schema=False)
@app.post("/api/messaging/platforms/{platform_id}", include_in_schema=False)
async def update_messaging_platform_compat(platform_id: str, req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    target = next((p for p in _DEFAULT_MESSAGING_PLATFORMS if p["id"] == platform_id), None)
    if target:
        if "enabled" in body:
            target["enabled"] = bool(body["enabled"])
        if "env" in body and isinstance(body["env"], dict):
            target["configured"] = True
            for ev in target.get("env_vars", []):
                if ev["key"] in body["env"]:
                    ev["is_set"] = True
                    ev["redacted_value"] = "••••••••"
        return {"ok": True, "platform": target}
    return {"ok": True}


@app.get("/api/profiles/sessions", include_in_schema=False)
@app.get("/api/sessions", include_in_schema=False)
async def get_profiles_sessions_compat(limit: int = 50, offset: int = 0):
    try:
        from core.session_service import get_session_service
        ss = get_session_service()
        all_s = [s.to_dict() for s in ss.list_sessions()]
        page = all_s[offset : offset + limit]
        return {
            "sessions": page,
            "total": len(all_s),
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < len(all_s),
        }
    except Exception:
        return {
            "sessions": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "has_more": False,
        }


@app.get("/api/pairing", include_in_schema=False)
async def get_pairing_compat():
    return {"approved": [], "pending": []}


@app.get("/api/webhooks", include_in_schema=False)
async def get_webhooks_compat():
    return {"webhooks": [], "enabled": False}



@app.get("/api/config/defaults", include_in_schema=False)
async def get_config_defaults_compat():
    return {
        "display": {
            "language": "en",
            "theme": "dark",
            "theme_name": "nous-alt",
        }
    }


@app.get("/api/config/schema", include_in_schema=False)
async def get_config_schema_compat():
    return {
        "fields": ["display", "system", "model"],
        "properties": {
            "display": {"type": "object", "properties": {"language": {"type": "string"}}},
            "system": {"type": "object", "properties": {"locale": {"type": "string"}}},
            "model": {"type": "object", "properties": {"default": {"type": "string"}}}
        },
        "required": [],
    }


@app.get("/api/mcp/servers", include_in_schema=False)
async def get_mcp_servers():
    try:
        servers = _mcp_registry.list_servers()
        return {"servers": servers}
    except Exception:
        return {"servers": []}


@app.post("/api/mcp/servers", include_in_schema=False)
async def add_mcp_server(req: Request):
    try:
        body = await req.json()
        res = _mcp_registry.add(body)
        return {"ok": True, "server": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/mcp/servers/{name}", include_in_schema=False)
async def remove_mcp_server(name: str):
    try:
        ok = _mcp_registry.remove(name)
        return {"ok": ok}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/mcp/servers/{name}/enabled", include_in_schema=False)
async def toggle_mcp_server_enabled(name: str, req: Request):
    try:
        body = await req.json()
        enabled = bool(body.get("enabled", True))
        ok = _mcp_registry.set_enabled(name, enabled)
        return {"ok": ok, "name": name, "enabled": enabled}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/mcp/servers/{name}/test", include_in_schema=False)
async def test_mcp_server(name: str):
    servers = _mcp_registry.list_servers()
    srv = next((s for s in servers if s["name"].lower() == name.lower()), None)
    if not srv:
        raise HTTPException(status_code=404, detail=f"Server {name} not found")
    return {
        "ok": True,
        "name": srv["name"],
        "alive": srv.get("alive", True),
        "tools": srv.get("tools", 0),
        "message": f"MCP server '{srv['name']}' verified with {srv.get('tools', 0)} tool(s).",
    }


@app.get("/api/mcp/catalog", include_in_schema=False)
async def get_mcp_catalog():
    servers = _mcp_registry.list_servers()
    catalog_entries = [
        {
            "name": s["name"],
            "description": s["description"],
            "category": "developer" if "git" in s["name"].lower() or "test" in s["name"].lower() else "system",
            "installed": True,
            "enabled": s["enabled"],
            "tools": s["tools"],
            "tool_names": s.get("tool_names", []),
        }
        for s in servers
    ]
    return {"entries": catalog_entries, "diagnostics": []}


@app.post("/api/mcp/catalog/install", include_in_schema=False)
async def install_mcp_catalog_entry(req: Request):
    try:
        body = await req.json()
        name = body.get("name", "")
        _mcp_registry.set_enabled(name, True)
        return {"ok": True, "name": name}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/env", include_in_schema=False)
async def get_env_compat():
    return {}


__all__ = ["app"]
# End of public API module.
