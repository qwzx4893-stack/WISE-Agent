"""Complete source bundle with immutable provenance; never include runtime state.

This is a local packaging step, not a standalone EXE/installer or publication.
The old pre-Plugins publication remains sealed and is never modified here.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import zipfile

from prepare_public_snapshot import EXCLUDED, SAMPLE_EXCLUSIONS, SECRETS, SUFFIXES
from source_stamp import ROOT, source_stamp
from verify_desktop_source import (MANIFEST, SCHEMA, SCOPE, MAX_FILES, MAX_FILE_BYTES,
                                   MAX_TOTAL_BYTES, content_digest, linked, safe_path, verify)

DIRECTORIES = ("api", "core", "modes", "policies", "skills", "tools/packs", "ui/wise_web", "qa/acceptance", "tests", "docs")
STATIC_FILES = ("README.md", "CHANGELOG.md", "LICENSE", "NOTICE", "COPYING", "pytest.ini", "run.sh",
                "config/server.json.example", "config/skills_config.json", "config/intelligence_resources.json",
                "sandbox/build_rootfs.sh", "sandbox/known_missing.json", "tools/bootstrap_voice.ps1",
                "tools/voice_runtime_requirements.txt", "qa/tooling/python-requirements.txt",
                "qa/tooling/package.json", "qa/tooling/package-lock.json", "qa/tooling/TOOLCHAIN.md")
REQUIRED = ("agent_core.py", "wise_desktop.py", "Start-WISE.ps1", "requirements.txt", "api/server.py", "ui/wise_web/index.html",
            "requirements-intelligence.txt", "requirements-optimization.txt", "requirements-semantic-search.txt",
            "core/paths.py", "core/models/local_model_manager.py", "policies/policies.rego",
            "config/server.json.example", "config/skills_config.json", "config/intelligence_resources.json",
            "sandbox/build_rootfs.sh", "sandbox/known_missing.json", "tools/bootstrap_voice.ps1",
            "tools/voice_runtime_requirements.txt", "README.md", "docs/VOICE_RUNTIME.md", "docs/PLUGINS_NANGO.md",
            "docs/OPTIONAL_SECURITY_TOOLS.md", "qa/acceptance/DESKTOP_SOURCE_BUNDLE.md",
            "qa/tooling/python-requirements.txt", "qa/tooling/package.json", "qa/tooling/TOOLCHAIN.md")
# The generic upstream "models" exclusion incorrectly pruned core/models code.
BLOCKED_PARTS = (EXCLUDED - {"models"}) | {".voice-venv", ".pytest_cache", ".hypothesis", ".schemathesis", "site-packages",
    "recordings", "weights", "backups", "checkpoints", "training", "userstate", "downloads", "qa-results", "results", "artifacts", "dist", "build",
    "vendorvenvs", "vendor_venvs", "venvs", "virtualenv"}
EXTRA_SUFFIXES = {".rego", ".mjs", ".cjs", ".xml", ".sql", ".r", ".template", ".jinja", ".jinja2", ".j2", ".cfg", ".ini", ".jpeg", ".webp", ".gif", ".woff", ".otf"}
STATIC_SKILL_METADATA = {"skills/.bundles.json", "skills/.catalog-sources.json"}
RUNTIME_SUFFIXES = {".exe", ".dll", ".pyd", ".so", ".dylib", ".pth", ".pt", ".bin", ".onnx", ".safetensors", ".ckpt",
                    ".wav", ".mp3", ".mp4", ".sqlite", ".sqlite3", ".db", ".pkl", ".pickle", ".key", ".pem"}


def allowed(name: str) -> bool:
    safe_path(name)
    path = Path(name)
    parts = name.split("/")
    if path.suffix.lower() in RUNTIME_SUFFIXES:
        return False
    if name in SAMPLE_EXCLUSIONS or any(p.lower() in BLOCKED_PARTS for p in parts):
        return False
    if any(p.lower() == "models" and not (i == 1 and parts[0] == "core") for i, p in enumerate(parts)):
        return False
    if any(p.startswith(".") for p in parts) and name not in STATIC_SKILL_METADATA:
        return False
    if name in STATIC_FILES or name in STATIC_SKILL_METADATA:
        return True
    if len(parts) == 1:
        return path.suffix == ".py" or (parts[0].startswith("requirements") and path.suffix == ".txt") or (parts[0].startswith("Start-WISE") and path.suffix == ".ps1") or any(label in path.name.upper() for label in ("LICENSE", "NOTICE", "COPYING"))
    return any(name.startswith(directory + "/") for directory in DIRECTORIES) and (
        path.suffix.lower() in SUFFIXES | EXTRA_SUFFIXES or any(label in path.name.upper() for label in ("LICENSE", "NOTICE", "COPYING")))


def collect(root: Path) -> list[Path]:
    root = root.resolve()
    restricted = list(restricted_paths(root))
    files = []
    for relative in DIRECTORIES:
        if linked(root / relative):
            continue
        for directory, children, names in os.walk(root / relative, followlinks=False):
            children[:] = [name for name in children if name.lower() not in BLOCKED_PARTS and not name.startswith(".") and not linked(Path(directory) / name)]
            for name in names:
                path = Path(directory) / name
                if linked(path) or any(p == path.parent or p in path.parents for p in restricted):
                    continue
                if not allowed(path.relative_to(root).as_posix()):
                    continue
                files.append(path)
    files.extend(root.glob("*.py"))
    files.extend(root.glob("requirements*.txt"))
    files.extend(root.glob("Start-WISE*.ps1"))
    for label in ("LICENSE", "NOTICE", "COPYING"):
        files.extend(root.glob("*" + label + "*"))
    files.extend(root / p for p in STATIC_FILES)
    files = sorted({p for p in files if p.is_file() and not linked(p) and allowed(p.relative_to(root).as_posix())
                    and not any(linked(parent) for parent in p.parents if parent != root and root in parent.parents)})
    oversized = [p.relative_to(root).as_posix() for p in files if p.stat().st_size > MAX_FILE_BYTES]
    if oversized:
        raise ValueError("Source assets exceed per-file bound: " + ", ".join(oversized))
    if len(files) > MAX_FILES or sum(p.stat().st_size for p in files) > MAX_TOTAL_BYTES:
        raise ValueError("Source bundle exceeds bounded verification limits")
    return files


def read_source(path: Path, root: Path) -> bytes:
    """Keep reads bounded too, and recheck source link exclusions after selection."""
    if not path.resolve().is_relative_to(root) or linked(path) or any(linked(parent) for parent in path.parents if parent != root and root in parent.parents):
        raise RuntimeError("Source link changed during packaging")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("Source asset exceeds per-file bound: " + path.relative_to(root).as_posix())
    return data


def package(root: Path, output: Path) -> dict:
    root = root.resolve()
    if output.exists() or linked(output):
        raise FileExistsError("Refusing to overwrite a sealed source bundle")
    files = collect(root)
    relative = {p.relative_to(root).as_posix() for p in files}
    missing = set(REQUIRED) - relative
    if missing:
        raise ValueError("Incomplete desktop bundle: " + ", ".join(sorted(missing)))
    records = []
    for path in files:
        data = read_source(path, root)
        if SECRETS.search(data.decode("utf-8", errors="ignore")):
            raise ValueError("Sensitive literal review required in: " + path.relative_to(root).as_posix())
        records.append({"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    records.sort(key=lambda r: r["path"])
    stamp = source_stamp() if root == ROOT else None
    warnings = ["LOCAL SOURCE ONLY: Python, dependencies, native host and optional runtimes must be installed separately.",
                "Voice references, approved checkpoint, IndexTTS vendor/runtime and weights are excluded; bootstrap_voice.ps1 alone cannot provision voice.",
                "Native QA FlaUI/UIAutomation assemblies and QA environments are external and excluded; QA source is not a bundled observation runtime.",
                "Targeted secret patterns passed; this is not proof of absence of all secrets.",
                "Redistribution rights and completeness of upstream licensing have not been established."]
    if not any((root / n).is_file() for n in ("LICENSE", "COPYING")):
        warnings.append("No project LICENSE/COPYING exists; README license assertion is unverified and no licensing grant is created.")
    manifest = {"schema": SCHEMA, "source_stamp": stamp, "files": records, "content_sha256": content_digest(records),
                "scope": SCOPE, "secret_scan": "targeted-patterns-pass-not-proof", "redistribution_ready": False, "warnings": warnings,
                "runtime_state_included": False, "redistribution_exclusions": sorted(p.relative_to(root).as_posix() for p in restricted_paths(root))}
    output.parent.mkdir(parents=True, exist_ok=True)
    owned = False
    try:
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            owned = True
            for record in records:
                data = read_source(root / record["path"], root)
                if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                    raise RuntimeError("Source changed during packaging")
                archive.writestr(record["path"], data)
            if stamp is not None and source_stamp() != stamp:
                raise RuntimeError("Source changed during packaging")
            archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        return verify(output, required=REQUIRED, allow=allowed)
    except Exception:
        if owned:
            output.unlink(missing_ok=True)  # Only this newly-created, owned bundle.
        raise


def restricted_paths(root: Path):
    if linked(root / "skills"):
        return
    for directory, children, names in os.walk(root / "skills", followlinks=False):
        children[:] = [n for n in children if n.lower() not in BLOCKED_PARTS and not linked(Path(directory) / n)]
        for name in names:
            path = Path(directory) / name
            if "LICENSE" in name.upper() and not linked(path) and path.stat().st_size <= MAX_FILE_BYTES:
                if "Distribute, sublicense, or transfer" in path.read_text(encoding="utf-8", errors="ignore"):
                    yield path.parent


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(ROOT, args.output), ensure_ascii=False))
