"""Pytest configuration.

Sets ``AGENT_OS_ROOT`` to the repo root before any test imports
``core.paths``. This avoids the legacy fallback to ``~/agent-os`` when
that directory exists on the developer's machine but doesn't contain the
expected layout.

Also sets ``AGENT_OS_SKIP_FIRST_BOOT_CHECK=1`` so the SkillIndexer
constructor doesn't run the lifecycle health check during tests that
build their own isolated indexes.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Assets are read from the checkout.  All mutable state belongs to one
# disposable runtime tree so a test can never edit a user's credentials,
# MCP registry, sessions, or configuration in the repository.
_TEST_RUNTIME_ROOT = Path(tempfile.mkdtemp(prefix="wise-pytest-runtime-"))
_TEST_SKILLS_ROOT = Path(tempfile.mkdtemp(prefix="wise-pytest-skills-"))
shutil.copytree(
    _REPO_ROOT / "skills", _TEST_SKILLS_ROOT, dirs_exist_ok=True,
    ignore=shutil.ignore_patterns(".archive", ".git", ".venv", "node_modules", "__pycache__", ".*cache*"),
)
os.environ["WISE_SKILLS_DIR"] = str(_TEST_SKILLS_ROOT)
os.environ.setdefault("AGENT_OS_ROOT", str(_REPO_ROOT))
os.environ["WISE_RUNTIME_ROOT"] = str(_TEST_RUNTIME_ROOT)
os.environ.setdefault("AGENT_OS_SKIP_FIRST_BOOT_CHECK", "1")


@atexit.register
def _remove_test_runtime_root() -> None:
    shutil.rmtree(_TEST_RUNTIME_ROOT, ignore_errors=True)
    shutil.rmtree(_TEST_SKILLS_ROOT, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_runtime_between_tests():
    """Keep stateful singletons and durable files from crossing test cases."""
    def clear_runtime() -> None:
        for child in _TEST_RUNTIME_ROOT.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    try:
        from core.browser import shutdown_browser_session
        shutdown_browser_session()
    except Exception:
        pass
    try:
        from core.models.provider_interface import reset_model_provider
        reset_model_provider()
    except Exception:
        pass
    clear_runtime()
    yield
    try:
        from core.browser import shutdown_browser_session
        shutdown_browser_session()
    except Exception:
        pass
    clear_runtime()
    try:
        from core.models.provider_interface import reset_model_provider
        reset_model_provider()
    except Exception:
        pass
