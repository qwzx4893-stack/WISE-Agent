"""MCP security & control layer.

Three concerns live here:

1. **Tool classification** (:class:`ToolClassifier`) — every MCP-discovered
   tool is labelled ``read | write | dangerous`` based on a layered
   ruleset:

   - explicit override per server (``classifications`` dict in
     ``mcp_servers.json``).
   - JSON Schema heuristics (write-y verbs in name/description; presence
     of side-effect-shaped params like ``path`` + ``content``, ``url``
     + body, ``cmd``…).
   - DANGEROUS_PATTERNS regex against the tool name + description.

   The classification is stored beside each registered tool so the
   confirmation gate, the UI, and the bridge can all read it.

2. **Confirmation gate** (:class:`ConfirmationGate`) — a thread-safe
   state machine that holds *pending* write-class calls until either:

   - the API surface accepts it (``POST /admin/mcp/confirm/<call_id>``),
   - the call is made with ``confirm=True`` (programmatic skip used by
     trusted callers), or
   - the (server, tool) tuple is on the per-server ``auto_approve``
     allowlist.

   Calls that aren't approved within the timeout raise
   :class:`ConfirmationRequired` so the caller (ThinkingEngine, REST
   handler, …) can surface the prompt to the user.

3. **Network allowlist** (:func:`validate_http_url`) — HTTP / SSE
   transports must point at a domain on the configured allowlist before
   the client opens. Wildcards (``*.example.com``) are supported.

The module deliberately has zero hard dependencies (no httpx, no fastapi)
so unit tests can drive it without the rest of the stack.
"""

from __future__ import annotations

import fnmatch
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlparse


# --------------------------------------------------------------------------
# Tool classification
# --------------------------------------------------------------------------

# Verbs / nouns that strongly signal a write side-effect.
_WRITE_VERBS = {
    "create", "make", "add", "insert", "post", "put", "send", "submit",
    "write", "save", "store", "upload", "publish", "deploy", "merge",
    "commit", "push", "delete", "remove", "drop", "destroy", "purge",
    "kill", "stop", "shutdown", "reboot", "restart", "execute", "exec",
    "run", "invoke", "call", "patch", "update", "modify", "edit",
    "rename", "move", "copy", "set", "clear", "reset", "rotate",
    "rollback", "approve", "reject", "close", "open", "fork", "branch",
    "release", "tag", "schedule", "cancel", "trigger", "ack",
}
_READ_VERBS = {
    "list", "search", "get", "fetch", "read", "show", "describe",
    "ping", "status", "head", "view", "preview", "lookup", "find",
    "query", "select", "stat", "diff", "log", "history", "inspect",
    "dump", "tail",
}

# Dangerous tokens — write *and* high-blast-radius. These imply
# auto-classification as ``dangerous`` regardless of confirm flag,
# unless the user explicitly whitelists.
_DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/", r"mkfs", r"dd\s+if=/dev/zero",
    r"shutdown", r"reboot", r"halt",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\};:",
    r"chmod\s+777\s+/", r">\s*/dev/sd",
    r"curl[^|]+\|\s*(sh|bash|zsh)",
    r"wget[^|]+\|\s*(sh|bash|zsh)",
    r"format[\s_-]*disk", r"wipe[\s_-]*device",
    r"drop[\s_-]*database", r"truncate[\s_-]*table",
    r"force[\s_-]*push", r"delete[\s_-]*repo", r"force[\s_-]*delete",
]
_DANGEROUS_RE = [re.compile(p, re.IGNORECASE) for p in _DANGEROUS_PATTERNS]


@dataclass
class ToolClassification:
    """Result of running :class:`ToolClassifier` against one MCP tool."""

    name: str
    kind: str  # "read" | "write" | "dangerous"
    reason: str = ""
    auto_approved: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "reason": self.reason,
                "auto_approved": self.auto_approved}


class ToolClassifier:
    """Classifies MCP tools as read / write / dangerous."""

    def __init__(self,
                 overrides: Optional[Dict[str, str]] = None,
                 auto_approve: Optional[List[str]] = None,
                 dangerous_extras: Optional[List[str]] = None) -> None:
        self.overrides = dict(overrides or {})
        self.auto_approve: Set[str] = set(auto_approve or [])
        extra_re = [re.compile(p, re.IGNORECASE) for p in (dangerous_extras or [])]
        self._dangerous_re = list(_DANGEROUS_RE) + extra_re

    # ------------------------------------------------------------------
    def classify(self, tool: Dict[str, Any]) -> ToolClassification:
        name = str((tool or {}).get("name", "")).strip()
        if not name:
            return ToolClassification(name="?", kind="dangerous",
                                      reason="empty tool name")

        # 1. Explicit override wins.
        if name in self.overrides:
            kind = self.overrides[name]
            if kind not in ("read", "write", "dangerous"):
                kind = "write"
            return ToolClassification(
                name=name, kind=kind,
                reason="override",
                # Dangerous classifications can never auto-approve, even
                # if the operator put the tool on the allowlist.
                auto_approved=(kind != "dangerous"
                                and name in self.auto_approve),
            )

        description = str((tool or {}).get("description", "")).lower()
        haystack = f"{name.lower()}\n{description}"

        # 2. Dangerous regex first.
        for rx in self._dangerous_re:
            if rx.search(haystack):
                return ToolClassification(
                    name=name, kind="dangerous",
                    reason=f"matched dangerous pattern '{rx.pattern}'",
                    auto_approved=False,  # never auto-approve dangerous
                )

        # 3. Verb classification — first token wins, then any token.
        bare_name = name.lower().replace("-", "_").replace("/", "_")
        tokens = [t for t in bare_name.split("_") if t]
        first_token = tokens[0] if tokens else ""
        if first_token in _WRITE_VERBS:
            return ToolClassification(
                name=name, kind="write",
                reason=f"verb '{first_token}' implies write",
                auto_approved=(name in self.auto_approve),
            )
        if first_token in _READ_VERBS:
            return ToolClassification(
                name=name, kind="read",
                reason=f"verb '{first_token}' implies read",
                auto_approved=False,
            )
        # Fall through: any token a write verb? (catches names like
        # ``safe_edit_file`` where the verb is in the middle.)
        for tok in tokens[1:]:
            if tok in _WRITE_VERBS:
                return ToolClassification(
                    name=name, kind="write",
                    reason=f"name contains write verb '{tok}'",
                    auto_approved=(name in self.auto_approve),
                )
            if tok in _READ_VERBS:
                return ToolClassification(
                    name=name, kind="read",
                    reason=f"name contains read verb '{tok}'",
                )

        # 4. Param-shape heuristic.
        schema = (tool or {}).get("inputSchema") or {}
        props = (schema or {}).get("properties") or {}
        param_names = {str(k).lower() for k in props.keys()}
        write_signals = {"content", "body", "data", "payload", "value",
                         "command", "cmd", "sql", "script"}
        if param_names & write_signals:
            return ToolClassification(
                name=name, kind="write",
                reason=f"params suggest mutation: {sorted(param_names & write_signals)}",
                auto_approved=(name in self.auto_approve),
            )

        # 5. Description verb fallback.
        for verb in _WRITE_VERBS:
            if re.search(rf"\b{verb}\b", description):
                return ToolClassification(
                    name=name, kind="write",
                    reason=f"description contains write verb '{verb}'",
                    auto_approved=(name in self.auto_approve),
                )

        # Default: read (least privilege from user POV — caller will
        # still pass through normal sandbox/policy on actual execution).
        return ToolClassification(
            name=name, kind="read", reason="no write signals found",
        )

    def classify_many(self, tools: List[Dict[str, Any]]) -> List[ToolClassification]:
        return [self.classify(t or {}) for t in tools or []]


# --------------------------------------------------------------------------
# Confirmation gate
# --------------------------------------------------------------------------
class ConfirmationRequired(Exception):
    """Raised when a write call needs interactive approval."""

    def __init__(self, call_id: str, server: str, tool: str,
                 arguments: Dict[str, Any], kind: str) -> None:
        super().__init__(
            f"confirmation required: {server}.{tool} (kind={kind})"
        )
        self.call_id = call_id
        self.server = server
        self.tool = tool
        self.arguments = arguments
        self.kind = kind


@dataclass
class _PendingCall:
    call_id: str
    server: str
    tool: str
    arguments: Dict[str, Any]
    kind: str
    created_at: float = field(default_factory=time.time)
    decision_event: threading.Event = field(default_factory=threading.Event)
    approved: bool = False
    decision_reason: str = ""


class ConfirmationGate:
    """Thread-safe pending-call register with optional blocking wait.

    Two usage modes:

    * **Non-blocking** (REST): :meth:`request` returns the ``call_id`` and
      raises :class:`ConfirmationRequired` immediately. The HTTP layer
      surfaces the prompt; later, an approval call invokes
      :meth:`resolve` and the original caller can re-issue the request
      with ``confirm=True``.
    * **Blocking** (CLI / interactive): pass ``wait_seconds > 0`` to
      :meth:`request_and_wait`; the caller blocks until ``resolve`` is
      called or the timeout elapses.
    """

    def __init__(self) -> None:
        self._pending: Dict[str, _PendingCall] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def list_pending(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = list(self._pending.values())
        return [{
            "call_id": p.call_id,
            "server": p.server,
            "tool": p.tool,
            "kind": p.kind,
            "arguments": p.arguments,
            "created_at": p.created_at,
            "age_seconds": round(time.time() - p.created_at, 2),
        } for p in rows]

    # ------------------------------------------------------------------
    def request(self, *, server: str, tool: str,
                arguments: Dict[str, Any], kind: str) -> str:
        """Register a pending call and raise ``ConfirmationRequired``."""
        call_id = uuid.uuid4().hex[:16]
        with self._lock:
            self._pending[call_id] = _PendingCall(
                call_id=call_id, server=server, tool=tool,
                arguments=dict(arguments or {}), kind=kind,
            )
        raise ConfirmationRequired(call_id, server, tool, arguments, kind)

    def request_and_wait(self, *, server: str, tool: str,
                          arguments: Dict[str, Any], kind: str,
                          wait_seconds: float) -> bool:
        """Block until the call is approved/denied or the timeout fires.

        Returns ``True`` if approved, ``False`` if denied/timed out.
        """
        call_id = uuid.uuid4().hex[:16]
        pending = _PendingCall(
            call_id=call_id, server=server, tool=tool,
            arguments=dict(arguments or {}), kind=kind,
        )
        with self._lock:
            self._pending[call_id] = pending

        ok = pending.decision_event.wait(timeout=max(0.0, wait_seconds))
        with self._lock:
            self._pending.pop(call_id, None)
        if not ok:
            return False
        return pending.approved

    # ------------------------------------------------------------------
    def resolve(self, call_id: str, *, approved: bool,
                reason: str = "") -> bool:
        with self._lock:
            pending = self._pending.get(call_id)
            if not pending:
                return False
            pending.approved = approved
            pending.decision_reason = reason or ("approved" if approved else "denied")
            pending.decision_event.set()
            # Remove resolved entries that no caller is waiting on
            # (request() variant). request_and_wait() pops after wake.
            if not pending.decision_event.is_set():
                self._pending.pop(call_id, None)
        return True

    def consume(self, call_id: str) -> Optional[_PendingCall]:
        """Atomically pop a pending call (used by a single REST round-trip)."""
        with self._lock:
            return self._pending.pop(call_id, None)


# Process-wide singleton — REST handlers and the bridge share one gate.
_GATE = ConfirmationGate()


def get_gate() -> ConfirmationGate:
    return _GATE


# --------------------------------------------------------------------------
# Network allowlist
# --------------------------------------------------------------------------
class AllowlistError(Exception):
    """Raised when an HTTP/SSE URL fails the allowlist check."""


class SensitiveRemoteArgumentsError(ValueError):
    """Remote tool arguments contain credentials, not public query data."""


# Do not classify public emails, ordinary prose or arbitrary numbers as secrets.
# Authentication headers are intentionally outside this tool-data boundary.
_REMOTE_CREDENTIAL_FIELDS = {
    "password", "passwd", "passphrase", "secret", "client_secret",
    "api_key", "apikey", "access_token", "refresh_token", "auth_token",
    "authorization", "bearer", "cookie", "cookies", "private_key",
    "ssh_private_key", "كلمة مرور", "كلمة السر",
}
_REMOTE_CREDENTIAL_PATTERNS = (
    re.compile(r"\bsk-[a-zA-Z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[a-zA-Z0-9_\-.+/=]{16,}"),
    re.compile(r"(?i)\b(?:api_key|apikey|access_token|refresh_token|client_secret)\s*[:=]\s*[\"']?[^\s\"']{16,}"),
    re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"),
)


def validate_remote_arguments(arguments: Any) -> None:
    """Reject recognized credentials before any remote MCP tool-data dispatch.

    This is a narrow deterministic secret guard, not a claim that arbitrary
    private documents/PII are detectable. Owners must decide what query data
    may leave the device. Intentional credential use belongs to an approved
    connector's authentication channel, never model-generated tool arguments.
    """
    remaining = [5000]
    def visit(value: Any, depth: int = 0) -> None:
        remaining[0] -= 1
        if depth > 20 or remaining[0] < 0:
            raise SensitiveRemoteArgumentsError("Remote MCP arguments exceed inspection limits")
        if isinstance(value, dict):
            from core.security.transient_vault import is_sensitive_key
            for key, item in value.items():
                # Common JSON spellings (apiKey, accessToken, private-key).
                normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
                normalized = re.sub(r"[\s\-]+", "_", normalized.lower())
                if normalized in _REMOTE_CREDENTIAL_FIELDS and is_sensitive_key(normalized) and item not in (None, "", [], {}):
                    raise SensitiveRemoteArgumentsError(
                        "Remote MCP credential arguments blocked; use an owner-controlled authenticated connector")
                # authorization/private_key are explicit credential fields but
                # are not all covered by the logging helper's keyword set.
                if normalized in {"authorization", "private_key", "ssh_private_key", "cookie", "cookies", "bearer"} and item not in (None, "", [], {}):
                    raise SensitiveRemoteArgumentsError(
                        "Remote MCP credential arguments blocked; use an owner-controlled authenticated connector")
                visit(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item, depth + 1)
        elif isinstance(value, str) and any(pattern.search(value) for pattern in _REMOTE_CREDENTIAL_PATTERNS):
            raise SensitiveRemoteArgumentsError(
                "Remote MCP credential arguments blocked; use an owner-controlled authenticated connector")
    visit(arguments)


def validate_http_url(url: str, allowed_domains: List[str]) -> None:
    """Enforce that ``url`` points at a domain in ``allowed_domains``.

    ``allowed_domains`` entries may be exact (``api.example.com``) or
    use a wildcard (``*.example.com``). An empty allowlist means
    "block all HTTP transports" — callers should populate it explicitly
    in ``mcp_servers.json``.
    """
    if not url:
        raise AllowlistError("empty url")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        raise AllowlistError("HTTP transport requires an http(s) URL without embedded credentials")
    host = (parsed.hostname or "").lower()
    if not host:
        raise AllowlistError(f"could not parse host from '{url}'")
    if not allowed_domains:
        raise AllowlistError(
            f"no allowed_domains configured for HTTP transport '{host}'"
        )
    for entry in allowed_domains:
        e = (entry or "").strip().lower()
        if not e:
            continue
        if fnmatch.fnmatch(host, e):
            return
    raise AllowlistError(
        f"host '{host}' not in allowlist {allowed_domains}",
    )


__all__ = [
    "AllowlistError",
    "ConfirmationGate",
    "ConfirmationRequired",
    "ToolClassifier",
    "ToolClassification",
    "get_gate",
    "validate_http_url",
    "validate_remote_arguments",
    "SensitiveRemoteArgumentsError",
]
