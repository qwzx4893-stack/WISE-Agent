"""Acceptance reports must not leak secrets or silently accept stale evidence."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "qa/acceptance"))
    spec = importlib.util.spec_from_file_location("qa_" + name, ROOT / "qa/acceptance" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_security_staging_never_copies_raw_credentials(tmp_path, monkeypatch):
    module = load_script("run_acceptance", monkeypatch)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    (tmp_path / "memory").mkdir()
    (tmp_path / "config").mkdir()
    secret = "synthetic-private-value-not-for-reports"
    (tmp_path / "config/key.json").write_text(secret)
    (tmp_path / "AGENTS.md").write_text(secret)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text(secret)
    descriptors = [{"name":secret, "enabled":True, "command":"node", "args":[secret],
        "url":"https://user:" + secret + "@mcp.example/mcp?token=" + secret,
        "env":{"TOKEN":secret}, "headers":{"Authorization":secret}, "description":secret}]
    (tmp_path / "memory/mcp_servers.json").write_text(json.dumps(descriptors))
    destination = tmp_path / "safe-staging"
    module.prepare_agentshield_input(destination)
    files = list(destination.rglob("*"))
    assert {path.name for path in files} == {".mcp.json", "SCAN_SCOPE.md"}
    assert all(secret not in path.read_text() for path in files)
    config = json.loads((destination / ".mcp.json").read_text())["mcpServers"]["server-1"]
    assert config["command"] == "node" and config["url"] == "https://mcp.example/mcp"
    with pytest.raises(ValueError, match="must be empty"):
        module.prepare_agentshield_input(destination)


def test_evidence_gate_requires_current_build_and_reproduction_assets(tmp_path, monkeypatch):
    module = load_script("quality_gate", monkeypatch)
    screenshot, log = tmp_path / "screen.png", tmp_path / "run.log"
    screenshot.write_bytes(b"test-fixture")
    log.write_text("test fixture")
    case = {"id":"required-case", "status":"PASS", "steps":["Open settings"],
        "screenshot":str(screenshot), "log":str(log)}
    report = {"cases":[case], "source_stamp":"current", "source_changed_during_run":False}
    assert not module.inspect_report(report, {"required-case"}, "current")
    assert module.inspect_report(report, {"required-case"}, "different")
    assert module.inspect_report(report, {"missing-case"}, "current")
    case["steps"] = []
    assert module.inspect_report(report, {"required-case"}, "current")
    case["steps"] = ["Open settings"]
    screenshot.unlink()
    assert module.inspect_report(report, {"required-case"}, "current")


def test_log_redaction_removes_bearer_and_json_tokens(monkeypatch):
    module = load_script("run_acceptance", monkeypatch)
    secret = "synthetic-token-do-not-publish"
    value = 'Authorization: Bearer ' + secret + '\n{"access_token":"' + secret + '", "api_key":"' + secret + '"}'
    assert secret not in module.redact(value)


def test_source_stamp_includes_metadata_but_not_installed_venv(tmp_path,monkeypatch):
    module=load_script("source_stamp",monkeypatch)
    monkeypatch.setattr(module,"ROOT",tmp_path)
    for directory in ("core/integrations","core/mcp/.venv","config"):
        (tmp_path/directory).mkdir(parents=True,exist_ok=True)
    for name in ("requirements.txt","requirements-intelligence.txt","config/intelligence_resources.json"):
        (tmp_path/name).write_text("fixture")
    metadata=tmp_path/"core/integrations/metadata.json"
    metadata.write_text('{"description":"first"}')
    before=module.source_stamp()
    (tmp_path/"core/mcp/.venv/installed.py").write_text("not app source")
    assert module.source_stamp()==before
    metadata.write_text('{"description":"changed"}')
    assert module.source_stamp()!=before


def test_execution_pass_cannot_replace_semantic_correctness(tmp_path, monkeypatch):
    module = load_script("quality_gate", monkeypatch)
    path = (tmp_path / "report.json").resolve()
    review = {"source_stamp": "current", "project_report": str(path), "research_verdict": "FAIL",
              "checked_claims": [{"verdict": "UNSUPPORTED_ATTRIBUTION"}],
              "findings": [{"id": "unsupported-claim", "status": "OPEN", "blocking": True}]}
    failures = module.inspect_semantic_review(review, "current", path)
    assert any("grounding" in item for item in failures)
    assert any("unsupported-claim" in item for item in failures)
    review.update(research_verdict="PASS", findings=[])
    assert module.inspect_semantic_review(review, "current", path)
    review["checked_claims"] = [{"verdict": "SUPPORTED"}]
    assert not module.inspect_semantic_review(review, "current", path)
    assert module.inspect_semantic_review(review, "different", path)
    assert module.inspect_semantic_review(review, "current", tmp_path / "other.json")
    review["checked_claims"] = []
    assert module.inspect_semantic_review(review, "current", path)


@pytest.mark.parametrize("foreign", [False, True])
def test_owned_runtime_health_identity_is_verified(tmp_path, monkeypatch, foreign):
    import hashlib
    from types import SimpleNamespace
    module = load_script("user_journeys", monkeypatch)
    runtime = module.Runtime(tmp_path)
    expected = hashlib.sha256(str(runtime.root.resolve()).casefold().encode()).hexdigest()[:16]
    calls = []

    def get(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise module.httpx.ConnectError("Synthetic idle owned test port")
        return SimpleNamespace(status_code=200, json=lambda: {
            "service": "wise", "runtime_id": "foreign-runtime" if foreign else expected, "status": "ok"})

    monkeypatch.setattr(module.httpx, "get", get)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(poll=lambda: None))
    try:
        if foreign:
            with pytest.raises(RuntimeError, match="does not belong"):
                runtime.start()
        else:
            runtime.start()
        assert len(calls) == 2
    finally:
        for handle in runtime.handles:
            handle.close()


def test_owned_process_resources_include_launcher_children_not_unrelated_processes(monkeypatch):
    from types import SimpleNamespace
    module = load_script("process_resources", monkeypatch)

    class Process:
        def __init__(self, pid, rss, children=()): self.pid, self.rss, self.descendants = pid, rss, children
        def create_time(self): return self.pid + .25
        def is_running(self): return True
        def children(self, recursive): assert recursive; return self.descendants
        def memory_info(self): return SimpleNamespace(rss=self.rss)
        def cpu_percent(self, interval): assert interval is None; return 2

    child = Process(102, 200 * 1024**2)
    launcher = Process(101, 10 * 1024**2, [child])
    monkeypatch.setattr(module.psutil, "Process", lambda pid: launcher if pid == 101 else pytest.fail("Unexpected PID access"))
    sampler = module.OwnedProcessSampler(101)
    first = sampler.sample()
    assert first["rss_mb"] == 210 and first["cpu_percent"] is None
    second = sampler.sample()
    assert second["cpu_percent"] == 4 and second["scope"] == "OWNED_BACKEND_PROCESS_TREE"
    assert {row["pid"] for row in second["processes"]} == {101, 102}
    monkeypatch.setattr(launcher, "is_running", lambda: False)
    with pytest.raises(RuntimeError, match="exited"): sampler.sample()
