"""Account-backed Google tools.

Each tool is a thin wrapper around the relevant REST endpoint, with
three guarantees:

1. **Scope enforcement** — every tool declares ``REQUIRED_SCOPES``;
   if the linked account doesn't carry those scopes, the call returns
   ``{"ok": false, "error": "scope_missing", "needed": [...]}``.

2. **Token refresh** — before any call, we run the bundle through
   :func:`refresh_token_if_needed`, which transparently refreshes the
   access token via the refresh-token grant when expired.

3. **Account selection** — if no ``account`` is passed but exactly
   one Google account is linked, that one is used. Otherwise the user
   must pass ``account="user@example.com"``.

All tools return JSON-serialisable strings shaped
``{"ok": bool, "summary": str, ...}`` so the ReAct loop can render
them directly.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Any, Dict, Iterable, List, Optional

from .store import (
    OAuthAccount,
    list_accounts,
    refresh_token_if_needed,
)


_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"
_CAL_BASE = "https://www.googleapis.com/calendar/v3"
_DRIVE_BASE = "https://www.googleapis.com/drive/v3"
_DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"


# ---------------------------------------------------------------------------
# Account selection + scope checks
# ---------------------------------------------------------------------------
def _select_account(account: Optional[str] = None) -> str:
    if account:
        return account
    google_accts = [a for a in list_accounts() if a["provider"] == "google"]
    if len(google_accts) == 1:
        return google_accts[0]["account"]
    if not google_accts:
        raise RuntimeError("no Google account linked; run /admin/oauth/link/start")
    raise RuntimeError(
        "multiple Google accounts linked; pass account=<email>")


def _ensure(account: str, needed: Iterable[str]) -> OAuthAccount:
    acct = refresh_token_if_needed("google", account)
    if acct is None:
        raise RuntimeError(f"google account '{account}' is not linked")
    missing = [s for s in needed if s not in acct.scopes]
    if missing:
        raise PermissionError(json.dumps({
            "error": "scope_missing",
            "needed": missing,
            "have": list(acct.scopes),
        }))
    return acct


def _err(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        try:
            payload = json.loads(str(exc))
        except Exception:
            payload = {"error": str(exc)}
        return json.dumps({"ok": False,
                           "summary": "scope missing",
                           **payload})
    return json.dumps({"ok": False,
                        "summary": "google api error",
                        "error": str(exc)})


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def _request(method: str,
              url: str,
              acct: OAuthAccount,
              *,
              json_body: Optional[Any] = None,
              raw_body: Optional[bytes] = None,
              extra_headers: Optional[Dict[str, str]] = None,
              timeout: int = 20,
              ) -> Dict[str, Any]:
    headers = {
        "Authorization": f"{acct.token_type} {acct.access_token}",
        "Accept": "application/json",
    }
    body: Optional[bytes] = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        body = raw_body
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8") or "{}"
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------
GMAIL_SEND_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
GMAIL_READ_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def gmail_send(*,
                to: str,
                subject: str,
                body: str,
                account: Optional[str] = None,
                cc: str = "",
                bcc: str = "",
                **_) -> str:
    """Send a plaintext email via Gmail."""
    try:
        acct_id = _select_account(account)
        acct = _ensure(acct_id, GMAIL_SEND_SCOPES)
        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = acct_id
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
        resp = _request(
            "POST",
            f"{_GMAIL_BASE}/users/me/messages/send",
            acct,
            json_body={"raw": raw})
        return json.dumps({
            "ok": True,
            "summary": f"sent to {to}",
            "message_id": resp.get("id"),
            "thread_id": resp.get("threadId"),
        })
    except Exception as exc:
        return _err(exc)


def gmail_search(*,
                  query: str,
                  max_results: int = 10,
                  account: Optional[str] = None,
                  **_) -> str:
    """Search messages with a Gmail query (e.g. 'from:foo subject:bar')."""
    try:
        acct_id = _select_account(account)
        acct = _ensure(acct_id, GMAIL_READ_SCOPES)
        params = urllib.parse.urlencode({
            "q": query,
            "maxResults": max(1, min(max_results, 50)),
        })
        resp = _request(
            "GET",
            f"{_GMAIL_BASE}/users/me/messages?{params}",
            acct)
        messages = resp.get("messages", []) or []
        return json.dumps({
            "ok": True,
            "summary": f"{len(messages)} message(s)",
            "messages": messages,
        })
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------
CAL_READ_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
CAL_WRITE_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def calendar_create_event(*,
                            summary: str,
                            start: str,
                            end: str,
                            description: str = "",
                            calendar_id: str = "primary",
                            account: Optional[str] = None,
                            **_) -> str:
    """Create an event on the user's calendar.

    ``start`` / ``end`` accept full RFC3339 strings (e.g.
    ``2025-01-02T09:00:00-05:00``).
    """
    try:
        acct_id = _select_account(account)
        acct = _ensure(acct_id, CAL_WRITE_SCOPES)
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }
        resp = _request(
            "POST",
            f"{_CAL_BASE}/calendars/{urllib.parse.quote(calendar_id)}/events",
            acct,
            json_body=body)
        return json.dumps({
            "ok": True,
            "summary": f"created '{summary}'",
            "event_id": resp.get("id"),
            "html_link": resp.get("htmlLink"),
        })
    except Exception as exc:
        return _err(exc)


def calendar_list_events(*,
                          time_min: str = "",
                          time_max: str = "",
                          calendar_id: str = "primary",
                          max_results: int = 10,
                          account: Optional[str] = None,
                          **_) -> str:
    """List up to ``max_results`` events; both ranges are RFC3339."""
    try:
        acct_id = _select_account(account)
        acct = _ensure(acct_id, CAL_READ_SCOPES)
        params: Dict[str, Any] = {
            "maxResults": max(1, min(max_results, 50)),
            "orderBy": "startTime",
            "singleEvents": "true",
        }
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max
        url = (f"{_CAL_BASE}/calendars/{urllib.parse.quote(calendar_id)}"
               f"/events?{urllib.parse.urlencode(params)}")
        resp = _request("GET", url, acct)
        items: List[Dict[str, Any]] = resp.get("items", []) or []
        return json.dumps({
            "ok": True,
            "summary": f"{len(items)} event(s)",
            "events": items,
        })
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Drive
# ---------------------------------------------------------------------------
DRIVE_READ_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DRIVE_FILE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def drive_list_files(*,
                      query: str = "",
                      max_results: int = 10,
                      account: Optional[str] = None,
                      **_) -> str:
    """List Drive files; ``query`` is a Drive search expression."""
    try:
        acct_id = _select_account(account)
        acct = _ensure(acct_id, DRIVE_READ_SCOPES)
        params = {
            "pageSize": max(1, min(max_results, 50)),
            "fields": "files(id,name,mimeType,modifiedTime,size)",
        }
        if query:
            params["q"] = query
        url = f"{_DRIVE_BASE}/files?{urllib.parse.urlencode(params)}"
        resp = _request("GET", url, acct)
        files = resp.get("files", []) or []
        return json.dumps({
            "ok": True,
            "summary": f"{len(files)} file(s)",
            "files": files,
        })
    except Exception as exc:
        return _err(exc)


def drive_upload(*,
                  filename: str,
                  data_b64: str,
                  mime_type: str = "application/octet-stream",
                  account: Optional[str] = None,
                  **_) -> str:
    """Upload an arbitrary file (base64-encoded payload) to My Drive."""
    try:
        acct_id = _select_account(account)
        acct = _ensure(acct_id, DRIVE_FILE_SCOPES)
        try:
            payload = base64.b64decode(data_b64)
        except Exception:
            return json.dumps({
                "ok": False,
                "summary": "data_b64 is not valid base64"})

        boundary = "===supergent_drive_boundary==="
        meta = json.dumps({"name": filename}).encode("utf-8")
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        ).encode("utf-8") + meta + (
            f"\r\n--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + payload + (
            f"\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        url = (f"{_DRIVE_UPLOAD}/files?uploadType=multipart"
               "&fields=id,name,webViewLink")
        resp = _request(
            "POST",
            url,
            acct,
            raw_body=body,
            extra_headers={
                "Content-Type": f"multipart/related; boundary={boundary}",
            })
        return json.dumps({
            "ok": True,
            "summary": f"uploaded '{filename}'",
            "file_id": resp.get("id"),
            "name": resp.get("name"),
            "web_view_link": resp.get("webViewLink"),
        })
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Tool registry hook
# ---------------------------------------------------------------------------
GOOGLE_TOOLS: Dict[str, Any] = {
    "google.gmail.send": gmail_send,
    "google.gmail.search": gmail_search,
    "google.calendar.create_event": calendar_create_event,
    "google.calendar.list_events": calendar_list_events,
    "google.drive.list_files": drive_list_files,
    "google.drive.upload": drive_upload,
}


def register_google_tools(registry: Any) -> List[str]:
    """Register every Google tool with the agent's tool registry.

    Returns the list of tool names that were freshly registered (so we
    can log a count). Existing registrations are left alone — this
    keeps the function idempotent across reloads.
    """
    if registry is None or not hasattr(registry, "register"):
        return []
    registered: List[str] = []
    for name, fn in GOOGLE_TOOLS.items():
        try:
            existing = (registry.get(name)
                         if hasattr(registry, "get") else None)
            if existing is not None:
                continue
            registry.register(name, fn)
            registered.append(name)
        except Exception:
            continue
    return registered


__all__ = [
    "GOOGLE_TOOLS",
    "register_google_tools",
    "gmail_send",
    "gmail_search",
    "calendar_create_event",
    "calendar_list_events",
    "drive_list_files",
    "drive_upload",
]
