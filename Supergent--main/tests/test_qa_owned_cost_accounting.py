"""A soak's shared ledger window must not claim another runner's paid costs."""
import pytest
from qa.acceptance.live_budget import LiveBudget, BudgetError


def test_owned_turns_exclude_concurrent_runner_and_keep_unknown_reserve(tmp_path):
    budget = LiveBudget(tmp_path / "qa-budget.json")
    budget.initialize(additional_usd=1, model="fixture", pricing={"prompt":"0.000001","completion":"0.000001"})
    first = budget.begin_turn()
    own = budget.reserve("fixture", input_byte_bound=100, max_tokens=100)
    budget.settle(own, {"id":"fixture-own","usage":{"cost":"0.0002"}})
    unknown = budget.reserve("fixture", input_byte_bound=100, max_tokens=100)
    budget.end_turn(first)
    other = budget.begin_turn()
    foreign = budget.reserve("fixture", input_byte_bound=100, max_tokens=100)
    budget.settle(foreign, {"id":"fixture-other","usage":{"cost":"0.0003"}})
    budget.end_turn(other)
    before = budget.summary()
    result = budget.summarize_turns([first])
    assert result["attributed_generation_cost_usd"] == pytest.approx(.0002)
    assert result["pending_worst_case_usd"] == before["pending_worst_case_usd"] > 0
    assert result["dispatched_requests"] == 2 and result["actual_generations"] == 1
    assert before["attributed_generation_cost_usd"] == pytest.approx(.0005)
    assert budget.summary() == before  # no reserves/cap/turns cleared
    assert unknown != own


@pytest.mark.parametrize("identifiers", [["missing"], [1], [""], ["duplicate","duplicate"], "all", ["x"] * 257])
def test_invalid_turn_selection_fails_closed(tmp_path, identifiers):
    budget = LiveBudget(tmp_path / "qa-budget.json")
    budget.initialize(additional_usd=1, model="fixture", pricing={"prompt":"0","completion":"0"})
    with pytest.raises(BudgetError):
        budget.summarize_turns(identifiers)


def test_empty_owned_selection_is_zero_not_global_total(tmp_path):
    budget = LiveBudget(tmp_path / "qa-budget.json")
    budget.initialize(additional_usd=1, model="fixture", pricing={"prompt":"0","completion":"0"})
    result = budget.summarize_turns([])
    assert result["attributed_generation_cost_usd"] == 0
    assert result["dispatched_requests"] == result["actual_generations"] == 0
