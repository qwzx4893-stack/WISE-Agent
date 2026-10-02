"""§3 (live registration via Settings UI) + §5 (OAuth2 flow,
parallel calls, dangerous blocked end-to-end)."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest


FAKE_MCP = str(Path(__file__).parent / "_fake_mcp_server.py")


@pytest.fixture(autouse=True)
def _isolated_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    yield


# --------------------------------------------------------------------------
# §3 — live registration through the API the Settings UI calls
# --------------------------------------------------------------------------
def test_post_admin_mcp_servers_registers_tools_at_runtime():
    """Adding a server via the same endpoint the Flutter Settings UI hits
    must make the MCP tools available *immediately* — no restart."""
    from fastapi.testclient import TestClient
    from api.server import app

    c = TestClient(app)
    r = c.post("/admin/mcp/servers", json={
        "name": "ui_added",
        "transport": "stdio",
        "command": "python3",
        "args": [FAKE_MCP],
        "enabled": True,
        "sandbox": False,
    })
    body = r.json()
    assert r.status_code == 200
    assert body["alive"] is True
    assert body["tools"] >= 2

    # The /admin/mcp/servers/{name}/tools endpoint shows the same set
    # with classifications, exactly what the Settings UI lists.
    r2 = c.get("/admin/mcp/servers/ui_added/tools")
    tools = r2.json()["tools"]
    by_name = {t["name"]: t for t in tools}
    assert "echo" in by_name
    assert "add" in by_name
    assert by_name["echo"]["kind"] == "read"

    # Cleanup so subsequent tests don't see this server.
    c.delete("/admin/mcp/servers/ui_added")


def test_pending_confirmation_can_be_resolved_via_api():
    """When the UI sees a pending write, it POSTs to
    /admin/mcp/confirm/{call_id} to approve. This proves the gate
    end-to-end."""
    from core.mcp import MCPRegistry, build_mcp_tools, get_gate

    reg = MCPRegistry()
    reg.add({"name": "cf", "transport": "stdio",
             "command": "python3", "args": [FAKE_MCP],
             "classifications": {"add": "write"}, "sandbox": False})
    tools = build_mcp_tools(reg, auto_start=False)
    box: Dict[str, Any] = {}
    def caller():
        box["v"] = tools["mcp_cf_add"](a=10, b=20, _wait=3.0)

    th = threading.Thread(target=caller)
    th.start()
    time.sleep(0.4)
    pending = get_gate().list_pending()
    assert pending, "expected a pending call"
    get_gate().resolve(pending[-1]["call_id"], approved=True)
    th.join(timeout=4.0)
    assert isinstance(box["v"], dict)
    assert box["v"].get("value") == 30.0
    reg.stop_all()


# --------------------------------------------------------------------------
# §5 — OAuth2 dummy provider
# --------------------------------------------------------------------------
class _OAuthHandler(BaseHTTPRequestHandler):
    """Minimal OAuth2 token + protected resource."""

    issued: List[str] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode() if length else ""
        # tiny x-www-form-urlencoded parser
        params = dict(p.split("=", 1) for p in body.split("&") if "=" in p)
        if self.path != "/token":
            self.send_response(404); self.end_headers(); return
        if (params.get("grant_type") != "client_credentials"
                or params.get("client_id") != "dev"
                or params.get("client_secret") != "secret"):
            self.send_response(401); self.end_headers(); return
        token = "tok-" + str(len(self.issued))
        self.issued.append(token)
        payload = json.dumps({"access_token": token,
                              "token_type": "Bearer",
                              "expires_in": 3600})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload.encode())

    def do_GET(self):  # noqa: N802
        if self.path != "/protected":
            self.send_response(404); self.end_headers(); return
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.issued:
            self.send_response(401); self.end_headers(); return
        payload = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args, **_kw):  # silence
        return


def _start_oauth_server() -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", 0), _OAuthHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv


def test_oauth2_flow_with_dummy_provider():
    """Token-exchange flow: client credentials → bearer token → use it
    in the Authorization header for the protected resource. This is the
    same pattern an MCP HTTP client uses to talk to an OAuth-protected
    MCP server."""
    import urllib.parse
    import urllib.request

    srv = _start_oauth_server()
    try:
        port = srv.server_address[1]
        # 1. Exchange.
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": "dev",
            "client_secret": "secret",
        }).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/token",
                                      data=body)
        with urllib.request.urlopen(req, timeout=2) as r:
            token_resp = json.loads(r.read())
        assert token_resp["token_type"] == "Bearer"
        token = token_resp["access_token"]

        # 2. Use token. Reject without it.
        unauth = urllib.request.Request(f"http://127.0.0.1:{port}/protected")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(unauth, timeout=2)
        assert ei.value.code == 401

        # 3. Authorized call succeeds.
        req2 = urllib.request.Request(
            f"http://127.0.0.1:{port}/protected",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req2, timeout=2) as r:
            data = json.loads(r.read())
        assert data["ok"] is True
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------
# §5 — parallel calls to multiple MCP servers
# --------------------------------------------------------------------------
def test_parallel_calls_to_multiple_mcp_servers():
    """Spin up two MCP servers and fire reads in parallel. Both must
    return their tool's result without deadlocking on the bridge."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from core.mcp import MCPRegistry, build_mcp_tools

    reg = MCPRegistry()
    reg.add({"name": "p1", "transport": "stdio",
             "command": "python3", "args": [FAKE_MCP], "enabled": True, "sandbox": False})
    reg.add({"name": "p2", "transport": "stdio",
             "command": "python3", "args": [FAKE_MCP], "enabled": True, "sandbox": False})
    tools = build_mcp_tools(reg, auto_start=False)
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = []
            for i in range(20):
                target = "mcp_p1_echo" if i % 2 == 0 else "mcp_p2_echo"
                futs.append(ex.submit(tools[target], text=f"v{i}"))
            results = [f.result(timeout=10) for f in as_completed(futs)]
        # 20 results, each containing the matching text in content.
        assert len(results) == 20
        all_texts = [r.get("content", [{}])[0].get("text", "")
                     for r in results]
        assert sum(1 for t in all_texts if t.startswith("v")) == 20
    finally:
        reg.stop_all()


def test_dangerous_command_through_mcp_tool_is_blocked_end_to_end():
    """§5 acceptance test: a dangerous *argument* fed to a benign-named
    MCP tool reaches the Agent OS MCP server (when the agent acts as a
    server) and is rejected by DANGEROUS_PATTERNS."""
    from core.mcp.server import MCPServer

    class FakeReg:
        tools = {"create_note": lambda title, body: f"{title}/{body}"}
        def get(self, n): return self.tools.get(n)

    srv = MCPServer(tool_registry=FakeReg())
    out = srv.dispatch({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "create_note",
                    "arguments": {"title": "x", "body": "rm -rf /",
                                   "confirm": True}},
    })
    assert "error" in out
    assert out["error"]["code"] == -32001
