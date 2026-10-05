"""Phase 7 — Part 3a: multi-agent workforce.

We avoid pytest-asyncio (not pinned in the project) by using
``asyncio.run`` inside sync test functions.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest


def _arun(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
def test_root_planner_splits_on_then():
    from core.workforce import RootPlanner
    out = RootPlanner().plan("scrape the docs then summarise them")
    assert len(out) == 2
    assert "scrape" in out[0].description
    assert "summarise" in out[1].description.lower()
    assert out[1].deps == [out[0].id]


def test_root_planner_handles_numbered_lists():
    from core.workforce import RootPlanner
    out = RootPlanner().plan("1. fetch data 2. clean it 3. publish report")
    assert len(out) == 3
    assert "fetch" in out[0].description
    assert out[1].deps == [out[0].id]
    assert out[2].deps == [out[1].id]


def test_root_planner_returns_single_when_no_separators():
    from core.workforce import RootPlanner
    out = RootPlanner().plan("write a haiku")
    assert len(out) == 1
    assert out[0].deps == []


def test_retry_policy_backoff_is_capped():
    from core.workforce import RetryPolicy
    p = RetryPolicy(max_attempts=10, base_delay=1.0, max_delay=8.0)
    assert p.delay_for(1) == 1.0
    assert p.delay_for(2) == 2.0
    assert p.delay_for(3) == 4.0
    assert p.delay_for(4) == 8.0
    assert p.delay_for(10) == 8.0


# ---------------------------------------------------------------------------
# Workforce execution
# ---------------------------------------------------------------------------
def test_workforce_runs_simple_chain():
    from core.workforce import Workforce, SubTask
    seen: list[str] = []

    async def execute(st: SubTask):
        seen.append(st.description)
        return f"ok:{st.description}"

    async def run():
        wf = Workforce(execute=execute, max_workers=1)
        return await wf.run("scrape docs then summarise")

    rpt = _arun(run())
    assert rpt.status == "done"
    assert len(rpt.subtasks) == 2
    assert all(s.status == "done" for s in rpt.subtasks)
    assert seen == [s.description for s in rpt.subtasks]


def test_workforce_retries_on_transient_failure():
    from core.workforce import Workforce, RetryPolicy

    failures = {"count": 0}

    async def execute(st):
        if failures["count"] < 2:
            failures["count"] += 1
            raise RuntimeError("transient")
        return "ok"

    async def run():
        wf = Workforce(execute=execute, max_workers=1,
                        retry=RetryPolicy(max_attempts=3, base_delay=0.001,
                                            max_delay=0.001))
        return await wf.run("solve the riddle")

    rpt = _arun(run())
    assert rpt.status == "done"
    assert rpt.subtasks[0].attempts == 3


def test_workforce_marks_failed_after_exhausting_retries():
    from core.workforce import Workforce, RetryPolicy

    async def execute(st):
        raise RuntimeError("permanent")

    async def run():
        wf = Workforce(execute=execute, max_workers=1,
                        retry=RetryPolicy(max_attempts=2, base_delay=0.001,
                                            max_delay=0.001))
        return await wf.run("attempt impossible")

    rpt = _arun(run())
    assert rpt.status == "failed"
    assert rpt.subtasks[0].status == "failed"
    assert "permanent" in rpt.failures[0]


def test_workforce_recursive_spawn():
    from core.workforce import Workforce, RecursionRequest, SubTask

    async def execute(st: SubTask):
        if not st.parent_id and st.attempts == 1:
            children = [
                SubTask(id=uuid.uuid4().hex[:6], description="leaf-A"),
                SubTask(id=uuid.uuid4().hex[:6], description="leaf-B"),
            ]
            raise RecursionRequest(children)
        return "ok"

    async def run():
        wf = Workforce(execute=execute, max_workers=1)
        return await wf.run("compound work")

    rpt = _arun(run())
    assert rpt.status == "done"
    parent = rpt.subtasks[0]
    assert len(parent.children) == 2


def test_workforce_recursion_depth_limit():
    from core.workforce import (
        Workforce, RecursionRequest, SubTask, MAX_RECURSION_DEPTH)

    async def execute(st):
        if st.depth < MAX_RECURSION_DEPTH + 1:
            child = SubTask(id=uuid.uuid4().hex[:6], description="deeper")
            raise RecursionRequest([child])
        return "ok"

    async def run():
        wf = Workforce(execute=execute, max_workers=1)
        rpt = await wf.run("infinite recurse")
        # Inspect the workforce's full subtask index.
        return rpt, list(wf._subtask_index.values())

    rpt, all_subtasks = _arun(run())
    statuses = {s.status for s in all_subtasks}
    assert "failed" in statuses or "partial" in statuses


def test_workforce_parallel_workers_run_concurrently():
    from core.workforce import (
        Workforce, RootPlanner, SubTask)

    class _NoDepsPlanner(RootPlanner):
        def plan(self, description):
            return [SubTask(id=uuid.uuid4().hex[:6], description=f"job{i}")
                    for i in range(4)]

    async def execute(st):
        await asyncio.sleep(0.05)
        return "ok"

    async def run():
        wf = Workforce(execute=execute, planner=_NoDepsPlanner(),
                        max_workers=4)
        return await wf.run("4 independent jobs")

    rpt = _arun(run())
    assert rpt.status == "done"
    # 4 workers × 0.05s should be well under 200ms; serial would need ≥200ms.
    assert rpt.duration_ms < 200, rpt.duration_ms


def test_dependent_subtask_skipped_when_dep_fails():
    from core.workforce import Workforce, RetryPolicy

    async def execute(st):
        if "fail" in st.description:
            raise RuntimeError("nope")
        return "ok"

    async def run():
        wf = Workforce(execute=execute, max_workers=1,
                        retry=RetryPolicy(max_attempts=1, base_delay=0.0,
                                           max_delay=0.0))
        return await wf.run("fail-step then good-step")

    rpt = _arun(run())
    assert rpt.status == "partial"
    statuses = [s.status for s in rpt.subtasks]
    assert "failed" in statuses
    assert "skipped" in statuses


def test_make_workforce_from_settings_respects_lite_mode(monkeypatch):
    from core.workforce import make_workforce_from_settings

    class _S:
        max_parallel_tools = 1

    class _Store:
        def load(self):
            return _S()

    import core.resource_settings as rs
    monkeypatch.setattr(rs, "get_store", lambda: _Store())

    async def execute(st):
        return None
    wf = make_workforce_from_settings(execute)
    assert wf.max_workers == 1
