"""Bounded diagnostic probes, NOT a product acceptance suite or fixes.

Uses only private disposable fixture state, no models/accounts/UAC/system edits.
An experiment completing successfully may demonstrate a failed product contract.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch, Mock
import urllib.request

APP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP))


def main(output: Path):
    output = output.resolve()
    if not output.is_relative_to(APP / "qa-results") or output.exists():
        raise ValueError("Use a new qa-results report path")
    rows = []
    with tempfile.TemporaryDirectory(prefix="risk-audit-fixtures-", dir=APP / ".tooling") as temporary:
        fixture = Path(temporary).resolve()
        os.environ["AGENT_OS_ROOT"] = str(APP)
        os.environ["WISE_RUNTIME_ROOT"] = str(fixture / "runtime")
        os.environ["WISE_SKILLS_DIR"] = str(fixture / "skills")
        os.environ["AGENT_OS_SKIP_FIRST_BOOT_CHECK"] = "1"
        from core.session_service import SessionService
        from core.brain.task_engine import TaskEngine
        from core.brain.conversational_core import ConversationalCore
        from core.resource.resource_manager import ManagedWorker

        def run(name, fn):
            try:
                observation = fn()
                rows.append(dict(name=name, experiment="COMPLETED", **observation))
            except Exception as exc:
                rows.append(dict(name=name, experiment="INCONCLUSIVE", error=type(exc).__name__ + ": " + str(exc)))

        def worker_false():
            called = []
            worker = ManagedWorker("fixture-only", start_fn=lambda: called.append(True) or False)
            returned = worker.start()
            return {"contract": "FAIL" if returned and worker.status.value == "RUNNING" else "PASS",
                "start_callback_return": False, "reported_start": returned,
                "reported_status": worker.status.value, "reported_health": worker.is_healthy(), "callback_called": len(called)}

        run("worker_false_start", worker_false)

        sessions = SessionService(storage_dir=fixture / "sessions")
        tasks = TaskEngine(storage_path=fixture / "tasks.json")
        core = ConversationalCore.__new__(ConversationalCore)
        core._turn_state = threading.local()
        core._lock = threading.RLock()
        core._provider = None
        core.task_engine = tasks

        def contextual_remove():
            session = sessions.get_or_create_session("remove-fixture")
            session.working_items = [{"name": "Alpha"}, {"name": "Beta"}, {"name": "Gamma"}]
            sessions.save_session(session)
            with patch("core.session_service.get_session_service", return_value=sessions):
                result = core.process_turn("Remove the second one", session_id=session.session_id)
            persisted = SessionService(storage_dir=fixture / "sessions").get_or_create_session(session.session_id)
            names = [r.get("name") for r in persisted.working_items]
            return {"contract": "FAIL" if "Removed item #2" in result.reply_text and "Beta" in names else "PASS",
                "reply": result.reply_text, "persisted_items": names,
                "reply_items": [r.get("name") for r in result.working_items]}

        run("contextual_remove_persistence", contextual_remove)

        def task_state():
            from core.orchestrator.closed_loop_orchestrator import OrchestrationStep, OrchestrationCycleResult
            from core.hands import ComputerActionType
            preflight = SimpleNamespace(selected_skill=None, selected_capabilities=["windows_control"],
                needs_clarification=False, can_answer_directly=False, is_deep_research=False, real_goal="fixture desktop inspection")
            core.world_state_engine = SimpleNamespace(get_current_world_state=lambda: None)
            core.decision_engine = SimpleNamespace(preflight_analyze=lambda **kwargs: preflight)
            core.orchestrator = SimpleNamespace(run_cycle=lambda **kwargs: OrchestrationCycleResult(intent=kwargs["intent"], success=True, steps_executed=1))
            core._formulate_plan_for_intent = lambda *args, **kwargs: [OrchestrationStep(action_type=ComputerActionType.OPEN_APP, params={"app": "fixture-only-not-dispatched"}, description="Fixture only; no desktop dispatch")]
            with patch("core.session_service.get_session_service", return_value=sessions), \
                 patch("core.memory_service.get_memory_service", side_effect=RuntimeError("No external memory in probe")), \
                 patch("core.capability_network.get_capability_network", side_effect=RuntimeError("No catalog traversal in probe")):
                result = core.process_turn("Inspect the active desktop", session_id="task-fixture")
            task = tasks.get_task(result.task_id)
            persisted = TaskEngine(storage_path=fixture / "tasks.json").get_task(result.task_id)
            return {"contract": "FAIL" if result.task_status == "COMPLETED" and task.status.value != "COMPLETED" else "PASS",
                "response_status": result.task_status, "in_memory_status": task.status.value,
                "restarted_status": persisted.status.value, "step_status": task.subgoals[0].steps[0].status.value,
                "boundary": "Real conversation + TaskEngine persistence; successful execution result is stubbed; no desktop action"}

        run("conversation_task_completion", task_state)

        def preview_link():
            from core.preview_server import PreviewManager
            from core.paths import WORKSPACE_DIR
            public = WORKSPACE_DIR / "public-fixture"
            public.mkdir(parents=True)
            outside = fixture / "outside-preview"
            outside.mkdir()
            (public / "normal.txt").write_text("PUBLIC_FIXTURE", encoding="utf-8")
            (outside / "marker.txt").write_text("PRIVATE_FIXTURE_NOT_REAL_SECRET", encoding="utf-8")
            link = public / "linked-fixture"
            if os.name == "nt":
                import _winapi
                _winapi.CreateJunction(str(outside), str(link))
            else:
                link.symlink_to(outside, target_is_directory=True)
            manager = PreviewManager()
            info = None
            try:
                info = manager.start(str(public), ttl_seconds=15)
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(info.url + "/normal.txt", timeout=4) as response:
                    normal = response.read().decode()
                with opener.open(info.url + "/linked-fixture/marker.txt", timeout=4) as response:
                    linked = response.read().decode()
                try:
                    manager.start(str(outside))
                    outside_rejected = False
                except ValueError:
                    outside_rejected = True
                return {"contract": "FAIL" if linked == "PRIVATE_FIXTURE_NOT_REAL_SECRET" else "PASS",
                    "normal_file_served": normal == "PUBLIC_FIXTURE", "outside_root_start_rejected": outside_rejected,
                    "outside_child_served": linked == "PRIVATE_FIXTURE_NOT_REAL_SECRET", "host": "127.0.0.1",
                    "link_type": "Windows junction" if os.name == "nt" else "symlink", "private_user_files_read": False}
            finally:
                if info is not None:
                    manager.stop(info.server_id)
                # Remove only the fixture link object, never traverse its target.
                if link.exists():
                    if os.name == "nt":
                        os.rmdir(link)
                    else:
                        link.unlink()

        run("preview_link_containment", preview_link)

        def adopted_port():
            spec = importlib.util.spec_from_file_location("wise_fixture_supervisor", APP.parent / "wise_supervisor.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            supervisor = module.WiseSupervisor.__new__(module.WiseSupervisor)
            supervisor.services = {}
            supervisor._shutting_down = False
            supervisor._setup_services()
            registered = list(supervisor.services)
            supervisor.probe_port = lambda *args, **kwargs: True
            starts = []
            for name, service in supervisor.services.items():
                service.start = Mock(side_effect=lambda key=name: starts.append(key) or setattr(supervisor, "_shutting_down", True))
            with patch.object(module.time, "sleep", lambda _: None):
                supervisor.start_all()
                before = list(starts)
                supervisor.run_loop()
            return {"contract": "FAIL" if not before and starts else "PASS", "registered_services": registered,
                "start_calls_after_adoption": before, "watchdog_start_calls": starts,
                "boundary": "Simulated listening ports and process starts; no services actually launched"}

        run("root_supervisor_port_adoption", adopted_port)

    from source_stamp import source_stamp
    result = {"scope": "BOUNDED_OFFLINE_DIAGNOSTICS_NOT_PRODUCT_ACCEPTANCE", "source_stamp": source_stamp(),
        "product_code_modified": False, "paid_calls": 0, "owner_data_read": False, "experiments": rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args().output)
