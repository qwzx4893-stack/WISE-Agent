"""Regressions for confirmed acceptance findings; no paid calls or destructive actions."""
from types import SimpleNamespace
from pathlib import Path
import json
import zipfile

import pytest


@pytest.mark.parametrize("action,params", [
    ("run_shell", {"command": "echo test"}),
    ("execute_python", {"code": "print('test')"}),
])
def test_raw_code_requires_authorization(action, params):
    from core.security.security_gate import WindowsSecurityGate, SecurityContext
    gate = WindowsSecurityGate()
    assert not gate.evaluate(action, params).allowed
    assert not gate.evaluate(action, params, SecurityContext(is_untrusted_content=True, confirmed=True)).allowed
    assert gate.evaluate(action, params, SecurityContext(confirmed=True)).allowed


def test_direct_builtin_cannot_bypass_gate():
    from core.tools_bridge import _run_shell, _execute_python
    with pytest.raises(PermissionError):
        _run_shell({"command": "echo test"})
    with pytest.raises(PermissionError):
        _execute_python({"code": "print('test')"})


def test_confirmed_failed_command_does_not_report_success(monkeypatch, tmp_path):
    from core.tools_bridge import _run_shell, builtin_security_context
    from core.security.security_gate import SecurityContext
    monkeypatch.setattr("core.tools_bridge.WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr("core.tools_bridge.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=9, stderr="failed", stdout=""))
    args = {"command": "echo test"}
    with builtin_security_context(SecurityContext(confirmed=True), "run_shell", args):
        with pytest.raises(RuntimeError, match="code 9"):
            _run_shell(args)


@pytest.mark.parametrize("target", ["notepad.exe & echo test", "notepad.exe|cmd", "notepad.exe\ncmd"])
def test_app_launcher_rejects_shell_syntax(target):
    from core.security.security_gate import WindowsSecurityGate
    assert not WindowsSecurityGate().evaluate("open_app", {"app": target}).allowed


def test_app_launcher_cannot_hide_interpreter_command():
    from core.windows.computer_control import ComputerControl
    result = ComputerControl().open_app('cmd.exe /c echo test')
    assert result["success"] is False
    assert result.get("requires_confirmation")


def test_mcp_does_not_inherit_provider_secrets(monkeypatch):
    from core.mcp.client import MCPClient, MCPError
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-secret")
    monkeypatch.setenv("WISE_SESSION_TOKEN", "test-only-session")
    captured = {}
    def capture(*args, **kwargs):
        captured.update(kwargs["env"])
        raise FileNotFoundError("probe: process intentionally not started")
    monkeypatch.setattr("core.mcp.client.subprocess.Popen", capture)
    client = MCPClient(name="probe", transport="stdio", command="missing-probe", sandbox=False, env={"EXPLICIT_KEY": "scoped"})
    with pytest.raises(MCPError):
        client._open_stdio()
    assert "OPENROUTER_API_KEY" not in captured
    assert "WISE_SESSION_TOKEN" not in captured
    assert captured["EXPLICIT_KEY"] == "scoped"
    assert "PYTHONPATH" in captured


def test_http_mcp_supports_sessions_sse_and_empty_notification(monkeypatch):
    import httpx
    from core.mcp import client as module
    requests = []
    def handle(request):
        requests.append(request)
        frame = json.loads(request.content)
        if "id" not in frame:
            return httpx.Response(202)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream", "Mcp-Session-Id": "session-one"},
                              text='data: ' + json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": {"ok": True}}) + '\n\n')
    factory = httpx.Client
    monkeypatch.setattr(module, "_httpx", SimpleNamespace(Client=lambda **kw: factory(transport=httpx.MockTransport(handle), **kw)))
    client = module.MCPClient(name="probe", transport="http", url="https://example.com/mcp", allowed_domains=["example.com"])
    assert client._http_post({"jsonrpc": "2.0", "id": 1, "method": "initialize"})["result"]["ok"]
    assert client._http_post({"jsonrpc": "2.0", "method": "notifications/initialized"}) == {}
    assert requests[1].headers["Mcp-Session-Id"] == "session-one"
    client.close()
    assert client._http_session_id is None


@pytest.mark.parametrize("url", ["ftp://example.com/mcp", "https://user:password@example.com/mcp"])
def test_http_mcp_rejects_unsafe_url(url):
    from core.mcp.policy import validate_http_url, AllowlistError
    with pytest.raises(AllowlistError):
        validate_http_url(url, ["example.com"])


def test_skills_read_real_frontmatter_description(tmp_path):
    from core.skills.indexer import SkillIndexer
    md = tmp_path / "SKILL.md"
    md.write_text('---\nname: test\ndescription: |\n  Read real instructions\n  in Arabic and English\n---\n# Title\nActual instructions', encoding="utf-8")
    description, excerpt = SkillIndexer()._read_description(md)
    assert "real instructions" in description
    assert description != "---"
    assert "Actual instructions" in excerpt


def test_skill_search_is_lightweight_by_default():
    from core.skills.search import SkillSearch
    assert SkillSearch().encoder is False


def test_skill_search_does_not_recommend_irrelevant_packages(monkeypatch):
    from core.skills.search import SkillSearch
    from core.skills.ranker import SkillScore
    monkeypatch.setattr("core.skills.ranker.rank_skills", lambda query: [SkillScore(name="irrelevant", score=0.9, relevance=0, bundle_boost=0)])
    assert SkillSearch().search("unmatched task") == []


def test_arabic_skill_keywords_are_searchable():
    from core.skills.ranker import _tokens
    assert "الأمان" in _tokens("مراجعة الأمان وتحويل المستندات")


def test_memory_and_audit_redact_openrouter_keys(tmp_path):
    from core.memory_service import MemoryService
    from core.security.transient_vault import sanitize_text
    secret = "sk-or-v1-" + "testOnlySecret" * 4
    assert secret not in sanitize_text("credential " + secret)
    memory = MemoryService(storage_path=tmp_path / "memory.json")
    memory.store("credential-test", "credential " + secret)
    assert secret not in memory.storage_path.read_text(encoding="utf-8")


def test_web_fetch_cannot_read_local_credentials():
    from core.tools_bridge import _web_fetch
    with pytest.raises(ValueError):
        _web_fetch({"url": "file:///test-only-credential"})


def test_zip_cannot_escape_extraction_directory(tmp_path):
    from core.tool_installer import ToolInstaller
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../escaped.txt", "test")
    with pytest.raises(ValueError, match="escapes"):
        object.__new__(ToolInstaller)._extract_archive(archive, tmp_path / "out", "escaped.txt")
    assert not (tmp_path / "escaped.txt").exists()


def test_research_never_invents_entities():
    from core.brain.conversational_core import ConversationalCore
    core = object.__new__(ConversationalCore)
    assert core._extract_entities_from_research("Find laptops with RTX", {}) == []
    assert core._extract_entities_from_research("Find laptops", {"entities": [{"name": "Source result"}]}) == [{"name": "Source result"}]


def test_passive_plan_cannot_complete_action_request():
    from core.brain.conversational_core import ConversationalCore
    from core.models.provider_interface import ModelCompletionResponse
    core = object.__new__(ConversationalCore)
    core._provider = SimpleNamespace(generate=lambda req: ModelCompletionResponse(
        text="", parsed_json={"steps": [{"action_type": "wait", "params": {"duration": 0.1}}]}))
    assert core._formulate_plan_for_intent("Rename my document", None, {}) == []


def test_academic_source_rejects_xml_entities(monkeypatch):
    from core.rag.sources import academic
    source = academic.ArxivSource()
    monkeypatch.setattr(source, "_gate", lambda: None)
    monkeypatch.setattr(academic, "http_get", lambda *a, **k: (200, '<!DOCTYPE doc [<!ENTITY probe "bad">]><doc>&probe;</doc>', {}))
    assert source.search("safe probe") == []


def test_malformed_routing_does_not_invent_desktop_actions(monkeypatch):
    from core.brain.cognitive_decision_engine import CognitiveDecisionEngine
    from core.context.world_state import WISEWorldState
    from core.models.provider_interface import ModelCompletionResponse
    provider = SimpleNamespace(generate=lambda req: ModelCompletionResponse(text="{truncated"))
    engine = CognitiveDecisionEngine(provider=provider)
    decision = engine.preflight_analyze("Reliability check: reply with exactly TEST", world_state=WISEWorldState())
    assert decision.can_answer_directly
    assert "windows_control" not in decision.selected_capabilities
