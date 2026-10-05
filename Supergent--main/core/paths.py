"""
Centralized path resolution for Agent OS.

Resolution order for AGENT_OS_ROOT:
  1. ``AGENT_OS_ROOT`` environment variable (if set and exists)
  2. The repository root (parent of this file's parent).

The old ``~/agent-os`` fallback created a second, unrelated runtime tree
whenever a developer machine happened to contain it.  That split model keys,
sessions and logs away from the running WISE checkout.  Operators who need a
separate runtime tree can still set ``AGENT_OS_ROOT`` explicitly.

Mutable runtime state is deliberately separate from the checkout.  Set
``WISE_RUNTIME_ROOT`` when an operator wants an explicit state location.  This
keeps logs, credentials, sessions and test artifacts out of versioned source
files while the immutable tool packs and bundled skills continue to come from
the repository.
"""

import os
from pathlib import Path


def _resolve_root() -> Path:
    env = os.environ.get("AGENT_OS_ROOT")
    if env:
        p = Path(env).expanduser().resolve()
        if p.exists():
            return p

    return Path(__file__).resolve().parent.parent


AGENT_OS_ROOT: Path = _resolve_root()

# The repository root (where this file lives) always ships the JSON tool packs
# and the pre-indexed skills library.  When AGENT_OS_ROOT points to the legacy
# ``~/agent-os`` runtime tree (which only stores writable state — memory, logs,
# config, workspace, …), we still need to read packs and skills from the repo
# checkout so that the agent registry isn't crippled.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent


def _resolve_runtime_root() -> Path:
    """Return the writable WISE state directory without changing asset lookup."""
    env = os.environ.get("WISE_RUNTIME_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return AGENT_OS_ROOT


RUNTIME_ROOT: Path = _resolve_runtime_root()


def _has_substantive_content(p: Path) -> bool:
    """A directory is "substantive" when it contains at least one file or
    subdirectory whose name does NOT begin with a dot. This skips legacy
    runtime trees that only hold cache files like ``.index_cache.json``.
    """
    try:
        if not p.exists():
            return False
        for child in p.iterdir():
            if not child.name.startswith("."):
                return True
    except Exception:
        return False
    return False


def _first_nonempty(*candidates: Path) -> Path:
    """Return the first existing **substantive** directory in ``candidates``.

    Falls back to the first existing candidate (even if empty/dot-only); if
    none exist, returns the first candidate so callers can ``mkdir`` later.
    """
    for c in candidates:
        if _has_substantive_content(c):
            return c
    for c in candidates:
        try:
            if c.exists():
                return c
        except Exception:
            continue
    return candidates[0]


TOOLS_DIR: Path = AGENT_OS_ROOT / "tools"
TOOLS_PACKS_DIR: Path = _first_nonempty(
    TOOLS_DIR / "packs",
    REPO_ROOT / "tools" / "packs",
)
SKILLS_DIR: Path = Path(os.environ["WISE_SKILLS_DIR"]).resolve() if os.environ.get("WISE_SKILLS_DIR") else _first_nonempty(
    AGENT_OS_ROOT / "skills",
    REPO_ROOT / "skills",
)
MEMORY_DIR: Path = RUNTIME_ROOT / "memory"
LOGS_DIR: Path = RUNTIME_ROOT / "logs"
CONFIG_DIR: Path = RUNTIME_ROOT / "config"
WORKSPACE_DIR: Path = RUNTIME_ROOT / "workspace"
SANDBOX_ROOT: Path = RUNTIME_ROOT / "sandbox" / "rootfs"
SESSIONS_DIR: Path = RUNTIME_ROOT / "sessions"
BACKUPS_DIR: Path = RUNTIME_ROOT / "backups"
CUSTOM_TOOLS_DIR: Path = TOOLS_DIR / "custom"


def resolve_config_file(filename: str) -> Path:
    """
    Deterministic path resolution for config files:
    1. Explicit environment override (WISE_CONFIG_DIR)
    2. Writable runtime state (RUNTIME_ROOT / 'config')
    3. Read-only repository defaults (REPO_ROOT / 'config')
    """
    env_cfg = os.environ.get("WISE_CONFIG_DIR")
    if env_cfg:
        p = Path(env_cfg) / filename
        if p.exists():
            return p

    runtime_p = CONFIG_DIR / filename
    if runtime_p.exists():
        return runtime_p

    repo_p = REPO_ROOT / "config" / filename
    if repo_p.exists():
        return repo_p

    return repo_p


def ensure_runtime_dirs() -> None:
    """Create runtime directories that the agent expects to write to."""
    for d in (MEMORY_DIR, LOGS_DIR, CONFIG_DIR, WORKSPACE_DIR,
              SESSIONS_DIR, BACKUPS_DIR, CUSTOM_TOOLS_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


__all__ = [
    "AGENT_OS_ROOT", "RUNTIME_ROOT", "REPO_ROOT", "TOOLS_DIR", "TOOLS_PACKS_DIR", "SKILLS_DIR",
    "MEMORY_DIR", "LOGS_DIR", "CONFIG_DIR", "WORKSPACE_DIR",
    "SANDBOX_ROOT", "SESSIONS_DIR", "BACKUPS_DIR", "CUSTOM_TOOLS_DIR",
    "resolve_config_file", "ensure_runtime_dirs",
]
