"""Owned security source inventory and bounded recovery copy (no runtime input).

Never traverses runtime state, models, profiles, installed environments or links.
This records an application *source* snapshot, not a disk/credential backup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import sys
import zipfile
from datetime import datetime, timezone

APP = Path(__file__).resolve().parents[2]
WORKSPACE = APP.parent
SOURCE_DIRS = ("api", "core", "modes", "policies", "tools", "ui/wise_web",
               "skills", "tests", "qa/acceptance", ".github", "sandbox")
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "site-packages",
             "__pycache__", ".pytest_cache", ".archive", "results", "logs",
             "downloads", "checkpoints", "outputs", "generated"}
SOURCE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".html", ".css", ".json",
                   ".yaml", ".yml", ".toml", ".txt", ".md", ".ps1", ".bat",
                   ".cmd", ".sh", ".rego", ".xml", ".svg", ".lock"}
SOURCE_SUFFIXES.update({".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rs", ".psm1", ".vue", ".svelte"})


def source_files():
    found = set()
    for base, relative in ([(APP, name) for name in SOURCE_DIRS]
                           + [(WORKSPACE, name) for name in ('tools', 'tests')]):
        for folder, children, names in os.walk(base / relative, followlinks=False):
            children[:] = sorted(name for name in children if name not in SKIP_DIRS
                                 and not (Path(folder) / name).is_symlink()
                                 and not getattr((Path(folder) / name), "is_junction", lambda: False)())
            for name in names:
                path = Path(folder) / name
                if path.suffix.lower() in SOURCE_SUFFIXES and not path.is_symlink():
                    found.add(path)
    for parent in (APP, WORKSPACE):
        for path in parent.iterdir():
            if path.is_file() and not path.is_symlink() and (
                path.suffix.lower() in {".py", ".ps1", ".bat", ".cmd"}
                or path.name in {"package.json", "pnpm-lock.yaml", "package-lock.json", "pyproject.toml"}
                or path.name.startswith("requirements") and path.suffix == ".txt"
            ):
                found.add(path)
    for path in (APP / "config").glob("*.example"):
        if path.is_file() and not path.is_symlink():
            found.add(path)
    for relative in ("config/intelligence_resources.json", "config/mcp_market_seed.json",
                     "qa/tooling/python-requirements.txt", "qa/tooling/package.json",
                     "qa/tooling/package-lock.json"):
        path = APP / relative
        if path.is_file() and not path.is_symlink():
            found.add(path)
    for path in (WORKSPACE / ".github/workflows").glob("*.yml"):
        if path.is_file() and not path.is_symlink():
            found.add(path)
    return sorted(found)


def inventory():
    rows = []
    stamp = hashlib.sha256()
    for path in source_files():
        data = path.read_bytes()
        relative = path.relative_to(WORKSPACE).as_posix()
        checksum = hashlib.sha256(data).hexdigest()
        rows.append({"path": relative, "sha256": checksum, "bytes": len(data)})
        stamp.update(relative.encode("utf-8")); stamp.update(bytes.fromhex(checksum))
    return {"schema": "wise.security-source.v1", "sha256": stamp.hexdigest(), "files": rows,
            "exclusions": sorted(SKIP_DIRS),
            "limitations": ["Source text only; executable/model/OS identity requires the separate SBOM/runtime gate",
                            "Runtime state, private configuration and credentials are deliberately not copied",
                            "Excluded outputs and caches are not executable-source certification"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backup", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(APP / "qa-results") or output.exists():
        parser.error("Use a new WISE-owned qa-results directory")
    output.mkdir(parents=True)
    record = inventory()
    record["created_at"] = datetime.now(timezone.utc).isoformat()
    record["environment"] = {"python": sys.version, "platform": platform.platform()}
    record["backup_status"] = "NOT_REQUESTED" if not args.backup else "INCOMPLETE"
    # Preserve the original inventory even if a concurrent edit invalidates
    # the recovery archive. Never label a partial zip a successful checkpoint.
    (output / "source.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    if args.backup:
        total = sum(row["bytes"] for row in record["files"])
        if total > 256 * 1024 * 1024:
            raise ValueError("Source backup exceeds 256 MiB; do not silently produce an incomplete backup")
        with zipfile.ZipFile(output / "source-before.zip", "x", zipfile.ZIP_DEFLATED) as archive:
            for row in record["files"]:
                path = WORKSPACE / row["path"]
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != row["sha256"]:
                    raise RuntimeError("Source changed during snapshot: " + row["path"])
                archive.writestr(row["path"], data)
        record["backup"] = "source-before.zip"
        record["backup_status"] = "COMPLETE"
    (output / "source.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps({"source_sha256": record["sha256"], "files": len(record["files"]),
                      "output": str(output), "backup": record.get("backup")}))


if __name__ == "__main__":
    main()
