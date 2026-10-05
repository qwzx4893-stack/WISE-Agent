import pytest
from qa.acceptance.autonomy_evidence import owned_window_matches, summarize_scenarios


def row(identity, **changes):
    return {"task_id": identity, "success": True, "verified": True,
        "steps": 2, "recovery_attempted": False, "recovery_succeeded": None,
        "false_success": False, **changes}


def test_recovery_rate_has_real_success_denominator_not_tautology():
    result = summarize_scenarios([row("good", recovery_attempted=True, recovery_succeeded=True),
        row("bad", recovery_attempted=True, recovery_succeeded=False, success=False, verified=False),
        row("unknown", recovery_attempted=True), row("no-recovery")])
    assert result["recovery_attempts"] == 3 and result["successful_recoveries"] == 1
    assert result["unconfirmed_recoveries"] == 1 and result["unsuccessful_recoveries"] == 1
    assert result["recovery_rate"] == 33.33


def test_no_attempt_is_not_a_successful_recovery():
    result = summarize_scenarios([row("baseline", recovery_succeeded=True)])
    assert result["recovery_rate"] is None and result["successful_recoveries"] == 0


def test_reported_success_without_verification_is_inferred_false_success():
    result = summarize_scenarios([row("api-only", verified=False), row("verified")])
    assert result["false_successes"] == 1 and result["false_success_rate"] == 50
    assert result["successful_tasks"] == 1 and result["task_success_rate"] == 50


def test_claimed_recovery_with_failed_final_state_cannot_pass():
    result = summarize_scenarios([row("failed", recovery_attempted=True, recovery_succeeded=True, success=False)])
    assert result["successful_recoveries"] == 0 and result["unsuccessful_recoveries"] == 1


@pytest.mark.parametrize("metrics", [[], [row("same"), row("same")], [row("")]])
def test_empty_duplicate_or_missing_identity_cannot_certify_coverage(metrics):
    with pytest.raises(ValueError):
        summarize_scenarios(metrics)


def ownership(**changes):
    data = {"launch_pid": 123, "preexisting_pids": {9, 10},
        "process": {"pid": 123, "running": True},
        "window": {"process_id": 123, "hwnd": 300, "is_visible": True, "title": "owned.txt - Notepad"},
        "fixture_name": "owned.txt"}
    data.update(changes)
    return owned_window_matches(**data)


def test_exact_fresh_pid_and_visible_unique_fixture_window_is_verified():
    assert ownership()


@pytest.mark.parametrize("changes", [{"launch_pid": None}, {"launch_pid": True},
    {"launch_pid": 0}, {"preexisting_pids": {123}}, {"process": {"pid": 123, "running": False}},
    {"window": {"process_id": 555, "hwnd": 300, "is_visible": True, "title": "owned.txt"}},
    {"window": {"process_id": 123, "hwnd": 0, "is_visible": True, "title": "owned.txt"}},
    {"window": {"process_id": 123, "hwnd": 300, "is_visible": True, "title": "other.txt"}},
    {"window": {"process_id": 123, "hwnd": 300, "is_visible": False, "title": "owned.txt"}},
    {"window": None}])
def test_title_only_existing_process_delegation_or_missing_window_is_not_owned(changes):
    assert not ownership(**changes)


def test_native_a_calls_use_actual_signature_and_disable_duplicate_retry():
    """Static call contract only: never launches Notepad or observes a desktop."""
    import ast
    import inspect
    from pathlib import Path
    from core.hands import WISEHands
    source = Path(__file__).with_name("test_production_autonomy_harness.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
        and node.name == "test_scenario_a_windows_application_control")
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "execute_closed_loop_action"]
    assert len(calls) == 2
    for call in calls:
        keywords = {item.arg: item.value for item in call.keywords}
        inspect.signature(WISEHands.execute_closed_loop_action).bind(None,
            **{name: None for name in keywords})
        assert isinstance(keywords["max_retries"], ast.Constant) and keywords["max_retries"].value == 0
    assert "get_perception_snapshot" not in ast.unparse(function)
