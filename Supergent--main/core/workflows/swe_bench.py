"""
SWE-Bench-style workflow template.

A durable, replayable recipe for solving a software-engineering issue
end-to-end: clone the target repo, explore the relevant files, reproduce
the bug, patch the source, run the test suite, and emit a unified diff.

The workflow is implemented as a sequence of :class:`CodeActStrategy`
turns so the LLM produces bash / python directly. It is deliberately
small (≈200 lines) — the heavy lifting happens inside the model.

Inspired by OpenHands' SWE-Bench agent design but written from scratch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..observability import Tracer
from ..thinking.codeact import CodeActResult, CodeActStrategy
from ..thinking.ipython_session import IPythonSession


@dataclass
class SWEBenchTask:
    """A single SWE-Bench style task description."""

    repo_url: str
    issue: str
    base_commit: Optional[str] = None
    test_command: Optional[str] = "pytest -q"
    timeout_s: int = 600
    extra_context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SWEBenchResult:
    ok: bool
    repo_path: str
    final_message: str
    diff: str = ""
    tests_passed: bool = False
    test_output: str = ""
    steps_log: List[Dict[str, Any]] = field(default_factory=list)
    duration_s: float = 0.0


class SWEBenchWorkflow:
    """Run a SWE-Bench-style task end-to-end.

    Parameters
    ----------
    llm_callable:
        Same shape as :class:`CodeActStrategy.llm`. Pass ``None`` for a
        deterministic stub run useful in unit tests.
    workdir:
        Where to clone repos. Defaults to a fresh ``tempfile.mkdtemp()``.
    """

    def __init__(self,
                 llm_callable: Optional[Callable] = None,
                 *,
                 workdir: Optional[Path] = None) -> None:
        self.llm = llm_callable
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="swe_bench_"))
        self.workdir.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------- API
    def run(self, task: SWEBenchTask) -> SWEBenchResult:
        Tracer.emit("swe_bench.start", repo=task.repo_url, issue=task.issue[:200])
        started = time.time()
        steps: List[Dict[str, Any]] = []

        repo_path = self._clone(task)
        steps.append({"step": "clone", "ok": repo_path is not None,
                      "path": str(repo_path) if repo_path else ""})
        if repo_path is None:
            return SWEBenchResult(ok=False, repo_path="",
                                  final_message="failed to clone repository",
                                  steps_log=steps,
                                  duration_s=time.time() - started)

        # CodeAct turn: ask the model to read the issue + repo and propose a fix.
        ipython = IPythonSession(workdir=repo_path)
        strategy = CodeActStrategy(llm_callable=self.llm,
                                   max_steps=8,
                                   workdir=repo_path,
                                   ipython_session=ipython)

        prompt = _build_prompt(task, repo_path)
        plan_result = strategy.run(prompt, system_prompt=_SYSTEM_PROMPT,
                                   extra_context=task.extra_context or {})
        steps.append({"step": "plan_and_patch",
                      "stopped_reason": plan_result.stopped_reason,
                      "iterations": len(plan_result.steps)})

        # Run the test command after the model has applied its patch.
        tests_passed, test_output = self._run_tests(repo_path, task.test_command,
                                                    timeout=task.timeout_s)
        steps.append({"step": "test",
                      "ok": tests_passed,
                      "output_tail": (test_output or "")[-500:]})

        # Capture the unified diff so the caller can inspect / open a PR.
        diff = self._capture_diff(repo_path)
        steps.append({"step": "diff", "size": len(diff)})

        ipython.close()
        return SWEBenchResult(
            ok=tests_passed,
            repo_path=str(repo_path),
            final_message=plan_result.final,
            diff=diff,
            tests_passed=tests_passed,
            test_output=test_output,
            steps_log=steps,
            duration_s=time.time() - started,
        )

    # -------------------------------------------------------------- Helpers
    def _clone(self, task: SWEBenchTask) -> Optional[Path]:
        target = self.workdir / _slug(task.repo_url)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", task.repo_url, str(target)],
                check=True, capture_output=True, text=True, timeout=120,
            )
        except Exception as exc:
            Tracer.emit("swe_bench.clone_failed", error=str(exc))
            return None
        if task.base_commit:
            try:
                subprocess.run(["git", "fetch", "--depth", "1", "origin", task.base_commit],
                               cwd=target, check=True, capture_output=True,
                               text=True, timeout=60)
                subprocess.run(["git", "checkout", task.base_commit],
                               cwd=target, check=True, capture_output=True,
                               text=True, timeout=30)
            except Exception as exc:
                Tracer.emit("swe_bench.checkout_warning", error=str(exc))
        return target

    @staticmethod
    def _run_tests(repo_path: Path, cmd: Optional[str], *,
                   timeout: int) -> tuple[bool, str]:
        if not cmd:
            return True, "(test_command not set)"
        try:
            proc = subprocess.run(
                (["cmd.exe", "/d", "/s", "/c", cmd]
                 if os.name == "nt" else ["/bin/bash", "-c", cmd]),
                cwd=str(repo_path),
                capture_output=True, text=True, timeout=timeout,
            )
            return proc.returncode == 0, (proc.stdout + "\n" + proc.stderr)
        except subprocess.TimeoutExpired as exc:
            return False, f"timeout after {timeout}s\n{exc.stdout or ''}\n{exc.stderr or ''}"
        except Exception as exc:
            return False, f"test runner error: {exc}"

    @staticmethod
    def _capture_diff(repo_path: Path) -> str:
        try:
            proc = subprocess.run(["git", "diff"], cwd=str(repo_path),
                                  capture_output=True, text=True, timeout=15)
            return proc.stdout or ""
        except Exception:
            return ""


# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """You are SWE-Bench Agent. You operate inside a freshly-cloned
repository using a persistent IPython kernel and bash. Your goal is to:

  1. Read and understand the issue described by the user.
  2. Locate the relevant files (use bash + grep / rg).
  3. Reproduce the bug if a reproducer is suggested.
  4. Apply a minimal patch directly to the source files.
  5. Run the test suite (`pytest -q` unless the issue says otherwise).
  6. When tests pass, reply `FINAL: <one-line summary of the fix>`.

Constraints:
- Use ```bash ... ``` for shell commands and ```python ... ``` for Python.
- Keep each step under 60 lines.
- Do NOT push to remote, do NOT modify .git, do NOT run network installers.
"""


def _build_prompt(task: SWEBenchTask, repo_path: Path) -> str:
    lines = [
        f"Repository: {task.repo_url}",
        f"Local checkout: {repo_path}",
        "",
        "Issue:",
        task.issue.strip(),
    ]
    if task.test_command:
        lines += ["", f"Run tests with: {task.test_command}"]
    return "\n".join(lines)


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s).strip("_")[-60:]


__all__ = ["SWEBenchTask", "SWEBenchResult", "SWEBenchWorkflow"]
