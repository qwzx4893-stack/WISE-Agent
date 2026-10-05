"""Pure QA evidence checks; API success alone never establishes native ownership."""
from __future__ import annotations


def owned_window_matches(launch_pid, preexisting_pids, process, window, fixture_name):
    if type(launch_pid) is not int or launch_pid <= 0 or launch_pid in preexisting_pids:
        return False
    if not isinstance(process, dict) or not isinstance(window, dict):
        return False
    return bool(process.get("pid") == launch_pid and process.get("running") is True
        and window.get("process_id") == launch_pid
        and type(window.get("hwnd")) is int and window["hwnd"] > 0
        and window.get("is_visible") is True
        and isinstance(fixture_name, str) and bool(fixture_name)
        and fixture_name in str(window.get("title", "")))


def summarize_scenarios(metrics):
    if not metrics:
        raise ValueError("No observed scenarios")
    identities = [row.get("task_id") for row in metrics]
    if any(not isinstance(identity, str) or not identity for identity in identities) or len(set(identities)) != len(identities):
        raise ValueError("Scenario identities must be present and unique")
    total = len(metrics)
    successful = sum(row.get("success") is True and row.get("verified") is True for row in metrics)
    verified = sum(row.get("verified") is True for row in metrics)
    attempts = [row for row in metrics if row.get("recovery_attempted") is True]
    recovered = sum(row.get("recovery_succeeded") is True and row.get("success") is True
        and row.get("verified") is True for row in attempts)
    unknown = sum(row.get("recovery_succeeded") is None for row in attempts)
    false_successes = sum(row.get("false_success") is True or
        (row.get("success") is True and row.get("verified") is not True) for row in metrics)
    successful_steps = sum(row.get("steps", 0) for row in metrics
        if row.get("success") is True and row.get("verified") is True)
    return {"scope": "COMPONENT_AND_FIXTURE_SCENARIOS_NOT_MODEL_AUTONOMY_OR_DAILY_RELEASE_CERTIFICATE",
        "total_tasks": total, "successful_tasks": successful,
        "task_success_rate": round(successful / total * 100, 2),
        "verification_success_rate": round(verified / total * 100, 2),
        "recovery_attempts": len(attempts), "successful_recoveries": recovered,
        "unconfirmed_recoveries": unknown,
        "unsuccessful_recoveries": len(attempts) - recovered - unknown,
        "recovery_rate": round(recovered / len(attempts) * 100, 2) if attempts else None,
        "false_successes": false_successes, "false_success_rate": round(false_successes / total * 100, 2),
        "human_intervention_rate": round(sum(row.get("human_interventions", 0) > 0 for row in metrics) / total * 100, 2),
        "loop_rate": round(sum(row.get("loop_detected") is True for row in metrics) / total * 100, 2),
        "mean_steps_per_successful_task": round(successful_steps / successful, 2) if successful else None}
