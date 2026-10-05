"""Git argument/result regressions; never invoke Git or any remote service."""
from types import SimpleNamespace

import pytest


@pytest.fixture
def clone_boundary(tmp_path, monkeypatch):
    from core import tools_bridge, capability_router
    from core.security.security_gate import WindowsSecurityGate
    monkeypatch.setattr(tools_bridge, "WORKSPACE_DIR", tmp_path)
    gate = WindowsSecurityGate(audit_log_path=tmp_path / "security.jsonl")
    monkeypatch.setattr(capability_router, "get_security_gate", lambda: gate)
    calls = []
    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="cloned fixture", stderr="")
    monkeypatch.setattr(tools_bridge.subprocess, "run", fake_run)
    router = object.__new__(capability_router.CapabilityRouter)
    router._capabilities = {}
    router._load_native_capabilities()
    return tools_bridge, router, calls


@pytest.mark.parametrize("repo", ["--config=core.sshCommand=echo", "-o", "", "  ",
                                  "https://example.invalid/repo\n--option", "repo\x00", "ext::echo harmless"])
def test_git_clone_rejects_option_and_helper_inputs_without_process(clone_boundary, repo):
    _, router, calls = clone_boundary
    result = router.execute("native.git_clone", {"repo": repo, "dest": "fixture"})
    assert not result.success
    assert calls == []


def test_git_clone_uses_option_terminator_and_workspace_destination(clone_boundary):
    bridge, router, calls = clone_boundary
    result = router.execute("native.git_clone", {"repo": " https://example.invalid/fixture.git ", "dest": "fixture"})
    assert result.success
    assert calls[0][0] == ["git", "clone", "--", "https://example.invalid/fixture.git", str(bridge.WORKSPACE_DIR / "fixture")]
    assert calls[0][1]["capture_output"] is True


def test_git_clone_nonzero_exit_is_structured_failure(clone_boundary, monkeypatch):
    bridge, router, _ = clone_boundary
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=128, stdout="", stderr="fixture remote rejected"))
    result = router.execute("native.git_clone", {"repo": "https://example.invalid/fixture.git", "dest": "fixture"})
    assert not result.success
    assert "code 128" in result.error
    assert result.output is None


def test_git_clone_timeout_and_missing_binary_are_failures(clone_boundary, monkeypatch):
    bridge, router, _ = clone_boundary
    def timeout(*a, **kw):
        raise bridge.subprocess.TimeoutExpired(a[0], 1)
    monkeypatch.setattr(bridge.subprocess, "run", timeout)
    assert "timed out" in router.execute("native.git_clone", {"repo": "fixture", "dest": "fixture"}).error
    def missing(*a, **kw):
        raise FileNotFoundError("fixture git executable absent")
    monkeypatch.setattr(bridge.subprocess, "run", missing)
    result = router.execute("native.git_clone", {"repo": "fixture", "dest": "fixture"})
    assert not result.success
    assert "absent" in result.error


def test_git_clone_cannot_write_outside_workspace(clone_boundary):
    _, router, calls = clone_boundary
    result = router.execute("native.git_clone", {"repo": "fixture", "dest": "../outside-fixture"})
    assert not result.success
    assert "OUTSIDE WORKSPACE" in result.error
    assert calls == []
