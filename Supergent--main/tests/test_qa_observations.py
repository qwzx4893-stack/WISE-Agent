"""Evidence collection is observation, never a replacement/fabricated result."""
import json
from types import SimpleNamespace
import pytest
from qa.acceptance import tool_evidence


def test_observed_public_result_is_redacted_but_fact_preserved(tmp_path):
    path=tmp_path/"evidence.ndjson"
    result=SimpleNamespace(to_dict=lambda:{"success":True,"output":{"page_text":"Verified public fact","api_key":"sensitive"}})
    tool_evidence.record(path,"native.web_fetch",result)
    row=json.loads(path.read_text(encoding="utf-8"))
    assert row["result"]["output"]["page_text"]=="Verified public fact"
    assert row["result"]["output"]["api_key"]=="[REDACTED]"
    assert "sensitive" not in path.read_text(encoding="utf-8")


def test_unrestricted_file_or_account_data_is_not_observed(tmp_path):
    path=tmp_path/"evidence.ndjson"
    result=SimpleNamespace(to_dict=lambda:(_ for _ in ()).throw(AssertionError("Must not inspect private payload")))
    for tool in ("native.read_file","channels.send","extension.plugins.api_key"):
        tool_evidence.record(path,tool,result)
    assert not path.exists()


def test_recursive_console_and_stack_trace_redact_bearer_credentials():
    observed = tool_evidence.redact({"console": [{"text": "failure Bearer synthetic-test-credential"}],
                                   "stack_trace": "bearer synthetic-test-credential",
                                   "output": {"access_token": "synthetic-test-credential"}})
    assert "synthetic-test-credential" not in json.dumps(observed)
    assert observed["stack_trace"] == "[REDACTED]"


def test_observer_calls_actual_router_once_and_returns_same_result(monkeypatch,tmp_path):
    from core.capability_router import CapabilityRouter
    monkeypatch.setattr(tool_evidence,"ROOT",tmp_path)
    calls=[]
    result=SimpleNamespace(to_dict=lambda:{"success":True,"output":{"page_text":"Observed"}})
    def actual(self,id_or_name,*args,**kwargs):
        calls.append((id_or_name,args,kwargs));return result
    monkeypatch.setattr(CapabilityRouter,"execute",actual)
    path=tmp_path/"qa-results/owned/evidence.ndjson"
    tool_evidence.install(path)
    returned=object.__new__(CapabilityRouter).execute(id_or_name="native.web_fetch",params={"url":"https://example.com"})
    assert returned is result and len(calls)==1
    assert calls[0][2]=={"params":{"url":"https://example.com"}}


def test_evidence_path_and_size_budget_fail_closed(monkeypatch,tmp_path):
    monkeypatch.setattr(tool_evidence,"ROOT",tmp_path)
    with pytest.raises(ValueError,match="owned"):
        tool_evidence.install(tmp_path/"user-files/evidence.ndjson")
    result=SimpleNamespace(to_dict=lambda:{"output":"x"*300001})
    with pytest.raises(ValueError,match="budget"):
        tool_evidence.record(tmp_path/"evidence.ndjson","native.web_fetch",result)
