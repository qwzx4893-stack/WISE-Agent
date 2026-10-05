"""The real preflight is charged; bounded QA admission is not a new dollar cap."""
import ast
from pathlib import Path

import pytest

from qa.acceptance.live_budget import BudgetError, LiveBudget


def test_normal_project_admits_one_preflight_plus_twelve_executor_requests(tmp_path):
    budget = LiveBudget(tmp_path / "ledger.json")
    budget.initialize(additional_usd=1, model="fixture", pricing={"prompt": "0.000001", "completion": "0.000001"})
    turn = budget.begin_turn(max_requests=13)
    for _ in range(13):
        budget.reserve("fixture", input_byte_bound=1, max_tokens=1)
    with pytest.raises(BudgetError, match="request count"):
        budget.reserve("fixture", input_byte_bound=1, max_tokens=1)
    before = budget.summary()
    budget.end_turn(turn)
    budget.initialize(additional_usd=1, model="fixture", pricing={"prompt": "0.000001", "completion": "0.000001"})
    assert budget.summary()["pending_worst_case_usd"] == before["pending_worst_case_usd"] > 0
    assert budget.summary()["remaining_admission_usd"] == before["remaining_admission_usd"]


def test_project_runner_bound_and_focused_profile_remain_explicit():
    root = Path(__file__).resolve().parents[1]
    source = (root / "qa/acceptance/project_journeys.py").read_text(encoding="utf-8")
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "begin_turn"]
    assert len(calls) == 1
    assert next(k.value.value for k in calls[0].keywords if k.arg == "max_requests") == 13
    assert "FOCUSED_FOLLOW_UP_DIAGNOSTIC_ONLY" in source and "runner_sha256_at_start" in source
