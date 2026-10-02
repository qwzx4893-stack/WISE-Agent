"""
Dynamic Tool Acquisition — fetch, analyse and register arbitrary tools.

Three pieces:

* :class:`ToolFetcher` — download a tool from a URL, package manager,
  or git repo. Supported package archives:
  ``.exe``, ``.msi``, ``.app``, ``.AppImage``, ``.deb``,
  ``.rpm``, ``.tar.gz``, ``.zip``, raw binaries.
* :class:`ToolAnalyzer` — best-effort metadata extraction. Inspects
  ``--help`` / ``man`` output, looks for a README, and (optionally)
  delegates to an LLM callable for richer documentation. Pure-stdlib
  heuristics keep tests fast.
* :class:`ManifestGenerator` — produces a standard
  ``manifest.json`` with ``name``, ``description``, ``capabilities``,
  ``use_cases``, ``dependencies``, ``cli_command`` and
  ``exec_strategy``.

The pipeline is intentionally separable so a caller can plug in a real
LLM for the analysis stage in production while CI tests rely only on
the heuristic path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from .observability import Tracer
from .universal_executor import SUPPORTED_STRATEGIES

#: Maximum download size in bytes (200 MB by default). Override via
#: ``AGENT_OS_FETCH_MAX_BYTES`` env var.
DEFAULT_MAX_FETCH_BYTES = 200 * 1024 * 1024

#: Mapping from binary extension to a heuristic ``exec_strategy``.
_EXT_TO_STRATEGY = {
    ".exe": "windows-gui",
    ".msi": "windows-gui",
    ".app": "mac-gui",
    ".dmg": "mac-gui",
    ".appimage": "linux-gui",
    ".deb": "cli",
    ".rpm": "cli",
}


@dataclass
class FetchResult:
    ok: bool
    tool_name: str
    source: str
    local_path: Optional[str]
    archive_kind: str = ""
    bytes_downloaded: int = 0
    error: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    name: str
    description: str
    capabilities: List[str] = field(default_factory=list)
    use_cases: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    cli_command: str = ""
    exec_strategy: str = "cli"
    notes: List[str] = field(default_factory=list)
    help_output: str = ""
    confidence: float = 0.5


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------
class ToolFetcher:
    """Download a tool. Supports URLs, package managers and git repos."""

    def __init__(self,
                 *,
                 cache_dir: Optional[Path] = None,
                 max_bytes: Optional[int] = None) -> None:
        self.cache_dir = Path(cache_dir or
                              Path(tempfile.gettempdir()) / "agent_os_fetch_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes or int(
            os.environ.get("AGENT_OS_FETCH_MAX_BYTES", DEFAULT_MAX_FETCH_BYTES)
        )

    # ------------------------------------------------------------------ API
    def fetch(self, source: str, *, name: Optional[str] = None) -> FetchResult:
        Tracer.emit("dynamic_tool.fetch.start", source=source[:200])
        if source.startswith(("http://", "https://", "file://")):
            return self._fetch_url(source, name=name)
        if source.startswith(("git+", "git://")) or source.endswith(".git"):
            return self._fetch_git(source, name=name)
        if source.startswith(("pip:", "apt:", "npm:")):
            return self._fetch_package(source, name=name)
        return FetchResult(False, name or "", source, None,
                           error=f"unsupported source: {source!r}")

    # ------------------------------------------------------------- URL fetch
    def _fetch_url(self, url: str, *, name: Optional[str]) -> FetchResult:
        parsed = urlparse(url)
        derived = name or Path(parsed.path).name or "downloaded_tool"
        target = self.cache_dir / derived
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Agent-OS-ToolFetcher/1.0",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
                read = 0
                with open(target, "wb") as out:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        read += len(chunk)
                        if read > self.max_bytes:
                            target.unlink(missing_ok=True)
                            return FetchResult(False, derived, url, None,
                                               error=f"download exceeded "
                                                     f"{self.max_bytes} bytes")
                        out.write(chunk)
            kind = _classify_archive(target)
            return FetchResult(True, derived, url, str(target),
                               archive_kind=kind, bytes_downloaded=read)
        except Exception as exc:
            return FetchResult(False, derived, url, None, error=str(exc))

    # ------------------------------------------------------------- git fetch
    def _fetch_git(self, source: str, *, name: Optional[str]) -> FetchResult:
        url = source[4:] if source.startswith("git+") else source
        derived = name or _slug_from_git(url)
        target = self.cache_dir / derived
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        try:
            subprocess.run(["git", "clone", "--depth", "1", url, str(target)],
                           check=True, capture_output=True, text=True, timeout=120)
            return FetchResult(True, derived, source, str(target),
                               archive_kind="git-repo")
        except Exception as exc:
            return FetchResult(False, derived, source, None, error=str(exc))

    # ---------------------------------------------------------- package fetch
    def _fetch_package(self, source: str, *,
                       name: Optional[str]) -> FetchResult:
        kind, _, package = source.partition(":")
        if not package:
            return FetchResult(False, name or "", source, None,
                               error="package source must be `pip:NAME` or `apt:NAME`")
        return FetchResult(True, name or package, source, None,
                           archive_kind=f"{kind}-package",
                           extras={"package_kind": kind, "package": package})


def _classify_archive(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".tar", ".gz", ".tgz", ".xz", ".bz2"):
        return "tar"
    if suffix == ".zip":
        return "zip"
    if suffix in _EXT_TO_STRATEGY:
        return suffix.lstrip(".")
    return "binary"


def _slug_from_git(url: str) -> str:
    last = url.rstrip("/").split("/")[-1]
    return last.removesuffix(".git") or "git_repo"


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------
class ToolAnalyzer:
    """Best-effort introspection of a freshly fetched tool.

    The pipeline is:

    1. If a binary path is provided, run ``<bin> --help`` (and ``-h``)
       and capture the output.
    2. Look for a README / man page next to the binary.
    3. (Optional) hand the captured text to ``llm_callable`` for richer
       structured extraction.
    4. Fall back to keyword heuristics on the help text.
    """

    def __init__(self,
                 llm_callable: Optional[Callable[[str], Dict[str, Any]]] = None,
                 *, help_timeout: float = 5.0) -> None:
        self.llm = llm_callable
        self.help_timeout = help_timeout

    # ------------------------------------------------------------------ API
    def analyze(self,
                *,
                name: str,
                binary_path: Optional[str] = None,
                source: str = "",
                kind: str = "") -> AnalysisResult:
        help_text = ""
        notes: List[str] = []
        if binary_path:
            help_text, _err = self._run_help(binary_path)
            if not help_text:
                notes.append("no help output captured")
            readme = self._find_readme(Path(binary_path).parent)
            if readme:
                try:
                    help_text += "\n\n" + Path(readme).read_text(encoding="utf-8",
                                                                 errors="replace")[:8000]
                    notes.append(f"included readme: {readme}")
                except Exception:
                    pass

        # Heuristic + LLM enrichment.
        capabilities, use_cases = _heuristic_capabilities(help_text, name=name)
        description = _heuristic_description(help_text, name=name)
        strategy = _heuristic_strategy(name=name, kind=kind, source=source,
                                       help_text=help_text)
        cli_command = name if strategy == "cli" else ""
        deps: List[str] = _heuristic_dependencies(help_text)

        if self.llm is not None and help_text:
            try:
                enriched = self.llm(help_text) or {}
            except Exception as exc:
                Tracer.emit("dynamic_tool.analyzer.llm_error", error=str(exc))
                enriched = {}
            description = enriched.get("description") or description
            capabilities = enriched.get("capabilities") or capabilities
            use_cases = enriched.get("use_cases") or use_cases
            deps = enriched.get("dependencies") or deps
            if enriched.get("exec_strategy") in SUPPORTED_STRATEGIES:
                strategy = enriched["exec_strategy"]
            if enriched.get("cli_command"):
                cli_command = enriched["cli_command"]

        confidence = 0.4
        if help_text:
            confidence += 0.3
        if self.llm is not None:
            confidence += 0.2
        if capabilities and use_cases:
            confidence += 0.1

        return AnalysisResult(
            name=name,
            description=description,
            capabilities=capabilities[:8] or [f"run {name}"],
            use_cases=use_cases[:5] or [f"use {name} from the command line"],
            dependencies=deps,
            cli_command=cli_command,
            exec_strategy=strategy,
            notes=notes,
            help_output=help_text[:4000],
            confidence=round(min(confidence, 1.0), 2),
        )

    # ----------------------------------------------------------- Help capture
    def _run_help(self, binary_path: str) -> tuple[str, str]:
        out = ""
        err = ""
        for flag in ("--help", "-h"):
            try:
                proc = subprocess.run(
                    [binary_path, flag],
                    capture_output=True, text=True,
                    timeout=self.help_timeout,
                )
                out = proc.stdout or ""
                err = proc.stderr or ""
                if out or err:
                    return out + ("\n" + err if err else ""), ""
            except Exception as exc:
                err = str(exc)
        return out, err

    @staticmethod
    def _find_readme(folder: Path) -> Optional[str]:
        for candidate in ("README.md", "README", "README.txt", "readme.md"):
            f = folder / candidate
            if f.exists():
                return str(f)
        return None


# ---------------------------------------------------------------------------
# Manifest generator
# ---------------------------------------------------------------------------
class ManifestGenerator:
    """Produce the manifest dict that Agent OS' tool registry consumes."""

    def generate(self,
                 analysis: AnalysisResult,
                 *,
                 source: str,
                 fetch: Optional[FetchResult] = None,
                 category: Optional[str] = None) -> Dict[str, Any]:
        manifest = {
            "name": analysis.name,
            "description": analysis.description,
            "capabilities": list(analysis.capabilities),
            "use_cases": list(analysis.use_cases),
            "dependencies": list(analysis.dependencies),
            "category": category or _heuristic_category(analysis),
            "cli_command": analysis.cli_command,
            "exec_strategy": analysis.exec_strategy,
            "source": source,
            "auto_generated": True,
            "confidence": analysis.confidence,
        }
        if fetch and fetch.local_path:
            manifest["local_path"] = fetch.local_path
        if fetch and fetch.archive_kind:
            manifest["archive_kind"] = fetch.archive_kind
        if analysis.help_output:
            manifest["help_excerpt"] = analysis.help_output[:1200]
        if analysis.notes:
            manifest["analyzer_notes"] = list(analysis.notes)
        return manifest

    @staticmethod
    def write(manifest: Dict[str, Any], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                        encoding="utf-8")


# ---------------------------------------------------------------------------
# Heuristics — small enough to read top-to-bottom.
# ---------------------------------------------------------------------------
_VERB_NOUN_RE = re.compile(
    r"\b(scan|build|render|deploy|monitor|test|fetch|download|upload|"
    r"compress|decompress|encrypt|decrypt|sign|verify|format|lint|trace|"
    r"convert|generate|search|analyze|read|write|edit|run|inspect|extract|"
    r"detect|compute|publish|migrate|rotate|tag)(?:s|es|ed|ing)?\b\s+"
    r"([a-z][a-z\-]+)",
    re.IGNORECASE,
)


def _heuristic_capabilities(text: str, *, name: str) -> tuple[List[str], List[str]]:
    if not text:
        return [], []
    found: List[str] = []
    for m in _VERB_NOUN_RE.finditer(text or ""):
        verb = m.group(1).lower()
        cap = f"{verb} {m.group(2).lower()}"
        if cap not in found:
            found.append(cap)
    use_cases: List[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith(("example", "usage:")):
            use_cases.append(line[:120])
        if line.startswith("$ ") or line.startswith("# "):
            use_cases.append(line[:120])
        if len(use_cases) >= 5:
            break
    return found[:8], use_cases[:5]


def _heuristic_description(text: str, *, name: str) -> str:
    if not text:
        return f"{name} tool (auto-generated; please review)"
    # Pick the first non-empty, non-usage line.
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith(("usage:", "example", "options:")):
            continue
        if line.startswith(("-", "$", "#")):
            continue
        return line[:200]
    return f"{name} (auto-generated)"


def _heuristic_dependencies(text: str) -> List[str]:
    if not text:
        return []
    deps = set()
    for token in ("python3", "node", "java", "ruby", "perl", "go", "git",
                  "curl", "wget", "docker", "wine", "xvfb"):
        if re.search(rf"\b{token}\b", text, re.IGNORECASE):
            deps.add(token)
    return sorted(deps)


def _heuristic_strategy(*, name: str, kind: str, source: str,
                        help_text: str) -> str:
    suffix = ""
    if source:
        suffix = Path(urlparse(source).path).suffix.lower()
    if suffix in _EXT_TO_STRATEGY:
        return _EXT_TO_STRATEGY[suffix]
    if kind in ("exe", "msi"):
        return "windows-gui"
    if kind == "appimage":
        return "linux-gui"
    if kind in ("dmg", "app"):
        return "mac-gui"
    low = (help_text or "").lower()
    if "playwright" in low or "browser" in low and "url" in low:
        return "web-app"
    if "X11" in (help_text or "") or "DISPLAY" in (help_text or ""):
        return "linux-gui"
    return "cli"


def _heuristic_category(a: AnalysisResult) -> str:
    blob = " ".join([a.description, " ".join(a.capabilities),
                     " ".join(a.use_cases)]).lower()
    table = [
        (("scan", "vulnerability", "security", "exploit", "audit"), "security"),
        (("docker", "kubernetes", "kubectl", "terraform", "deploy"), "devops"),
        (("test", "playwright", "selenium"), "testing"),
        (("render", "blender", "model", "image"), "media"),
        (("database", "sql", "postgres"), "database"),
        (("seo", "keyword"), "seo"),
        (("frontend", "react", "vue", "tailwind"), "frontend"),
        (("backend", "fastapi", "django"), "backend"),
        (("agent", "llm", "openai"), "agents"),
    ]
    for keywords, category in table:
        if any(k in blob for k in keywords):
            return category
    return "general"


# ---------------------------------------------------------------------------
# Convenience pipeline
# ---------------------------------------------------------------------------
def acquire_tool(source: str, *,
                 name: Optional[str] = None,
                 llm_callable: Optional[Callable[[str], Dict[str, Any]]] = None,
                 manifest_dir: Optional[Path] = None,
                 ) -> Dict[str, Any]:
    """Fetch + analyse + register a tool in one call.

    Returns the generated manifest. Side effect: when ``manifest_dir``
    is set, writes ``<manifest_dir>/<tool>.json``.
    """
    fetcher = ToolFetcher()
    fetch_result = fetcher.fetch(source, name=name)
    analyser = ToolAnalyzer(llm_callable=llm_callable)
    analysis = analyser.analyze(
        name=fetch_result.tool_name or (name or "unknown"),
        binary_path=fetch_result.local_path,
        source=source,
        kind=fetch_result.archive_kind,
    )
    manifest = ManifestGenerator().generate(
        analysis, source=source, fetch=fetch_result,
    )
    manifest["fetch"] = {
        "ok": fetch_result.ok,
        "bytes": fetch_result.bytes_downloaded,
        "kind": fetch_result.archive_kind,
        "error": fetch_result.error,
    }
    if manifest_dir is not None:
        path = Path(manifest_dir) / f"{manifest['name']}.json"
        ManifestGenerator.write(manifest, path)
        manifest["manifest_path"] = str(path)
    Tracer.emit("dynamic_tool.acquire.done",
                tool=manifest["name"], strategy=manifest["exec_strategy"],
                ok=fetch_result.ok)
    return manifest


__all__ = [
    "ToolFetcher", "ToolAnalyzer", "ManifestGenerator",
    "FetchResult", "AnalysisResult",
    "acquire_tool",
]
