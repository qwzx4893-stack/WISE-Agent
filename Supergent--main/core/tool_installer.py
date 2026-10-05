"""Tool Installer
==================

Detects which Agent OS tools (defined as JSON manifests under
``tools/packs/*.json``) are *actually* runnable inside the active
sandbox/rootfs and which ones have only their manifest published.

The installer keeps a single, well-defined contract:

* :func:`scan` — read every pack, derive each tool's *requirements*
  (binary on ``$PATH`` and/or Python dependency installable through
  ``pip``), and classify the tool as ``available`` or ``missing``.
* :func:`missing_report` — summarise what's missing in a UI-friendly
  shape (binary name, package name, suggested install command).
* :func:`install_missing` — run the existing :mod:`core.self_install`
  helpers (``pip_install`` / ``apt_install``) inside the sandbox to make
  tools runnable.  Reuses the same allowlists / forbidden flags.

The installer never duplicates installer logic — it always delegates to
:mod:`core.self_install`.  Rate limiting, observability tracing, and
sandbox isolation are layered around it.
"""

from __future__ import annotations

import fnmatch
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .paths import SANDBOX_ROOT, TOOLS_PACKS_DIR

try:  # Tracer is optional at import time.
    from .observability import Tracer  # type: ignore
except Exception:  # pragma: no cover - degraded mode
    class Tracer:  # type: ignore
        @staticmethod
        def span(*a, **kw):
            class _N:
                def __enter__(self_inner): return self_inner
                def __exit__(self_inner, *e): return False
            return _N()
        @staticmethod
        def emit(*a, **kw): return None


# --------------------------------------------------------------------------
# Heuristics
# --------------------------------------------------------------------------
#: Dependencies that look like Python distribution names rather than apt
#: packages or npm modules. These are rough heuristics and only used when
#: the dependency string is ambiguous.
_PY_HINTS = {
    "django", "flask", "fastapi", "uvicorn", "starlette", "requests",
    "httpx", "aiohttp", "numpy", "pandas", "scipy", "scikit-learn",
    "sklearn", "torch", "tensorflow", "transformers", "openai",
    "anthropic", "google-generativeai", "boto3", "azure-identity",
    "google-cloud-storage", "redis", "psycopg2", "psycopg2-binary",
    "sqlalchemy", "pydantic", "pytest", "playwright", "selenium",
    "bs4", "beautifulsoup4", "lxml", "pyyaml", "jinja2", "rich",
    "typer", "click", "celery", "sentence-transformers", "chromadb",
    "langchain", "langchain-core", "langgraph", "litellm",
}

#: System packages (apt) that the agent might want to install.
_APT_HINTS = {
    "curl", "wget", "git", "jq", "make", "cmake", "build-essential",
    "ffmpeg", "imagemagick", "tesseract-ocr", "pandoc", "graphviz",
    "ripgrep", "tree", "unzip", "zip", "tar", "gzip", "bzip2", "xz-utils",
    "openssl", "ca-certificates", "sqlite3", "postgresql-client",
    "redis-tools", "nodejs", "npm", "yarn", "pnpm", "bun", "deno",
    "docker", "docker-ce", "docker.io", "podman", "kubectl",
    "python3", "python3-pip", "python3-venv", "ruby", "perl", "go",
    "golang", "rustc", "cargo", "java", "openjdk-17-jre",
}

#: Builtins shipped by the kernel — always treated as available.
_KERNEL_BUILTINS = {
    "execute_command", "search_knowledge", "list_skills", "load_skill",
    "search_skills", "read_file", "write_file", "list_directory",
    "grep_file", "search_memory", "remember", "ask_user",
    "execute_python", "git_clone", "web_fetch", "web_search", "run_shell",
    "safe_edit_file", "apply_patch", "rollback_file", "list_backups",
    "verify_python", "verify_json", "verify_yaml", "run_self_check",
    "tail_log", "pip_install", "apt_install", "git_clone_repo",
}

#: Tokens we must never accept as a binary name (placeholders / shell
#: glue produced by templating).
_INVALID_BINARY_RE = re.compile(r"^[\s'\"`{}]*$")


@dataclass
class ToolStatus:
    """Result of inspecting a single tool manifest."""

    name: str
    impl_type: str
    category: str
    required: List[Dict[str, str]] = field(default_factory=list)
    found: bool = False
    missing: List[Dict[str, str]] = field(default_factory=list)
    install_hint: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "impl_type": self.impl_type,
            "category": self.category,
            "required": self.required,
            "found": self.found,
            "missing": self.missing,
            "install_hint": self.install_hint,
            "note": self.note,
        }


# --------------------------------------------------------------------------
# Rate limiter (token bucket-style sliding window)
# --------------------------------------------------------------------------
class _RateLimiter:
    """Thread-safe sliding window: ``max_per_window`` per ``window_s``."""

    def __init__(self, max_per_window: int = 10, window_s: float = 60.0):
        self.max = max_per_window
        self.window = window_s
        self._events: deque = deque()
        self._lock = threading.Lock()

    def check(self) -> Tuple[bool, float]:
        """Return ``(allowed, retry_after_s)``.

        ``retry_after_s`` is 0 when allowed.
        """
        now = time.monotonic()
        with self._lock:
            while self._events and now - self._events[0] > self.window:
                self._events.popleft()
            if len(self._events) >= self.max:
                retry = self.window - (now - self._events[0])
                return False, max(retry, 0.0)
            self._events.append(now)
            return True, 0.0


# --------------------------------------------------------------------------
# ToolInstaller
# --------------------------------------------------------------------------
class ToolInstaller:
    """Discovers, classifies and (optionally) installs tools."""

    def __init__(self,
                 packs_dir: Optional[Path] = None,
                 rootfs: Optional[Path] = None,
                 cache_ttl_s: float = 30.0,
                 rate_max_per_min: int = 10,
                 curated_map_path: Optional[Path] = None):
        self.packs_dir = Path(packs_dir) if packs_dir else TOOLS_PACKS_DIR
        self.rootfs = Path(rootfs) if rootfs else Path(SANDBOX_ROOT)
        self.cache_ttl_s = cache_ttl_s
        self._cache: Dict[str, ToolStatus] = {}
        self._cache_at: float = 0.0
        self._lock = threading.Lock()
        self.rate_limiter = _RateLimiter(rate_max_per_min, 60.0)
        self.curated_map_path = (Path(curated_map_path)
                                  if curated_map_path
                                  else Path(__file__).with_name(
                                      "tool_packages.json"))
        self._curated: Dict[str, Dict[str, Any]] = self._load_curated()

    def _load_curated(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self.curated_map_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {}
        return {k: v for k, v in data.items()
                if isinstance(v, dict) and not k.startswith("_")}

    # ------------------------------------------------------------------
    # Reading manifests
    # ------------------------------------------------------------------
    def _iter_manifests(self) -> Iterable[Tuple[str, Dict[str, Any]]]:
        if not self.packs_dir.exists():
            return
        for pack_file in sorted(self.packs_dir.glob("*.json")):
            try:
                with open(pack_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            entries = data if isinstance(data, list) else [data]
            for raw in entries:
                if not isinstance(raw, dict):
                    continue
                name = raw.get("name")
                if not isinstance(name, str) or not name:
                    continue
                yield pack_file.name, raw

    # ------------------------------------------------------------------
    # Requirement extraction
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_binary_from_command(cmd: str) -> Optional[str]:
        """Pull the first runnable binary name out of a templated command."""
        if not cmd:
            return None
        # Strip ``{placeholders}`` so they don't fool shlex.
        cleaned = re.sub(r"\{[^{}]*\}", "_", cmd).strip()
        # Strip subshells/loops noise.
        cleaned = cleaned.lstrip("$()`")
        try:
            tokens = shlex.split(cleaned, posix=True)
        except ValueError:
            tokens = cleaned.split()
        if not tokens:
            return None
        first = tokens[0]
        # `python3 -c '...'` and `npx ...` count their first token.
        if _INVALID_BINARY_RE.match(first or ""):
            return None
        # Skip "env-style" prefixes like VAR=value.
        if "=" in first and not first.endswith("="):
            for tok in tokens[1:]:
                if "=" not in tok:
                    return tok
            return None
        return first

    @classmethod
    def _classify_dependency(cls, dep: str) -> Dict[str, str]:
        """Decide whether a dep string is python / apt / npm / binary."""
        d = dep.strip()
        if not d:
            return {"type": "unknown", "name": d}
        low = d.lower()
        if low.startswith("python:"):
            return {"type": "python", "name": d.split(":", 1)[1]}
        if low.startswith("apt:"):
            return {"type": "apt", "name": d.split(":", 1)[1]}
        if low.startswith("npm:") or d.startswith("@"):
            return {"type": "npm",
                     "name": d.split(":", 1)[1] if ":" in d else d}
        if low in _APT_HINTS:
            return {"type": "apt", "name": low}
        if low in _PY_HINTS:
            return {"type": "python", "name": low}
        # Simple heuristic: dashed-lowercase + valid PEP503 → python.
        if re.match(r"^[a-z][a-z0-9_\-]*$", low):
            return {"type": "python", "name": low}
        return {"type": "unknown", "name": d}

    def _curated_requirements(self, curated: Dict[str, Any]
                              ) -> List[Dict[str, str]]:
        """Translate a curated map entry into a single requirement so the
        availability checker (``_check``) can verify it.
        """
        t = curated.get("type")
        if t == "pip":
            return [{"type": "python", "name": str(curated.get("package", ""))}]
        if t in ("apt", "apt_optional"):
            return [{"type": "apt", "name": str(curated.get("package", ""))}]
        if t == "npm":
            return [{"type": "npm", "name": str(curated.get("package", ""))}]
        if t in ("github_release", "binary_zip"):
            return [{"type": "binary",
                     "name": str(curated.get("binary")
                                  or curated.get("package") or "")}]
        return []

    def _requirements_for(self, manifest: Dict[str, Any]
                          ) -> List[Dict[str, str]]:
        impl = (manifest.get("implementation_type") or "python").lower()
        tool_name = manifest.get("name", "").lower()
        reqs: List[Dict[str, str]] = []
        if impl == "cli":
            binary = manifest.get("cli_binary")
            if not binary:
                binary = self._extract_binary_from_command(
                    manifest.get("cli_command") or "")
            if binary:
                reqs.append({"type": "binary", "name": binary})
        for dep in (manifest.get("dependencies") or []):
            if not isinstance(dep, str):
                continue
            # Skip self-references — packs commonly list the tool's own
            # name as a 'dependency' which would otherwise be installed
            # as a (non-existent) PyPI package.
            if dep.strip().lower() == tool_name:
                continue
            cls = self._classify_dependency(dep)
            if cls["type"] == "unknown":
                continue
            # Skip duplicates.
            if any(r["type"] == cls["type"] and r["name"] == cls["name"]
                   for r in reqs):
                continue
            reqs.append(cls)
        return reqs

    # ------------------------------------------------------------------
    # Availability checks
    # ------------------------------------------------------------------
    def _binary_exists(self, name: str) -> bool:
        # Look first inside rootfs, then on host PATH (when rootfs is unset
        # or empty — useful for development boxes).
        if self.rootfs.exists():
            for d in ("usr/local/bin", "usr/bin", "bin", "sbin",
                       "usr/sbin"):
                p = self.rootfs / d / name
                if p.is_file() and os.access(p, os.X_OK):
                    return True
        # Host fallback.
        return shutil.which(name) is not None

    def _python_dist_installed(self, name: str) -> bool:
        try:
            import importlib.metadata as md
            md.distribution(name)
            return True
        except Exception:
            pass
        # Some packages register under an importable module name only.
        mod = name.replace("-", "_")
        try:
            __import__(mod)
            return True
        except Exception:
            return False

    def _apt_installed(self, name: str) -> bool:
        # ``dpkg -s`` is the cheapest reliable check; if dpkg is absent
        # we fall back to ``which`` heuristics.
        if shutil.which("dpkg") is not None:
            try:
                proc = subprocess.run(
                    ["dpkg", "-s", name],
                    capture_output=True, text=True, timeout=5,
                )
                if proc.returncode == 0:
                    return True
            except Exception:
                pass
        return self._binary_exists(name)

    def _check(self, req: Dict[str, str]) -> bool:
        t, n = req["type"], req["name"]
        if t == "binary":
            return self._binary_exists(n)
        if t == "python":
            return self._python_dist_installed(n)
        if t == "apt":
            return self._apt_installed(n)
        if t == "npm":
            return self._binary_exists(n) or self._binary_exists("npx")
        return True  # unknown reqs don't block tools.

    @staticmethod
    def _hint(missing: List[Dict[str, str]]) -> str:
        parts: List[str] = []
        py = [m["name"] for m in missing if m["type"] == "python"]
        apt = [m["name"] for m in missing if m["type"] == "apt"]
        npm = [m["name"] for m in missing if m["type"] == "npm"]
        bins = [m["name"] for m in missing if m["type"] == "binary"]
        if py:
            parts.append(f"pip_install {' '.join(py)}")
        if apt:
            parts.append(f"apt_install {' '.join(apt)}")
        if npm:
            parts.append(f"npm install -g {' '.join(npm)}")
        if bins:
            parts.append("install missing binary: " + ", ".join(bins))
        return " ; ".join(parts)

    # ------------------------------------------------------------------
    # Public surface — scan / status
    # ------------------------------------------------------------------
    def scan(self, force: bool = False) -> Dict[str, ToolStatus]:
        with self._lock:
            now = time.monotonic()
            if (not force and self._cache
                    and now - self._cache_at < self.cache_ttl_s):
                return dict(self._cache)
            results: Dict[str, ToolStatus] = {}
            for _pack, manifest in self._iter_manifests():
                name = manifest["name"]
                if name in results:
                    continue
                impl = manifest.get("implementation_type", "python")
                category = manifest.get("category", "")
                if name in _KERNEL_BUILTINS:
                    results[name] = ToolStatus(
                        name=name, impl_type=impl, category=category,
                        required=[], found=True,
                        note="kernel builtin",
                    )
                    continue
                # Curated map override: declared ``skill``/``skip`` entries
                # are conceptually fulfilled by Agent OS layers (skills,
                # adapters, MCP) and should not be reported as missing.
                curated = self._curated.get(name)
                if curated and curated.get("type") in ("skill", "skip"):
                    note = curated.get("note") or (
                        "fulfilled by Agent OS skills/adapters layer")
                    results[name] = ToolStatus(
                        name=name, impl_type=impl, category=category,
                        required=[], found=True,
                        note=f"curated:{curated['type']} — {note}",
                    )
                    continue
                if curated and curated.get("type") in (
                        "pip", "apt", "apt_optional", "npm",
                        "github_release", "binary_zip"):
                    reqs = self._curated_requirements(curated)
                else:
                    reqs = self._requirements_for(manifest)
                missing = [r for r in reqs if not self._check(r)]
                status = ToolStatus(
                    name=name,
                    impl_type=impl,
                    category=category,
                    required=reqs,
                    found=(not missing),
                    missing=missing,
                )
                if missing:
                    status.install_hint = self._hint(missing)
                results[name] = status
            self._cache = results
            self._cache_at = now
            return dict(results)

    def status(self, name: str) -> Optional[ToolStatus]:
        return self.scan().get(name)

    def missing_report(self) -> Dict[str, Any]:
        scanned = self.scan()
        missing = [s.to_dict() for s in scanned.values() if not s.found]
        return {
            "total": len(scanned),
            "available": sum(1 for s in scanned.values() if s.found),
            "missing_count": len(missing),
            "missing": sorted(missing, key=lambda d: d["name"]),
        }

    # ------------------------------------------------------------------
    # Rootfs + known_missing
    # ------------------------------------------------------------------
    def rootfs_info(self) -> Dict[str, Any]:
        """Return metadata about the configured sandbox rootfs."""
        info: Dict[str, Any] = {
            "path": str(self.rootfs),
            "exists": self.rootfs.exists(),
            "bytes": 0,
            "debian_version": None,
            "stamp": None,
        }
        if not self.rootfs.exists():
            return info
        try:
            total = 0
            for p in self.rootfs.rglob("*"):
                try:
                    total += p.stat().st_size
                except Exception:
                    pass
            info["bytes"] = total
        except Exception:
            pass
        for f in ("etc/debian_version", "etc/os-release"):
            p = self.rootfs / f
            if p.exists():
                try:
                    info["debian_version"] = p.read_text(
                        encoding="utf-8", errors="replace").strip().splitlines()[0]
                    break
                except Exception:
                    pass
        stamp = self.rootfs / ".agent-os-stamp"
        if stamp.exists():
            try:
                info["stamp"] = dict(
                    line.split("=", 1) for line in
                    stamp.read_text(encoding="utf-8").splitlines()
                    if "=" in line
                )
            except Exception:
                info["stamp"] = None
        return info

    def write_known_missing(self, output: Path) -> Dict[str, Any]:
        """Write a JSON describing tools that have no installable channel.

        Each entry includes the curated reason (or 'no curated entry')
        and any manual install hint we can offer.
        """
        scan = self.scan(force=True)
        rows: List[Dict[str, Any]] = []
        for name, status in sorted(scan.items()):
            if status.found:
                continue
            curated = self._curated.get(name, {})
            ctype = curated.get("type")
            if ctype in ("pip", "apt", "npm",
                          "github_release", "binary_zip"):
                # Auto-installable — not "known missing".
                continue
            if ctype is None:
                # No curated entry: only flag as known-missing when none
                # of the detected requirements look auto-installable.
                kinds = {r.get("type") for r in status.required}
                if kinds & {"python", "apt", "npm"}:
                    continue
            rows.append({
                "name": name,
                "category": status.category,
                "impl_type": status.impl_type,
                "channel": ctype or "auto",
                "reason": curated.get("note") or (
                    "no curated install channel; manifest may be a skill "
                    "name rather than a real CLI"),
                "manual_steps": _manual_steps_for(name, curated),
                "missing_requirements": status.missing,
            })
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime()),
            "count": len(rows),
            "tools": rows,
        }
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2,
                                       ensure_ascii=False),
                          encoding="utf-8")
        return payload

    # ------------------------------------------------------------------
    # Public surface — install
    # ------------------------------------------------------------------
    def install_missing(self,
                        names: Optional[Iterable[str]] = None
                        ) -> Dict[str, Any]:
        """Install missing dependencies for the given tool names.

        When *names* is empty/None, installs missing deps for *every*
        missing tool. Each pip / apt / npm action is rate-limited (see
        :class:`_RateLimiter`) so a runaway request can't drain the box.
        """
        from . import self_install  # reuse, do not duplicate

        scanned = self.scan(force=True)
        targets: List[str]
        if names:
            targets = [n for n in names if n in scanned]
            unknown = [n for n in names if n not in scanned]
        else:
            targets = [s.name for s in scanned.values() if not s.found]
            unknown = []

        installed: List[Dict[str, str]] = []
        failed: List[Dict[str, str]] = []
        skipped: List[Dict[str, str]] = []
        for n in unknown:
            skipped.append({"tool": n, "reason": "unknown tool"})

        # Deduplicate package installs across tools so e.g. multiple
        # tools depending on `requests` only install it once.
        seen_pkgs: Dict[Tuple[str, str], bool] = {}
        for tool_name in targets:
            status = scanned[tool_name]
            if status.found:
                skipped.append({"tool": tool_name,
                                 "reason": "already available"})
                continue
            for req in status.missing:
                key = (req["type"], req["name"])
                if key in seen_pkgs:
                    continue
                seen_pkgs[key] = True
                ok, retry = self.rate_limiter.check()
                if not ok:
                    failed.append({"tool": tool_name, "type": req["type"],
                                   "name": req["name"],
                                   "reason": f"rate limited; retry in "
                                              f"{retry:.1f}s"})
                    Tracer.emit("tool.install.rate_limited",
                                tool=tool_name, package=req["name"],
                                type=req["type"], retry=retry)
                    continue
                # Curated install hint takes precedence over the default
                # dispatch so e.g. github_release tools land at the right
                # URL and apt_optional packages can be flagged.
                curated = self._curated.get(tool_name, {})
                with Tracer.span("tool.install",
                                  tool=tool_name,
                                  package=req["name"],
                                  type=req["type"],
                                  curated=curated.get("type", "auto")):
                    result = self._install_one(req, self_install,
                                                curated)
                entry = {"tool": tool_name,
                         "type": req["type"],
                         "name": req["name"],
                         "channel": curated.get("type", "auto"),
                         "result": result[:1500]}
                if result.startswith("✅"):
                    installed.append(entry)
                else:
                    failed.append(entry)
        # Invalidate cache so subsequent scans reflect the new state.
        with self._lock:
            self._cache.clear()
            self._cache_at = 0.0
        return {"installed": installed, "failed": failed,
                "skipped": skipped,
                "rate_limit": {"max_per_minute": self.rate_limiter.max}}

    def _install_in_rootfs(self, kind: str, name: str) -> Optional[str]:
        """Run pip / apt / npm inside the sandbox rootfs via proot.

        Returns None when the rootfs is not usable (missing, no proot
        on host, or required interpreter absent). Callers fall back to
        the host self_install on None.
        """
        if not self.rootfs.exists():
            return None
        proot = shutil.which("proot")
        if proot is None:
            return None
        env = ["env", "HOME=/root", "PATH=/usr/local/sbin:/usr/local/bin:"
                "/usr/sbin:/usr/bin:/sbin:/bin"]
        if kind == "pip":
            if not (self.rootfs / "usr" / "bin" / "python3").exists():
                return None
            cmd = [proot, "-r", str(self.rootfs), "-0", "-w", "/root",
                   *env,
                   "python3", "-m", "pip", "install",
                   "--break-system-packages", "--disable-pip-version-check",
                   "--no-color", "--no-cache-dir", "--", name]
        elif kind == "apt":
            cmd = [proot, "-r", str(self.rootfs), "-0", "-w", "/root",
                   *env, "apt-get", "install", "-y",
                   "--no-install-recommends", "--", name]
        elif kind == "npm":
            if not (self.rootfs / "usr" / "bin" / "npm").exists():
                return None
            if not re.match(r"^[@A-Za-z0-9._/\-]+$", name):
                return f"❌ اسم npm غير صالح: {name!r}"
            cmd = [proot, "-r", str(self.rootfs), "-0", "-w", "/root",
                   *env, "npm", "install", "-g",
                   "--no-audit", "--no-fund", "--", name]
        else:
            return None
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=600)
            out = (proc.stdout + proc.stderr).strip()
            if proc.returncode != 0:
                return f"❌ فشل {kind} في rootfs ({proc.returncode}):\n{out[-2000:]}"
            return f"✅ تم تثبيت {name} داخل rootfs\n{out[-1500:]}"
        except subprocess.TimeoutExpired:
            return f"❌ انتهت مهلة {kind} install داخل rootfs."
        except Exception as e:
            return f"❌ {e}"

    # ------------------------------------------------------------------
    # Binary downloader (github_release + direct binary_zip)
    # ------------------------------------------------------------------
    @staticmethod
    def _arch_tokens() -> Dict[str, List[str]]:
        machine = platform.machine().lower()
        system = platform.system().lower()
        machine_aliases = {
            "x86_64":  ["x86_64", "amd64", "x64", "linux64"],
            "amd64":   ["x86_64", "amd64", "x64", "linux64"],
            "aarch64": ["aarch64", "arm64"],
            "arm64":   ["aarch64", "arm64"],
            "armv7l":  ["armv7", "arm"],
        }
        return {
            "system": [system],
            "arch": machine_aliases.get(machine, [machine]),
        }

    @classmethod
    def _resolve_asset_pattern(cls, pattern: str) -> str:
        """Replace ``{arch}`` / ``{os}`` tokens then keep glob ``*``."""
        sys_tokens = cls._arch_tokens()
        out = pattern
        out = out.replace("{os}", sys_tokens["system"][0])
        out = out.replace("{arch}", sys_tokens["arch"][0])
        return out

    def _binary_install_dir(self) -> Path:
        if self.rootfs.exists():
            d = self.rootfs / "usr" / "local" / "bin"
        else:
            d = Path.home() / ".local" / "bin"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _http_get_json(url: str, timeout: float = 15.0) -> Any:
        req = urllib.request.Request(
            url, headers={"User-Agent": "agent-os-tool-installer/1.0",
                          "Accept": "application/vnd.github+json"})
        token = None
        try:
            from .secrets_store import get_secret
            token = get_secret("github_token")
        except Exception:
            token = None
        if not token:
            token = (os.environ.get("GITHUB_TOKEN")
                      or os.environ.get("GH_TOKEN"))
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    @staticmethod
    def _http_download(url: str, dest: Path, timeout: float = 120.0
                        ) -> None:
        req = urllib.request.Request(
            url, headers={"User-Agent": "agent-os-tool-installer/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r, \
                open(dest, "wb") as f:
            shutil.copyfileobj(r, f)

    def _extract_archive(self, archive: Path, into: Path,
                          binary_name: str) -> Optional[Path]:
        """Extract archive to *into* and return path of *binary_name*."""
        into.mkdir(parents=True, exist_ok=True)
        suf = archive.name.lower()
        if suf.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")):
            mode = "r:*"
            with tarfile.open(archive, mode) as tar:
                try:
                    tar.extractall(path=into, filter="data")
                except TypeError as exc:
                    raise RuntimeError("Safe tar extraction requires Python 3.12 or newer") from exc
        elif suf.endswith(".zip"):
            with zipfile.ZipFile(archive, "r") as zf:
                members = zf.infolist()
                if len(members) > 10000 or sum(m.file_size for m in members) > 512 * 1024 * 1024:
                    raise ValueError("Archive exceeds extraction limits")
                root = into.resolve()
                for member in members:
                    target = (root / member.filename.replace("\\", "/")).resolve()
                    if root != target and root not in target.parents:
                        raise ValueError("Archive member escapes extraction directory")
                    if stat.S_ISLNK(member.external_attr >> 16):
                        raise ValueError("Archive symlinks are not allowed")
                zf.extractall(into)
        else:
            # raw binary — no extraction
            target = into / binary_name
            shutil.copy2(archive, target)
            return target
        # Locate the binary anywhere inside *into*
        for p in into.rglob(binary_name):
            if p.is_file() and not p.is_symlink():
                return p
        # Fallback: any executable file
        for p in into.rglob("*"):
            if p.is_file() and os.access(p, os.X_OK):
                return p
        return None

    def _install_github_release(self, name: str, curated: Dict[str, Any]
                                  ) -> str:
        if os.environ.get("AGENT_BINARY_INSTALL", "").lower() not in {
                "1", "true", "yes"}:
            return (f"⏭️ تم تخطي {name}: تنزيل binaries مغلق. "
                    f"اضبط AGENT_BINARY_INSTALL=1 لتفعيله.")
        repo = curated.get("repo", "")
        if not re.match(r"^[A-Za-z0-9._\-]+/[A-Za-z0-9._\-]+$", repo):
            return f"❌ repo غير صالح: {repo!r}"
        pattern = self._resolve_asset_pattern(curated.get("asset", ""))
        if not pattern:
            return f"❌ asset pattern مفقود لـ{name}"
        binary = curated.get("binary") or name
        # Fast path: if the asset filename has no wildcards we can use
        # GitHub's stable redirect endpoint and avoid the API quota.
        if "*" not in pattern and "?" not in pattern:
            url = (f"https://github.com/{repo}/releases/latest/"
                   f"download/{pattern}")
            asset_name = pattern
        else:
            api = f"https://api.github.com/repos/{repo}/releases/latest"
            try:
                release = self._http_get_json(api)
            except urllib.error.HTTPError as e:
                return f"❌ GitHub API ({e.code}) لـ{repo}: {e.reason}"
            except Exception as e:
                return f"❌ تعذّر الاتصال بـ{api}: {e}"
            assets = release.get("assets") or []
            match = next((a for a in assets
                           if fnmatch.fnmatch(a.get("name", ""), pattern)),
                         None)
            if match is None:
                available = ", ".join(a.get("name", "") for a in assets[:5])
                return (f"❌ لم أجد أصلاً يطابق {pattern!r} في {repo}. "
                        f"المتاح: {available}...")
            url = match.get("browser_download_url")
            asset_name = match.get("name", pattern)
            if not url:
                return f"❌ asset {asset_name} بلا رابط"
        with tempfile.TemporaryDirectory(prefix="agent-os-rel-") as td:
            td = Path(td)
            archive = td / asset_name
            try:
                self._http_download(url, archive)
            except Exception as e:
                return f"❌ فشل التنزيل {url}: {e}"
            extracted = self._extract_archive(archive, td / "x", binary)
            if extracted is None:
                return (f"❌ لم أعثر على binary '{binary}' داخل "
                        f"{asset_name}")
            dest_dir = self._binary_install_dir()
            dest = dest_dir / binary
            try:
                shutil.copy2(extracted, dest)
                dest.chmod(dest.stat().st_mode | stat.S_IXUSR
                           | stat.S_IXGRP | stat.S_IXOTH)
            except Exception as e:
                return f"❌ فشل النسخ إلى {dest}: {e}"
        return (f"✅ تم تثبيت {name} ({binary}) من {repo} "
                f"إلى {dest}")

    def _install_binary_zip(self, name: str, curated: Dict[str, Any]
                              ) -> str:
        if os.environ.get("AGENT_BINARY_INSTALL", "").lower() not in {
                "1", "true", "yes"}:
            return (f"⏭️ تم تخطي {name}: تنزيل binaries مغلق. "
                    f"اضبط AGENT_BINARY_INSTALL=1 لتفعيله.")
        url = curated.get("url", "")
        if not url.startswith(("https://", "http://")):
            return f"❌ url غير صالح لـ{name}: {url!r}"
        binary = curated.get("binary") or name
        with tempfile.TemporaryDirectory(prefix="agent-os-bin-") as td:
            td = Path(td)
            archive = td / Path(url).name
            try:
                self._http_download(url, archive)
            except Exception as e:
                return f"❌ فشل التنزيل {url}: {e}"
            extracted = self._extract_archive(archive, td / "x", binary)
            if extracted is None:
                return f"❌ لم أعثر على binary '{binary}' داخل {url}"
            dest_dir = self._binary_install_dir()
            dest = dest_dir / binary
            try:
                shutil.copy2(extracted, dest)
                dest.chmod(dest.stat().st_mode | stat.S_IXUSR
                           | stat.S_IXGRP | stat.S_IXOTH)
            except Exception as e:
                return f"❌ فشل النسخ إلى {dest}: {e}"
        return f"✅ تم تثبيت {name} ({binary}) من {url} إلى {dest}"

    # ------------------------------------------------------------------
    # Prerequisites
    # ------------------------------------------------------------------
    def _install_prerequisites(self, curated: Dict[str, Any], self_install
                                 ) -> List[str]:
        """Install ``prerequisites`` declared on a curated entry.

        Each prerequisite is a dict ``{type: pip|apt|npm|shell, ...}``
        run sequentially. Failures are captured but do not abort the
        primary install (the primary will surface its own failure).
        """
        prereqs = curated.get("prerequisites") or []
        if not isinstance(prereqs, list):
            return []
        outcomes: List[str] = []
        for p in prereqs:
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t == "pip":
                pkg = p.get("package", "")
                r = self._install_in_rootfs("pip", pkg)
                outcomes.append(r if r is not None
                                 else self_install.pip_install(pkg))
            elif t == "apt":
                pkg = p.get("package", "")
                r = self._install_in_rootfs("apt", pkg)
                outcomes.append(r if r is not None
                                 else self_install.apt_install(pkg))
            elif t == "npm":
                pkg = p.get("package", "")
                r = self._install_in_rootfs("npm", pkg)
                outcomes.append(r if r is not None
                                 else ToolInstaller._npm_install(pkg))
            elif t == "shell":
                # Shell prereqs run only when explicitly enabled — the
                # operator must opt in via AGENT_SHELL_PREREQ=1 because
                # the command can do anything.
                if os.environ.get("AGENT_SHELL_PREREQ", "").lower() not in {
                        "1", "true", "yes"}:
                    outcomes.append(
                        f"⏭️ shell prereq معطَّل (AGENT_SHELL_PREREQ=1)")
                    continue
                cmd = p.get("run", "")
                try:
                    proc = subprocess.run(
                        ["bash", "-lc", cmd],
                        capture_output=True, text=True, timeout=300)
                    outcomes.append(
                        f"shell({proc.returncode}): "
                        f"{(proc.stdout + proc.stderr).strip()[-500:]}")
                except Exception as e:
                    outcomes.append(f"shell error: {e}")
        return outcomes

    def _install_one(self, req: Dict[str, str], self_install,
                      curated: Optional[Dict[str, Any]] = None) -> str:
        t, n = req["type"], req["name"]
        curated = curated or {}
        ctype = curated.get("type")
        prereq_log = self._install_prerequisites(curated, self_install) \
            if curated.get("prerequisites") else []
        if t == "python":
            # Prefer in-rootfs installation when the sandbox is built;
            # fall back to host-level self_install otherwise so we keep
            # working in development environments.
            r = self._install_in_rootfs("pip", n)
            if r is not None:
                return r
            return self_install.pip_install(n)
        if t == "apt":
            if ctype == "apt_optional":
                note = curated.get("note") or ""
                if os.environ.get("AGENT_APT_OPTIONAL", "").lower() not in {
                        "1", "true", "yes"}:
                    return (f"⏭️ تم تخطي {n}: حزمة اختيارية. "
                            f"اضبط AGENT_APT_OPTIONAL=1 للتثبيت. {note}")
            r = self._install_in_rootfs("apt", n)
            if r is not None:
                return r
            return self_install.apt_install(n)
        if t == "npm":
            r = self._install_in_rootfs("npm", n)
            if r is not None:
                return r
            return ToolInstaller._npm_install(n)
        if t == "binary":
            if ctype == "github_release":
                msg = self._install_github_release(n, curated)
            elif ctype == "binary_zip":
                msg = self._install_binary_zip(n, curated)
            else:
                msg = (f"❌ binary '{n}' must be installed via apt/npm; "
                       f"add a typed dependency to the pack manifest")
            if prereq_log:
                msg = "prereqs:\n  " + "\n  ".join(prereq_log) + "\n" + msg
            return msg
        return f"❌ نوع غير معروف: {t}"

    @staticmethod
    def _npm_install(name: str) -> str:
        if os.environ.get("AGENT_NPM_ALLOWED", "").lower() not in {
                "1", "true", "yes"}:
            return "❌ npm مغلق. اضبط AGENT_NPM_ALLOWED=1 لتفعيله."
        if not re.match(r"^[@A-Za-z0-9._/\-]+$", name):
            return f"❌ اسم npm غير صالح: {name!r}"
        cmd = ["npm", "install", "-g", "--no-audit", "--no-fund", "--",
               name]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=300)
            out = (proc.stdout + proc.stderr).strip()[-2000:]
            if proc.returncode != 0:
                return f"❌ فشل npm ({proc.returncode}):\n{out}"
            return f"✅ تم تثبيت {name}\n{out}"
        except subprocess.TimeoutExpired:
            return "❌ انتهت مهلة npm install."
        except Exception as e:
            return f"❌ {e}"


def _manual_steps_for(name: str, curated: Dict[str, Any]) -> List[str]:
    t = curated.get("type")
    if t == "github_release":
        return [f"Download the latest release of "
                f"{curated.get('repo')} → asset matching "
                f"'{curated.get('asset')}', extract, and place "
                f"'{curated.get('binary', name)}' on $PATH."]
    if t == "binary_zip":
        return [f"Download {curated.get('url')}, extract, and place "
                f"'{curated.get('binary', name)}' on $PATH."]
    if t == "apt_optional":
        note = curated.get("note") or ""
        return [f"Set AGENT_APT_OPTIONAL=1 then run apt_install "
                f"{curated.get('package', name)}.", note]
    if t in ("skill", "skip"):
        return [curated.get("note")
                or f"'{name}' is fulfilled by Agent OS layers; no "
                   "binary install is required."]
    return ["No curated install channel. Add an entry to "
            "core/tool_packages.json describing how this tool ships."]


def interactive_install_prompt(installer: Optional[ToolInstaller] = None
                                ) -> str:
    """Boot-time hook: ask the operator whether to install missing deps.

    * Returns ``'skipped'`` when nothing is missing.
    * Honours ``AGENT_AUTO_INSTALL`` (``1``/``true`` → unattended yes,
      ``0``/``false`` → unattended no).
    * Falls back to ``input()`` only when stdin is a TTY; otherwise
      defaults to ``no`` to keep production runs deterministic.
    """
    inst = installer or get_installer()
    rep = inst.missing_report()
    if rep["missing_count"] == 0:
        return "skipped"
    auto = os.environ.get("AGENT_AUTO_INSTALL", "").lower()
    if auto in {"1", "true", "yes"}:
        decision = "y"
    elif auto in {"0", "false", "no"}:
        decision = "n"
    elif sys.stdin.isatty() and sys.stdout.isatty():
        prompt = (f"📦 {rep['missing_count']} tool(s) need installation. "
                  f"Install now? (y/n) ")
        try:
            decision = (input(prompt) or "").strip().lower()[:1]
        except EOFError:
            decision = "n"
    else:
        decision = "n"
    if decision == "y":
        result = inst.install_missing()
        return (f"installed={len(result['installed'])} "
                f"failed={len(result['failed'])} "
                f"skipped={len(result['skipped'])}")
    return f"skipped ({rep['missing_count']} still missing)"


# --------------------------------------------------------------------------
# Module-level singleton helpers
# --------------------------------------------------------------------------
_default_installer: Optional[ToolInstaller] = None


def get_installer() -> ToolInstaller:
    global _default_installer
    if _default_installer is None:
        _default_installer = ToolInstaller()
    return _default_installer


# --------------------------------------------------------------------------
# Awareness / runtime integration helpers
# --------------------------------------------------------------------------
def annotate_awareness(awareness: Any,
                       installer: Optional[ToolInstaller] = None) -> int:
    """Mark every manifest in ``awareness.tools`` with install metadata.

    Returns the number of tools tagged as missing.
    """
    if awareness is None or not hasattr(awareness, "tools"):
        return 0
    inst = installer or get_installer()
    scan = inst.scan()
    missing = 0
    for name, manifest in awareness.tools.items():
        st = scan.get(name)
        if st is None:
            continue
        manifest["__install_status__"] = {
            "found": st.found,
            "missing": st.missing,
            "install_hint": st.install_hint,
        }
        if not st.found:
            missing += 1
    return missing


def wrap_registry_with_install_check(registry: Any,
                                      installer: Optional[ToolInstaller] = None
                                      ) -> int:
    """Wrap registry callables so missing-tool calls return a helpful
    message instead of crashing. Returns the number of wrappers added."""
    if registry is None:
        return 0
    inst = installer or get_installer()
    scan = inst.scan()
    wrapped = 0
    for name, status in scan.items():
        if status.found:
            continue
        existing = None
        if hasattr(registry, "get"):
            try:
                existing = registry.get(name)
            except Exception:
                existing = None
        if existing is None:
            continue
        already = getattr(existing, "__install_guard__", False)
        if already:
            continue
        guarded = _make_install_guard(name, status, existing)
        try:
            if hasattr(registry, "register"):
                registry.register(name, guarded)
            elif hasattr(registry, "tools"):
                registry.tools[name] = guarded
            wrapped += 1
        except Exception:
            continue
    return wrapped


def _make_install_guard(name: str, status: ToolStatus,
                         original: Callable[..., Any]
                         ) -> Callable[..., Any]:
    hint = status.install_hint or "POST /admin/tools/install"
    missing_pkgs = ", ".join(f"{m['type']}:{m['name']}"
                              for m in status.missing) or "?"
    msg = (f"⚠️ الأداة '{name}' غير مثبَّتة في الـsandbox "
           f"(ينقص: {missing_pkgs}). "
           f"شغّل: POST /admin/tools/install أو نفّذ يدوياً: {hint}")

    def _guard(*args: Any, **kwargs: Any) -> str:
        Tracer.emit("tool.missing", tool=name,
                    missing=[m["name"] for m in status.missing])
        return msg

    _guard.__name__ = f"missing_{name}"
    _guard.__doc__ = msg
    _guard.__install_guard__ = True  # type: ignore[attr-defined]
    _guard.__wrapped_tool__ = original  # type: ignore[attr-defined]
    return _guard


__all__ = [
    "ToolStatus",
    "ToolInstaller",
    "get_installer",
    "annotate_awareness",
    "wrap_registry_with_install_check",
    "interactive_install_prompt",
]
