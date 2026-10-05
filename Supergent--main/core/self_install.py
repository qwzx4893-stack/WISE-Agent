"""Self-installation helpers — used by the model to extend its own
toolbox at runtime.

Hardened version (post-audit):

- ``pip_install`` auto-detects whether we're inside a venv and drops the
  ``--user`` flag in that case (``--user`` is incompatible with venvs).
- ``apt_install`` runs ``sudo -n`` so it fails fast instead of hanging on
  a password prompt.
- All entry points sanitise the package name and refuse known escape
  options.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Iterable, List

_PKG_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._\-]*"
    r"(?:\[[A-Za-z0-9,_\-]+\])?"
    r"(?:[<>=!~]=?[A-Za-z0-9._\-+*]+)?$"
)
_FORBIDDEN_PIP_OPTS = {
    "--index-url", "-i",
    "--extra-index-url",
    "--no-build-isolation",
    "--target", "-t",
    "--prefix",
    "--root",
    "--editable", "-e",
    "--find-links", "-f",
    "--trusted-host",
    "--user",  # we manage this flag ourselves
}


def _validate_package_name(name: str) -> bool:
    return bool(_PKG_RE.match(name))


def _in_venv() -> bool:
    """True when running inside a venv/virtualenv/conda env."""
    if hasattr(sys, "real_prefix"):  # legacy virtualenv
        return True
    if sys.base_prefix != sys.prefix:
        return True
    if os.environ.get("VIRTUAL_ENV"):
        return True
    if os.environ.get("CONDA_PREFIX"):
        return True
    return False


def pip_install(package: str, *extra_args: str) -> str:
    """Install one Python package via ``pip`` (``--user`` outside venvs)."""
    if not _validate_package_name(package):
        return f"❌ اسم حزمة غير صالح: {package!r}"
    bad = [a for a in extra_args if a in _FORBIDDEN_PIP_OPTS]
    if bad:
        return f"❌ خيارات pip محظورة: {bad}"

    cmd: List[str] = [
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", "--no-color",
    ]
    if not _in_venv():
        cmd.append("--user")
    cmd.extend(["--", package, *extra_args])

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
        )
        out = (proc.stdout + proc.stderr).strip()
        if proc.returncode != 0:
            return f"❌ فشل pip ({proc.returncode}):\n{out[-2000:]}"
        return f"✅ تم تثبيت {package}\n{out[-2000:]}"
    except subprocess.TimeoutExpired:
        return "❌ انتهت مهلة pip install."
    except Exception as e:
        return f"❌ {e}"


def apt_install(package: str) -> str:
    """Install one Debian package. Requires ``AGENT_APT_ALLOWED=1``."""
    if os.environ.get("AGENT_APT_ALLOWED", "").lower() not in {"1", "true", "yes"}:
        return "❌ apt مغلق. اضبط AGENT_APT_ALLOWED=1 لتفعيله."
    if not _validate_package_name(package):
        return f"❌ اسم حزمة غير صالح: {package!r}"

    cmd = ["apt-get", "install", "-y", "--no-install-recommends", "--", package]
    if os.geteuid() != 0:
        # ``-n`` means non-interactive: fail instead of prompting for a password.
        cmd = ["sudo", "-n"] + cmd
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        out = (proc.stdout + proc.stderr).strip()
        if proc.returncode != 0:
            return f"❌ فشل apt ({proc.returncode}):\n{out[-2000:]}"
        return f"✅ تم تثبيت {package}\n{out[-2000:]}"
    except subprocess.TimeoutExpired:
        return "❌ انتهت مهلة apt install."
    except Exception as e:
        return f"❌ {e}"


def git_clone_repo(url: str, dest_subdir: str = "external") -> str:
    """Delegate to ``SelfMaintenance.update_from_repo`` (allowlist enforced)."""
    if not isinstance(url, str) or not url.strip():
        return "❌ رابط مستودع غير صالح."
    if not isinstance(dest_subdir, str) or not dest_subdir.strip():
        return "❌ مسار وجهة غير صالح."
    if "/" in dest_subdir or ".." in dest_subdir:
        return "❌ dest_subdir يجب ألا يحوي '/' أو '..'."
    from .self_maintenance import SelfMaintenance
    return SelfMaintenance.update_from_repo(url.strip(), dest_subdir=dest_subdir.strip())


__all__ = ["pip_install", "apt_install", "git_clone_repo"]
