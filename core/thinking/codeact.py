"""
CodeAct strategy — execute-by-writing-code loop.

Inspired by OpenHands' CodeAct agent: instead of selecting from a fixed
tool list, the agent writes a small piece of bash / python / jupyter
code, the runtime executes it inside the sandbox, and the observation is
fed back into the next reasoning step. This is especially effective for
"complex programming" tasks where every step ends up being some form of
``exec()``.

The strategy is **complementary** to the regular :class:`ThinkingEngine`
— it can be invoked when ``task_type=="codeact"`` or when the regular
engine repeatedly fails on a programming task. It reuses
:class:`IPythonSession` so multiple steps share a single Python kernel.
"""

from __future__ import annotations

import re
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..observability import Tracer
from ..platform_manager import get_platform_manager, lite_preexec
from .ipython_session import IPythonSession

#: Maximum number of CodeAct steps before we hard-stop with a final answer.
DEFAULT_MAX_STEPS = 8

_BLOCK_RE = re.compile(
    r"```(?P<lang>bash|sh|shell|python|py|ipython|jupyter)\s*\n(?P<body>.*?)```",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class CodeActStep:
    iteration: int
    lang: str
    code: str
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float


@dataclass
class CodeActResult:
    final: str
    steps: List[CodeActStep] = field(default_factory=list)
    stopped_reason: str = "final_answer"


class CodeActStrategy:
    """Drives a Plan → Code → Execute → Observe → Reflect loop.

    Parameters
    ----------
    llm_callable:
        Callable that takes ``(messages, context)`` and returns the LLM's
        completion text. Pass any wrapper around your provider here. When
        ``None`` the strategy returns a deterministic stub useful for
        tests.
    max_steps:
        Hard upper bound on the loop. Defaults to 8.
    workdir:
        Optional working directory for the IPython kernel and bash
        commands; defaults to the current cwd.
    """

    def __init__(self,
                 llm_callable: Optional[Callable[[List[Dict[str, str]], Dict[str, Any]], str]] = None,
                 *,
                 max_steps: int = DEFAULT_MAX_STEPS,
                 workdir: Optional[Path] = None,
                 ipython_session: Optional[IPythonSession] = None) -> None:
        self.llm = llm_callable
        self.max_steps = max_steps
        self.workdir = Path(workdir) if workdir else Path.cwd()
        self._ipython = ipython_session
        self._platform = get_platform_manager()

    @property
    def ipython(self) -> IPythonSession:
        if self._ipython is None:
            self._ipython = IPythonSession(workdir=self.workdir)
        return self._ipython

    # ------------------------------------------------------------- Public API
    def run(self, task: str, *,
            system_prompt: Optional[str] = None,
            extra_context: Optional[Dict[str, Any]] = None) -> CodeActResult:
        Tracer.emit("codeact.start", task=task[:200],
                    mode=self._platform.mode)
        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        else:
            messages.append({"role": "system", "content": _DEFAULT_SYSTEM_PROMPT})
        messages.append({"role": "user", "content": task})

        steps: List[CodeActStep] = []
        for i in range(self.max_steps):
            response = self._call_llm(messages, extra_context or {})
            if not response:
                Tracer.emit("codeact.empty_response", iteration=i)
                return CodeActResult(final="(no response from LLM)",
                                     steps=steps,
                                     stopped_reason="empty_llm_response")

            messages.append({"role": "assistant", "content": response})

            final = _extract_final_answer(response)
            if final is not None:
                Tracer.emit("codeact.final", iteration=i, final=final[:200])
                return CodeActResult(final=final, steps=steps,
                                     stopped_reason="final_answer")

            block = _extract_code_block(response)
            if block is None:
                # Re-prompt: no code, no final answer.
                messages.append({"role": "user",
                                 "content": "Please reply either with a fenced code block "
                                            "(```bash, ```python, or ```ipython) or with "
                                            "FINAL: <answer>."})
                continue

            lang, code = block
            step = self._execute(lang, code, iteration=i)
            steps.append(step)
            messages.append({
                "role": "user",
                "content": _format_observation(step),
            })

        Tracer.emit("codeact.max_steps", steps=len(steps))
        return CodeActResult(final="(max steps reached without final answer)",
                             steps=steps,
                             stopped_reason="max_steps")

    # -------------------------------------------------------- Execution helpers
    def _execute(self, lang: str, code: str, *, iteration: int) -> CodeActStep:
        lang_norm = lang.lower()
        started = time.time()
        if lang_norm in ("python", "py", "ipython", "jupyter"):
            res = self.ipython.run(code)
            stdout, stderr, rc = res.stdout, res.stderr, res.exit_code
        else:
            stdout, stderr, rc = self._exec_shell(code)
        dur = (time.time() - started) * 1000
        Tracer.emit("codeact.step",
                    iteration=iteration, lang=lang_norm, exit=rc,
                    duration_ms=round(dur, 2))
        return CodeActStep(iteration=iteration, lang=lang_norm, code=code,
                           stdout=stdout, stderr=stderr,
                           exit_code=rc, duration_ms=dur)

    def _exec_shell(self, code: str) -> tuple[str, str, int]:
        try:
            shell_argv = (["cmd.exe", "/d", "/s", "/c", code]
                         if os.name == "nt" else ["/bin/bash", "-c", code])
            proc = subprocess.run(
                shell_argv,
                cwd=str(self.workdir),
                capture_output=True, text=True,
                timeout=120,
                preexec_fn=lite_preexec(self._platform),
            )
            return proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            return (exc.stdout or "", exc.stderr or "(timed out)", 124)
        except Exception as exc:
            return ("", f"shell error: {exc}", 1)

    # ------------------------------------------------------------------ LLM
    def _call_llm(self, messages: List[Dict[str, str]],
                  context: Dict[str, Any]) -> str:
        if self.llm is None:
            # Deterministic fallback used in tests.
            user = next((m["content"] for m in reversed(messages)
                         if m["role"] == "user"), "")
            return f"FINAL: stub-no-llm — task was: {user[:80]}"
        try:
            return self.llm(messages, context) or ""
        except Exception as exc:
            Tracer.emit("codeact.llm_error", error=str(exc))
            return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_DEFAULT_SYSTEM_PROMPT = """You are CodeAct, a programming agent. Solve the user's task by
writing small, self-contained code blocks. Follow this protocol:

1. Reply with ONE fenced code block per turn:
   - ```python ... ``` for Python (runs inside a persistent IPython kernel —
     state survives across turns).
   - ```bash ... ``` for shell commands.
2. After you see the execution observation, decide your next step.
3. When you have the final answer, reply with `FINAL: <answer>` (no code).

Keep each step under 60 lines of code. Prefer Python over bash for anything
non-trivial. The kernel survives between steps, so import once, reuse later.
"""


def _extract_code_block(text: str) -> Optional[tuple[str, str]]:
    m = _BLOCK_RE.search(text or "")
    if not m:
        return None
    lang = m.group("lang") or "python"
    body = m.group("body") or ""
    return lang, body.strip()


def _extract_final_answer(text: str) -> Optional[str]:
    if not text:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("FINAL:"):
            return stripped.split(":", 1)[1].strip()
    return None


def _format_observation(step: CodeActStep) -> str:
    parts: List[str] = [f"[exit {step.exit_code} • {step.duration_ms:.0f} ms]"]
    if step.stdout:
        parts.append(f"--- stdout ---\n{step.stdout[:4000]}")
    if step.stderr:
        parts.append(f"--- stderr ---\n{step.stderr[:2000]}")
    return "\n".join(parts) or "(no output)"


__all__ = ["CodeActStrategy", "CodeActResult", "CodeActStep"]
