"""A content stamp prevents using acceptance evidence from a different build."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def source_stamp():
    digest = hashlib.sha256()
    files = []
    for relative in ("api","core","modes","ui/wise_web"):
        files.extend(path for path in (ROOT/relative).rglob("*") if path.is_file() and
            "__pycache__" not in path.parts and path.suffix in {".py",".js",".css",".html",".svg"})
    # Root entry points include the conversation orchestrator and native host.
    # Omitting them could accept evidence from a different execution path.
    files.extend(ROOT.glob("*.py"))
    files.extend(ROOT.glob("Start-WISE*.ps1"))
    files += [ROOT/"config/intelligence_resources.json",ROOT/"requirements.txt",ROOT/"requirements-intelligence.txt"]
    for path in sorted(files):
        digest.update(path.relative_to(ROOT).as_posix().encode()); digest.update(path.read_bytes())
    return digest.hexdigest()
