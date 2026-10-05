"""Offline production-boundary contracts, not live-model or GPU certification."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from core.capability_router import CapabilityDescriptor, CapabilityRouter
from core.contracts import CapabilitySource


@pytest.fixture
def router(monkeypatch):
    import core.capability_router as module
    router = object.__new__(CapabilityRouter)
    router._capabilities = {}
    monkeypatch.setattr(router, "get_capability", lambda key: router._capabilities.get(key))
    monkeypatch.setattr(module, "get_security_gate", lambda: SimpleNamespace(
        evaluate_action=lambda **kwargs: SimpleNamespace(allowed=True)))
    return router


def register(router, schema, adapter):
    router.register(CapabilityDescriptor("native.fixture", "fixture", CapabilitySource.NATIVE,
        "Offline fixture", input_schema=schema, execution_adapter=adapter))


@pytest.mark.parametrize("parameters", [{}, {"count":True}, {"count":"2"}, {"count":0}, {"count":2,"extra":1}])
def test_input_schema_blocks_invalid_arguments_before_adapter(router, parameters):
    calls = []
    register(router, {"type":"object","properties":{"count":{"type":"integer","minimum":1}},
        "required":["count"],"additionalProperties":False}, lambda params:calls.append(params))
    result = router.execute("native.fixture", parameters)
    assert not result.success and not calls
    assert result.metadata["failure_kind"] == "INVALID_ARGUMENTS"


def test_shorthand_schema_is_validated_without_coercion(router):
    register(router, {"path":{"type":"string"}}, lambda params:"observed")
    assert router.execute("native.fixture", {"path":"sample.txt"}).success
    assert not router.execute("native.fixture", {"path":3}).success


def test_remote_schema_reference_is_not_network_access(router):
    register(router, {"$ref":"https://must-not-fetch.invalid/schema.json"}, lambda params:"never")
    assert not router.execute("native.fixture", {}).success


@pytest.mark.parametrize("output", [{"success":False}, {"isError":True}, {"error":"failed"}])
def test_structured_tool_failure_is_never_success(router, output):
    register(router, {}, lambda params:output)
    assert not router.execute("native.fixture", {}).success


def test_rag_binding_calls_real_engine_entry_and_propagates_failure(router, monkeypatch):
    import core.brain.cognitive_decision_engine as engine
    calls=[]
    monkeypatch.setattr(engine,"get_cognitive_decision_engine",lambda:SimpleNamespace(
        execute_deep_research=lambda query:(calls.append(query) or {"errors":["No evidence"],"synthesis":"Unverified"})))
    router._load_rag_capabilities()
    result=router.execute("rag.deep_research",{"query":"Actual topic"})
    assert calls==["Actual topic"] and not result.success
    assert "No evidence" in result.error


def test_memory_bindings_use_actual_query_and_store_methods(router, monkeypatch):
    import core.memory_service as memory
    calls=[]
    monkeypatch.setattr(memory,"get_memory_service",lambda:SimpleNamespace(
        query=lambda query,limit:(calls.append(("query",query,limit)) or {"items":[]}),
        store=lambda key,value,metadata:calls.append(("store",key,value,metadata))))
    router._load_memory_capabilities()
    assert router.execute("memory.search",{"query":"fact","limit":2}).success
    assert router.execute("memory.store",{"key":"fact","value":"observed"}).success
    assert calls==[("query","fact",2),("store","fact","observed",{})]


def test_computer_binding_preserves_nested_parameters_and_verified_success(router, monkeypatch):
    import core.hands.computer_use as hands
    calls=[]
    record=SimpleNamespace(success=False,to_dict=lambda:{"verification_result":False,"action_result":{"success":True}})
    monkeypatch.setattr(hands,"get_wise_hands",lambda:SimpleNamespace(execute_closed_loop_action=
        lambda action,params,*,security_context:(calls.append((action.value,params,security_context)) or record)))
    router._load_computer_use_capabilities()
    result=router.execute("computer.execute_action",{"action":"wait","params":{"seconds":.1}},
        session_id="trusted-fixture", confirmed=True, untrusted_content=True)
    assert len(calls)==1 and calls[0][:2]==("wait",{"seconds":.1}) and not result.success
    context=calls[0][2]
    assert context.confirmed and context.is_untrusted_content
    assert context.session_id=="trusted-fixture"
    assert context.caller=="capability_router:computer.execute_action"


def test_artifact_outcome_needs_exact_readback_of_each_last_write():
    from core.usage_network import UsageNetwork
    plan=SimpleNamespace(phases=["modify","verify"])
    calls=[{"tool":"native.write_file","path":"a.txt","success":True},
           {"tool":"native.read_file","path":"b.txt","success":True,"content_verified":True}]
    assert UsageNetwork.assess_outcome(plan,calls)["unverified_artifacts"]==["a.txt"]
    calls.append({"tool":"native.read_file","path":"a.txt","success":True,"content_verified":True})
    assert UsageNetwork.assess_outcome(plan,calls)["artifact_obligations_met"]
    calls.append({"tool":"native.write_file","path":"a.txt","success":True})
    assert not UsageNetwork.assess_outcome(plan,calls)["artifact_obligations_met"]


def test_atomic_artifact_replace_failure_preserves_old_file(monkeypatch,tmp_path):
    import core.tools_bridge as bridge
    monkeypatch.setattr(bridge,"WORKSPACE_DIR",tmp_path)
    target=tmp_path/"artifact.txt"
    target.write_text("old",encoding="utf-8")
    monkeypatch.setattr(bridge.os,"replace",lambda *args:(_ for _ in ()).throw(OSError("fixture disk failure")))
    with pytest.raises(OSError): bridge._write_file({"path":"artifact.txt","content":"new"})
    assert target.read_text()=="old"
    assert sorted(item.name for item in tmp_path.iterdir())==["artifact.txt"]


def test_unsupported_exact_security_property_is_not_accepted_as_cited_fact():
    from core.web_research import validate_grounded_answer
    evidence=[{"url":"https://source.example/security","evidence_kind":"page_excerpt",
               "page_text":"Upgrade following the current advisory."}]
    answer="Set `log4j2.formatMsgNoLookups` [official](https://source.example/security)."
    errors=validate_grounded_answer(answer,evidence)
    assert errors and "technical claim" in errors[0]
    evidence[0]["page_text"]+=" log4j2.formatMsgNoLookups"
    assert not validate_grounded_answer(answer,evidence)


def test_snippet_does_not_prove_exact_code_claim():
    from core.web_research import validate_grounded_answer
    evidence=[{"url":"https://source.example/page","evidence_kind":"search_snippet","snippet":"run --safe"}]
    assert validate_grounded_answer("Run `--safe` [source](https://source.example/page)",evidence)


def test_selected_environment_provider_is_not_shadowed_by_another_key(monkeypatch):
    from core.models.provider_interface import _environment_provider_configuration
    monkeypatch.setenv("OPENAI_API_KEY","offline-placeholder-one")
    monkeypatch.setenv("GEMINI_API_KEY","offline-placeholder-two")
    monkeypatch.delenv("WISE_LLM_API_KEY",raising=False)
    monkeypatch.delenv("WISE_LLM_BASE_URL",raising=False)
    configuration=_environment_provider_configuration("gemini")
    assert configuration["provider_id"]=="gemini"
    assert configuration["api_key"]=="offline-placeholder-two"


def test_production_does_not_reuse_cached_simulation():
    from core.models.provider_interface import SimulatedTestProvider,set_active_model_provider,get_production_model_provider,UnavailableModelProvider
    set_active_model_provider(SimulatedTestProvider())
    assert isinstance(get_production_model_provider(),UnavailableModelProvider)


def test_named_legacy_failover_method_executes_selected_provider_once(monkeypatch):
    import core.models.model_router as module
    from core.models.provider_interface import ModelCompletionRequest,ModelCompletionResponse
    calls=[]
    router=object.__new__(module.ModelRouter)
    primary=SimpleNamespace(generate=lambda request:(calls.append("selected") or ModelCompletionResponse("",error="fixture")))
    monkeypatch.setattr(router,"route_workload",lambda *args:("selected",primary,"choice"))
    monkeypatch.setattr(module,"get_model_provider",lambda **kw:pytest.fail("No secondary provider selection"))
    response=router.execute_with_failover(ModelCompletionRequest(messages=[]))
    assert response.error=="fixture" and calls==["selected"]


def test_voice_gpu_probe_matches_device_zero_not_maximum_free_memory(monkeypatch):
    import core.voice.tts as module
    monkeypatch.setattr(module.subprocess,"run",lambda *a,**kw:SimpleNamespace(stdout="1000\n9000\n"))
    assert module.IndexTTSProvider._free_vram_mb()==1000


def test_voice_unknown_vram_blocks_with_local_model(monkeypatch):
    from core.voice.tts import IndexTTSProvider
    provider=object.__new__(IndexTTSProvider)
    provider._proc=None
    provider._selected_device="unselected"
    monkeypatch.setattr(provider,"_free_vram_mb",lambda:None)
    monkeypatch.setattr(provider,"_local_model_selected",lambda:True)
    assert not provider.resource_status()["ready"]


def test_serial_voice_turn_rejects_concurrent_execution():
    from core.voice.runtime import VoiceRuntime,VoiceRuntimeState
    runtime=object.__new__(VoiceRuntime)
    runtime._interaction_lock=threading.Lock()
    runtime._lock=threading.RLock()
    runtime._state=VoiceRuntimeState.PROCESSING
    runtime._interaction_lock.acquire()
    try:
        result=runtime.interact("second turn")
        assert not result.success and result.error.startswith("VOICE_BUSY")
    finally: runtime._interaction_lock.release()


def test_voice_core_error_is_reported_without_fallback_execution(monkeypatch):
    import core.brain.conversational_core as brain
    import core.session_service as sessions
    from core.voice.runtime import VoiceRuntime,VoiceRuntimeState
    runtime=object.__new__(VoiceRuntime)
    runtime._interaction_lock=threading.Lock()
    runtime._lock=threading.RLock()
    runtime._state=VoiceRuntimeState.IDLE
    runtime._interrupted_turn=threading.Event()
    runtime._stop_event=threading.Event()
    runtime._total_interactions=0
    runtime._set_state=lambda state:setattr(runtime,"_state",state)
    runtime.tts=SimpleNamespace(speak=lambda *a,**kw:SimpleNamespace(success=True,latency_ms=0,error=None),is_speaking=lambda:False)
    runtime.orchestrator=SimpleNamespace(orchestrate_intent=lambda _:pytest.fail("Never repeat execution after failure"))
    monkeypatch.setattr(sessions,"get_session_service",lambda:SimpleNamespace(get_or_create_session=lambda sid:SimpleNamespace(session_id=sid)))
    monkeypatch.setattr(brain,"get_conversational_core",lambda:SimpleNamespace(process_turn=lambda *a,**kw:(_ for _ in ()).throw(OSError("fixture after side effect"))))
    result=runtime.interact("inspect",session_id="voice-qa")
    assert not result.success and result.error=="Voice turn failed: OSError"


def test_universal_stream_failure_does_not_make_extra_request(monkeypatch):
    from core.llm.universal import UniversalLLM
    llm=object.__new__(UniversalLLM)
    llm.provider=SimpleNamespace(transport="openai")
    monkeypatch.setattr(llm,"_stream_openai_compat",lambda *a,**kw:(_ for _ in ()).throw(RuntimeError("fixture")))
    monkeypatch.setattr(llm,"_call_openai_compat",lambda *a,**kw:pytest.fail("No automatic second request"))
    with pytest.raises(RuntimeError,match="retry explicitly"):
        list(llm.stream([{"role":"user","content":"Hi"}]))


def test_tts_pipe_deadline_stops_worker_without_gpu_inference(monkeypatch,tmp_path):
    import core.voice.tts as module
    import queue
    provider=object.__new__(module.IndexTTSProvider)
    provider._request_lock=threading.Lock()
    provider._output_dir=tmp_path
    provider.language="ar"
    provider.reference_audio=tmp_path/"reference.wav"
    provider.gpt_checkpoint=None
    provider._stop_generation=0
    proc=SimpleNamespace(stdin=SimpleNamespace(write=lambda _:None,flush=lambda:None),
                         stdout=SimpleNamespace(readline=lambda:""))
    monkeypatch.setattr(provider,"is_available",lambda:True)
    monkeypatch.setattr(provider,"resource_status",lambda:{"ready":True})
    monkeypatch.setattr(provider,"_ensure_process",lambda:proc)
    stopped=[]
    monkeypatch.setattr(provider,"stop",lambda:stopped.append(True))
    ticks=iter([0,200])
    monkeypatch.setattr(module.time,"monotonic",lambda:next(ticks))
    result=provider.speak("اختبار")
    assert not result.success and "timed out" in result.error
    assert stopped==[True]


def test_agent_write_is_verified_without_extra_model_request(monkeypatch,tmp_path):
    import core.skills.indexer as skills
    from core.brain.capability_agent import CapabilityAgent
    from core.models.provider_interface import ModelCompletionResponse
    monkeypatch.setattr(skills.SkillIndexer,"get_index",lambda self:{})
    caps=[CapabilityDescriptor(f"native.{name}",name,CapabilitySource.NATIVE,name)
          for name in ("write_file","read_file")]
    observed={}
    calls=[]
    def execute(tool,args,**kwargs):
        calls.append(tool)
        if tool=="native.write_file": observed[args["path"]]=args["content"]
        output=observed[args["path"]] if tool=="native.read_file" else "OK"
        return SimpleNamespace(success=True,to_dict=lambda:{"success":True,"output":output})
    router=SimpleNamespace(list_capabilities=lambda:caps,execute=execute)
    actions=iter([{"tool":"native.write_file","arguments":{"path":"result.txt","content":"Observed bytes"}},
                  {"answer":"Saved and verified."}])
    requests=[]
    def generate(request):
        requests.append(request)
        action=next(actions)
        return ModelCompletionResponse(json.dumps(action),parsed_json=action,model_name="offline")
    outcome=CapabilityAgent(SimpleNamespace(generate=generate),router=router,max_steps=2).run(
        "Create result.txt",session_id="readback")
    assert not outcome.error and len(requests)==2
    assert calls==["native.write_file","native.read_file"]
    assert outcome.metrics["outcome_obligations"]["verified_artifacts"]==["result.txt"]


def test_memory_store_disk_failure_rolls_back_contradiction_and_state(monkeypatch,tmp_path):
    import core.memory_service as module
    memory=module.MemoryService(tmp_path/"memory.json")
    memory.store("old","observed",entity_key="subject")
    before=memory.storage_path.read_bytes()
    monkeypatch.setattr(module.os,"replace",lambda *args:(_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(module.MemoryPersistenceError): memory.store("new","changed",entity_key="subject")
    assert memory.storage_path.read_bytes()==before
    assert memory.get("new") is None and memory.get("old").superseded_by is None
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_memory_is_preserved_not_silently_replaced(tmp_path):
    from core.memory_service import MemoryService,MemoryPersistenceError
    path=tmp_path/"memory.json"
    path.write_text("{broken",encoding="utf-8")
    with pytest.raises(MemoryPersistenceError): MemoryService(path)
    assert path.read_text()=="{broken"


def test_additional_budget_cannot_reset_or_overspend_pending_requests(tmp_path):
    from qa.acceptance.live_budget import LiveBudget,BudgetError
    budget=LiveBudget(tmp_path/"budget.json")
    budget.initialize(additional_usd=1,model="fixture",pricing={"prompt":"0.0001","completion":"0.0001"})
    turn=budget.begin_turn(max_requests=2)
    first=budget.reserve("fixture",input_byte_bound=0,max_tokens=1)
    with pytest.raises(BudgetError): budget.reserve("fixture",input_byte_bound=10000,max_tokens=1)
    assert budget.summary()["pending_worst_case_usd"]>0
    budget.settle(first,{"id":"generation-fixture","usage":{"cost":.001}})
    second=budget.reserve("fixture",input_byte_bound=0,max_tokens=1)
    with pytest.raises(BudgetError): budget.reserve("fixture",input_byte_bound=0,max_tokens=1)
    budget.end_turn(turn)
    reopened=LiveBudget(tmp_path/"budget.json")
    reopened.initialize(additional_usd=1,model="fixture",pricing={"prompt":"0.0001","completion":"0.0001"})
    assert reopened.summary()["actual_generations"]==1
    assert reopened.summary()["pending_worst_case_usd"]>0
    with pytest.raises(BudgetError): reopened.initialize(additional_usd=.5,model="fixture",pricing={})


def test_unknown_generation_cost_remains_reserved(tmp_path):
    from qa.acceptance.live_budget import LiveBudget
    budget=LiveBudget(tmp_path/"budget.json")
    budget.initialize(additional_usd=1,model="fixture",pricing={"prompt":"0.000001","completion":"0.000001"})
    turn=budget.begin_turn(max_requests=1)
    reservation=budget.reserve("fixture",input_byte_bound=100,max_tokens=10)
    assert not budget.settle(reservation,{"id":"unknown"})
    budget.end_turn(turn)
    assert budget.summary()["pending_worst_case_usd"]>0
    assert budget.summary()["attributed_generation_cost_usd"]==0


def test_live_budget_requires_prices_and_raises_existing_tariff(tmp_path):
    from qa.acceptance.live_budget import LiveBudget, BudgetError
    budget = LiveBudget(tmp_path / "budget.json")
    with pytest.raises(BudgetError, match="prices"):
        budget.initialize(additional_usd=1, model="fixture", pricing={})
    budget.initialize(additional_usd=1, model="fixture", pricing={"prompt":".00001", "completion":".00001"})
    budget.initialize(additional_usd=1, model="fixture", pricing={"prompt":".001", "completion":".00001"})
    budget.begin_turn(max_requests=1)
    with pytest.raises(BudgetError, match="insufficient"):
        budget.reserve("fixture", input_byte_bound=0, max_tokens=1)
    assert budget.summary()["dispatched_requests"] == 0
