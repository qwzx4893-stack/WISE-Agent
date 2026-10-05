"""Owned QA browser lifecycle: local artifacts, no inherited provider secrets.

No filesystem permissions are relaxed. Only a newly owned QA output beneath
this project's qa-results is admitted; no existing browser/profile is attached.
This process-environment context is serial and must not be used concurrently
from multiple threads in the same Python process.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMP_NAMES = ("TEMP", "TMP", "TMPDIR")
SECRET_WORDS = ("API_KEY", "_KEY", "ACCESSKEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE", "AUTH",
                "NANGO", "OPENROUTER", "ANTHROPIC", "KEYSTORE")


def owned_temp(output: Path) -> Path:
    """Validate lexical and resolved ownership, rejecting links/junctions."""
    qa_root = ROOT / "qa-results"
    output = Path(output).absolute()
    if ".." in output.parts:
        raise ValueError("QA output cannot contain traversal components")
    try:
        relative = output.relative_to(qa_root.absolute())
        output.resolve(strict=True).relative_to(qa_root.resolve(strict=True))
    except (ValueError, FileNotFoundError) as exc:
        raise ValueError("Browser output must be an existing owned qa-results child") from exc
    if not relative.parts or not output.is_dir():
        raise ValueError("A specific owned QA output directory is required")
    current = qa_root
    for part in (None, *relative.parts, "browser-temp"):
        if part is not None:
            current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("Browser output cannot traverse symlinks or junctions")
    temp = output / "browser-temp"
    temp.mkdir(exist_ok=True)
    if not temp.is_dir() or temp.resolve().parent != output.resolve():
        raise ValueError("Temporary artifacts are outside the owned QA output")
    return temp.resolve()


def browser_environment(output: Path, environ=None) -> dict[str, str]:
    temp = str(owned_temp(output))
    source = os.environ if environ is None else environ
    env = {name: value for name, value in source.items()
           if not any(word in name.upper() for word in SECRET_WORDS)}
    env.update({name: temp for name in TEMP_NAMES})
    return env


def _playwright_context():
    from playwright.sync_api import sync_playwright
    return sync_playwright()


@contextmanager
def owned_browser_environment(output: Path):
    """Scope browser/driver environment without starting an extra event loop."""
    environment = browser_environment(output)
    previous = dict(os.environ)
    # Node's driver also inherits the filtered environment. The launched Brave
    # must receive browser_environment(output) explicitly at the call site.
    try:
        os.environ.clear()
        os.environ.update(environment)
        yield environment
    finally:
        os.environ.clear()
        os.environ.update(previous)


@contextmanager
def owned_playwright(output: Path):
    """Set driver temp paths before startup and restore even when startup fails."""
    with owned_browser_environment(output):
        with _playwright_context() as playwright:
            yield playwright
