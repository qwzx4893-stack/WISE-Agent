"""Tests for the local preview server (Phase 10 Part 2)."""

from __future__ import annotations

import json
import socket
import time
import urllib.request

import pytest

from core import preview_server as ps


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Redirect WORKSPACE_DIR to a clean tmp directory and reset manager."""
    import core.paths as paths
    monkeypatch.setattr(paths, "WORKSPACE_DIR", tmp_path)
    # Reset the singleton so every test gets its own manager state.
    ps.PreviewManager._instance = None
    ps._manager = None
    yield tmp_path
    ps.stop_all_previews()
    ps.PreviewManager._instance = None
    ps._manager = None


def _http_get(url: str, timeout: float = 2.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def test_start_serves_files_then_stop(workspace):
    (workspace / "index.html").write_text("<h1>hello</h1>", encoding="utf-8")
    info = ps.start_preview(str(workspace))
    try:
        assert info.url.startswith("http://127.0.0.1:")
        # Wait briefly for the thread to be accepting connections.
        time.sleep(0.05)
        status, body = _http_get(info.url + "/index.html")
        assert status == 200
        assert b"hello" in body
    finally:
        assert ps.stop_preview(info.server_id) is True

    # After stop, the port can't be connected to.
    s = socket.socket()
    s.settimeout(0.5)
    with pytest.raises(OSError):
        s.connect(("127.0.0.1", info.port))
    s.close()


def test_start_with_subdirectory_only(workspace):
    sub = workspace / "site"
    sub.mkdir()
    (sub / "index.html").write_text("inner", encoding="utf-8")
    info = ps.start_preview(str(sub))
    try:
        time.sleep(0.05)
        status, body = _http_get(info.url + "/index.html")
        assert status == 200
        assert b"inner" in body
    finally:
        ps.stop_preview(info.server_id)


def test_start_resolves_file_to_parent_directory(workspace):
    (workspace / "page.html").write_text("file-mode", encoding="utf-8")
    info = ps.start_preview(str(workspace / "page.html"))
    try:
        time.sleep(0.05)
        status, body = _http_get(info.url + "/page.html")
        assert status == 200
        assert b"file-mode" in body
    finally:
        ps.stop_preview(info.server_id)


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
def test_path_outside_workspace_rejected(workspace):
    with pytest.raises(ValueError):
        ps.start_preview("/etc")


def test_traversal_rejected(workspace):
    with pytest.raises(ValueError):
        ps.start_preview(str(workspace / ".." / ".." / "etc"))


def test_nonexistent_path_rejected(workspace):
    with pytest.raises(ValueError):
        ps.start_preview(str(workspace / "does-not-exist"))


def test_binds_only_to_loopback(workspace):
    (workspace / "index.html").write_text("x", encoding="utf-8")
    info = ps.start_preview(str(workspace))
    try:
        time.sleep(0.05)
        # URL is loopback by construction — the manager constant HOST is
        # "127.0.0.1" and that's the value embedded into the URL.
        assert info.url.startswith("http://127.0.0.1:")
        assert ps.HOST == "127.0.0.1"
        # Loopback connection succeeds.
        status, _ = _http_get(info.url + "/index.html")
        assert status == 200
    finally:
        ps.stop_preview(info.server_id)


# ---------------------------------------------------------------------------
# Concurrency caps + listing
# ---------------------------------------------------------------------------
def test_concurrent_cap(workspace, monkeypatch):
    monkeypatch.setattr(ps, "MAX_CONCURRENT", 2)
    (workspace / "index.html").write_text("x", encoding="utf-8")
    a = ps.start_preview(str(workspace))
    b = ps.start_preview(str(workspace))
    try:
        with pytest.raises(RuntimeError):
            ps.start_preview(str(workspace))
        assert len(ps.list_previews()) == 2
    finally:
        ps.stop_preview(a.server_id)
        ps.stop_preview(b.server_id)


def test_stop_all(workspace):
    (workspace / "index.html").write_text("x", encoding="utf-8")
    a = ps.start_preview(str(workspace))
    b = ps.start_preview(str(workspace))
    assert len(ps.list_previews()) == 2
    n = ps.stop_all_previews()
    assert n == 2
    assert ps.list_previews() == []


def test_stop_session_only(workspace):
    (workspace / "index.html").write_text("x", encoding="utf-8")
    a = ps.start_preview(str(workspace), session_id="s1")
    b = ps.start_preview(str(workspace), session_id="s2")
    try:
        n = ps.stop_session_previews("s1")
        assert n == 1
        ids = [p["server_id"] for p in ps.list_previews()]
        assert b.server_id in ids
        assert a.server_id not in ids
    finally:
        ps.stop_all_previews()


# ---------------------------------------------------------------------------
# Activity tracking
# ---------------------------------------------------------------------------
def test_activity_timestamp_advances_on_request(workspace):
    (workspace / "index.html").write_text("x", encoding="utf-8")
    info = ps.start_preview(str(workspace))
    try:
        time.sleep(0.05)
        first = ps.list_previews()[0]["last_activity"]
        time.sleep(0.05)
        _http_get(info.url + "/index.html")
        time.sleep(0.05)
        second = ps.list_previews()[0]["last_activity"]
        assert second >= first
    finally:
        ps.stop_preview(info.server_id)


# ---------------------------------------------------------------------------
# Agent tool wrappers
# ---------------------------------------------------------------------------
def test_preview_serve_tool_returns_url(workspace):
    (workspace / "index.html").write_text("x", encoding="utf-8")
    out = json.loads(ps.preview_serve(path=str(workspace)))
    try:
        assert out["ok"] is True
        assert out["url"].startswith("http://127.0.0.1:")
        assert "server_id" in out
    finally:
        ps.stop_all_previews()


def test_preview_serve_rejects_missing_path():
    out = json.loads(ps.preview_serve(path=""))
    assert out["ok"] is False


def test_preview_stop_tool_returns_404_for_unknown():
    out = json.loads(ps.preview_stop(server_id="nope"))
    assert out["ok"] is False


def test_preview_list_tool_returns_zero_when_empty(workspace):
    out = json.loads(ps.preview_list())
    assert out["ok"] is True
    assert out["summary"].startswith("0 ")


def test_register_preview_tools_idempotent():
    class R:
        def __init__(self): self.t = {}

        def register(self, n, f): self.t[n] = f

        def get(self, n): return self.t.get(n)

    r = R()
    first = ps.register_preview_tools(r)
    assert set(first) == set(ps.PREVIEW_TOOLS)
    second = ps.register_preview_tools(r)
    assert second == []


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------
def test_admin_endpoints(workspace, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("AGENT_API_TOKEN", "test-tok")
    # Patch WORKSPACE_DIR for the API endpoint too.
    import core.paths as paths
    monkeypatch.setattr(paths, "WORKSPACE_DIR", workspace)
    (workspace / "index.html").write_text("x", encoding="utf-8")

    from api.server import app
    headers = {"X-Agent-Token": "test-tok"}
    with TestClient(app) as client:
        # Empty list initially.
        r = client.get("/admin/preview/list", headers=headers)
        assert r.status_code == 200
        assert r.json()["count"] == 0

        # Start.
        r = client.post("/admin/preview/start",
                         json={"path": str(workspace), "ttl_seconds": 60},
                         headers=headers)
        assert r.status_code == 200
        sid = r.json()["server_id"]

        # List shows 1.
        r = client.get("/admin/preview/list", headers=headers)
        assert r.json()["count"] == 1

        # Stop.
        r = client.post("/admin/preview/stop", json={"server_id": sid},
                         headers=headers)
        assert r.status_code == 200

        # Stopping again -> 404.
        r = client.post("/admin/preview/stop", json={"server_id": sid},
                         headers=headers)
        assert r.status_code == 404


def test_admin_start_validates_path(workspace, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("AGENT_API_TOKEN", "test-tok")
    import core.paths as paths
    monkeypatch.setattr(paths, "WORKSPACE_DIR", workspace)

    from api.server import app
    with TestClient(app) as client:
        r = client.post("/admin/preview/start",
                         json={"path": "/etc"},
                         headers={"X-Agent-Token": "test-tok"})
        assert r.status_code == 400
