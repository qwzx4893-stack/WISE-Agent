"""Build a reviewed source payload, without touching the development Git index.

No credentials accepted here. Copies selected source mechanically; skips local
state and restricted assets. Publishing/authentication is a separate operation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Supergent--main"
sys.path.insert(0, str(APP / "qa/acceptance"))
from package_desktop_source import collect, restricted_paths  # noqa: E402
from prepare_public_snapshot import SECRETS, SUFFIXES  # noqa: E402
from verify_desktop_source import linked, safe_path  # noqa: E402
from source_stamp import source_stamp  # noqa: E402

REPO = "qwzx4893-stack/WISE-Agent"
BLOCKED = {".git", ".venv", ".voice-venv", ".tooling", "node_modules", "__pycache__", "dist", "build", "logs", "memory", "sessions", "workspace", "checkpoints", "models", "recordings", "training", "downloads", "backups", "profiles", ".leon", "site-packages", ".pytest_cache", ".hypothesis", ".cache", "bin"}
SOURCE_SUFFIXES = SUFFIXES | {".lock", ".mjs", ".cjs", ".xml", ".xsd", ".sql", ".rego", ".ini", ".cfg", ".template", ".jinja2", ".j2", ".webp", ".jpeg", ".gif", ".woff", ".otf", ".r", ".bat", ".cmd"}
ROOT_FILES = ("README.md", "CONTRIBUTING.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md", "package.json", "pnpm-lock.yaml", ".env.example", ".gitignore", ".gitattributes", "wise_supervisor.py", "wise_desktop.py", "config/wise_config.py", "tools/Install-WISE.ps1", "tools/prepare_public_update.py", "tools/publish_public_update.py", "tools/verify_public_source.py")
APP_DOCS = {"README.md", "CHANGELOG.md", "docs/VOICE_RUNTIME.md", "docs/OPTIONAL_SECURITY_TOOLS.md", "docs/PLUGINS_NANGO.md", "docs/WISE_FINAL_VALIDATION_PLAN.md", "docs/RELEASE_NOTES.md", "docs/releases/LICENSING_STATUS.md"}
REQUIRED = {"README.md", "tools/Install-WISE.ps1", "docs/INSTALLATION.md", "docs/VALIDATION.md", "leon-develop/LICENSE.md", "leon-develop/package.json", "leon-develop/pnpm-lock.yaml", "wise_supervisor.py", "Supergent--main/agent_core.py", "Supergent--main/api/server.py", "Supergent--main/api/plugin_routes.py", "Supergent--main/core/capability_network.py", "Supergent--main/core/usage_network.py", "Supergent--main/core/brain/capability_team.py", "Supergent--main/core/models/model_manager.py", "Supergent--main/core/integrations/service.py", "Supergent--main/ui/wise_web/index.html", "Supergent--main/ui/wise_web/plugins.js", "Supergent--main/ui/wise_web/messaging.js", "Supergent--main/requirements.txt", "Supergent--main/config/server.json.example", "Supergent--main/config/intelligence_resources.json", "Supergent--main/tools/bootstrap_voice.ps1", "Supergent--main/tools/voice_runtime_requirements.txt", "Supergent--main/qa/tooling/package-lock.json", "Supergent--main/pytest.ini"}


def walk_source(directory: Path):
    for parent, children, names in os.walk(directory, followlinks=False):
        children[:] = [n for n in children if n.lower() not in BLOCKED and not linked(Path(parent) / n)]
        for name in names:
            path = Path(parent) / name
            if linked(path) or path.suffix.lower() not in SOURCE_SUFFIXES and name not in {"Pipfile", ".nvmrc", ".gitignore", ".editorconfig", ".gitkeep"} and not any(n in name.upper() for n in ("LICENSE", "NOTICE", "COPYING")):
                continue
            if name.startswith(".env") and name not in {".env.sample", ".env.example"}:
                continue
            yield path


def prepare(output: Path):
    output = output.resolve()
    if not output.is_relative_to(APP / ".tooling") or output.exists():
        raise ValueError("Use a NEW payload directory inside the app's .tooling only")
    candidates = {p for p in collect(APP) if not p.relative_to(APP).as_posix().startswith("docs/") or p.relative_to(APP).as_posix() in APP_DOCS}
    candidates.update(APP / n for n in (".env.example", ".gitattributes", ".gitignore"))
    candidates.update(walk_source(APP / "tools/custom"))
    # Installed entry-point shims/binaries and the external voice checkout are
    # deliberately not a substitute for dependency manifests.
    candidates.update(ROOT / n for n in ROOT_FILES)
    candidates.update(walk_source(ROOT / "docs"))
    candidates.update(walk_source(ROOT / ".github"))
    candidates.update(walk_source(ROOT / "tests"))
    # Leon's source is already tracked in the workspace; do not absorb
    # untracked owner config/profile/context artifacts from the running copy.
    leon_tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", "leon-develop"], check=True, capture_output=True).stdout.decode().split("\0")
    permitted_leon = set(walk_source(ROOT / "leon-develop"))
    candidates.update(ROOT / name for name in leon_tracked if name and ROOT / name in permitted_leon)
    records, failures, omitted = [], [], []
    selected = []
    before = source_stamp()
    for path in sorted(candidates):
        if not path.is_file() or linked(path):
            continue
        relative = path.relative_to(ROOT).as_posix()
        safe_path(relative)
        if not path.resolve().is_relative_to(ROOT) or any(linked(parent) for parent in path.parents if parent != ROOT and ROOT in parent.parents):
            raise ValueError("Linked source cannot be published: " + relative)
        if path.stat().st_size > 16_000_000:
            raise ValueError("Oversized source requires review: " + relative)
        data = path.read_bytes()
        if SECRETS.search(data.decode("utf-8", errors="ignore")):
            failures.append(relative)
            continue
        selected.append((relative, data))
    if failures:
        raise ValueError("Potential credential literals need review (contents not printed): " + ", ".join(failures))
    names = {n for n, _ in selected}
    if REQUIRED - names:
        raise ValueError("Missing essential source: " + ", ".join(sorted(REQUIRED - names)))
    if before != source_stamp():
        raise RuntimeError("Runtime source changed during preparation")
    output.mkdir(parents=True)
    for relative, data in selected:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Mechanical source copying, not programmatic rewriting of source.
        shutil.copy2(ROOT / relative, target)
        if target.read_bytes() != data:
            raise RuntimeError("Source changed during copy: " + relative)
        records.append({"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    omitted = sorted("Supergent--main/" + p.relative_to(APP).as_posix() for p in restricted_paths(APP))
    manifest = {"schema": "wise.public-workspace.v1", "repository": REPO, "source_stamp": before,
        "scope": "CURRENT_WORKSPACE_SOURCE_NOT_STANDALONE_INSTALLER", "files": records,
        "required": sorted(REQUIRED), "restricted_skill_exclusions": omitted,
        "runtime_state_included": False, "voice_assets_included": False,
        "external_voice_revision": "ee40fa7d6c6b8a2c7f06105f9f1e65775b74868c",
        "licensing_review": "Known restricted trees excluded; no blanket license clearance or WISE-wide grant",
        "secret_scan": "targeted-patterns-pass-not-proof"}
    (output / "PUBLICATION_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"files": len(records), "bytes": sum(r["bytes"] for r in records), "required_files": "PASS", "source_stamp": before, "restricted_skill_trees": len(omitted)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    prepare(parser.parse_args().output)
