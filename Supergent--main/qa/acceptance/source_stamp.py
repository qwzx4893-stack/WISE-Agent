"""A content stamp prevents using acceptance evidence from a different build."""
import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def source_stamp():
    digest = hashlib.sha256()
    files = []
    for relative in ("api","core","modes","policies","tools/packs","ui/wise_web"):
        # Fingerprint application source, not installed packages under MCP venvs.
        # Prune traversal itself: filtering after rglob still scans every package.
        for directory, children, names in os.walk(ROOT/relative):
            children[:] = [name for name in children if name not in
                           {"__pycache__", ".venv", "venv", "node_modules", ".git", "site-packages"}]
            files.extend(Path(directory)/name for name in names if
                         Path(name).suffix in {".py",".js",".css",".html",".svg",".json",".yaml",".yml",".md",".png",".woff2",".ttf",".rego"})
    # Root entry points include the conversation orchestrator and native host.
    # Omitting them could accept evidence from a different execution path.
    files.extend(ROOT.glob("*.py"))
    files.extend(ROOT.glob("Start-WISE*.ps1"))
    files += [ROOT/"config/intelligence_resources.json",ROOT/"requirements.txt",ROOT/"requirements-intelligence.txt"]
    for path in sorted(files):
        digest.update(path.relative_to(ROOT).as_posix().encode()); digest.update(path.read_bytes())
    return digest.hexdigest()
