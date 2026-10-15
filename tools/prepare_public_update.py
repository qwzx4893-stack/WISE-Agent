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
APP_DOCS = {"README.md", "CHANGELOG.md", "docs/VOICE_RUNTIME.md", "docs/OPTIONAL_SECURITY_TOOLS.md", "docs/PLUGINS.md", "docs/MCP_CONNECTION_MARKETPLACE.md", "docs/WISE_FINAL_VALIDATION_PLAN.md", "docs/RELEASE_NOTES.md", "docs/releases/LICENSING_STATUS.md"}
PRIVATE_QA_SUMMARIES = {'qa/acceptance/CURRENT_STATUS_20261002.md'}
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


_EXAMPLE_ASSIGNMENT = re.compile(
    rb"(?P<prefix>\b(?P<key>[A-Za-z_][A-Za-z0-9_-]*)[\"']?\s*[:=]\s*[\"']?)(?P<value>[A-Za-z0-9._+/=:-]{6,})")
REQUIRED.update({'Supergent--main/config/mcp_market_seed.json',
                 'Supergent--main/docs/PLUGINS.md',
                 'Supergent--main/docs/MCP_CONNECTION_MARKETPLACE.md',
                 'Supergent--main/tools/update_mcp_catalog.py',
                 'Supergent--main/core/plugins/manager.py',
                 'Supergent--main/core/mcp/oauth.py'})
# The former Nango broker was removed; require the actual direct-MCP path.
REQUIRED.discard('Supergent--main/core/integrations/service.py')


def redact_reviewed_examples(relative: str, data: bytes, review: dict) -> bytes:
    """Redact only pinned Markdown examples; never rewrite executable source.

    Review metadata is private and contains locations/hashes, not secret values.
    Unknown bytes/locations/shapes block export instead of guessing a rewrite.
    """
    entry = review.get(relative)
    if entry is None:
        return data
    if not relative.endswith('.md') or hashlib.sha256(data).hexdigest() != entry['sha256']:
        raise ValueError('Credential example changed; review required: ' + relative)
    lines = data.splitlines(keepends=True)
    spans = entry.get('spans', [(n, n) for n in entry['lines']])
    previous = 0
    for start, end in spans:
        if (type(start) is not int or type(end) is not int
                or not previous < start <= end <= len(lines) or end - start > 15):
            raise ValueError('Invalid credential review location')
        previous = end
        original = b''.join(lines[start - 1:end])
        matches = [m for m in _EXAMPLE_ASSIGNMENT.finditer(original)
                   if any(k in m.group('key').lower().replace(b'-', b'_') for k in (b'password', b'passwd', b'secret', b'token', b'api_key', b'apikey', b'basic_auth'))]
        if len(matches) != 1:
            raise ValueError('Credential example shape needs review: ' + relative)
        match = matches[0]
        replacement = original[:match.start('value')] + b'YOUR_OWN_SECRET' + original[match.end('value'):]
        if b'basic_auth' in match.group('key').lower() and b'#' in replacement:
            # Encoded auth examples may repeat plaintext credentials in a
            # comment. Preserve the assignment, not that explanatory secret.
            ending = b'\r\n' if replacement.endswith(b'\r\n') else b'\n' if replacement.endswith(b'\n') else b''
            replacement = replacement.partition(b'#')[0] + b'# Configure your own credentials.' + ending
        replaced_lines = replacement.splitlines(keepends=True)
        if len(replaced_lines) != end - start + 1:
            raise ValueError('Credential redaction changed line structure')
        lines[start - 1:end] = replaced_lines
    return b''.join(lines)


def prepare(output: Path, credential_review: Path):
    output = output.resolve()
    if not output.is_relative_to(APP / ".tooling") or output.exists():
        raise ValueError("Use a NEW payload directory inside the app's .tooling only")
    review_path = credential_review.resolve()
    if not review_path.is_relative_to(APP / 'qa-results') or linked(review_path):
        raise ValueError('Use a private, WISE-owned credential review record')
    record = json.loads(review_path.read_text(encoding='utf-8'))
    if record.get('schema') != 'wise.publication-secret-review.v1':
        raise ValueError('Missing credential-review schema')
    review = {r['path']: r for r in record['redactions']}
    if len(review) != len(record['redactions']):
        raise ValueError('Duplicate credential review path')
    for name in review:
        safe_path(name)
    candidates = {p for p in collect(APP) if not p.relative_to(APP).as_posix().startswith("docs/") or p.relative_to(APP).as_posix() in APP_DOCS}
    candidates = {p for p in candidates if p.relative_to(APP).as_posix() not in PRIVATE_QA_SUMMARIES}
    candidates.update(APP / n for n in (".env.example", ".gitattributes", ".gitignore"))
    candidates.update(walk_source(APP / "tools/custom"))
    candidates.add(APP / 'tools/update_mcp_catalog.py')
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
        data = redact_reviewed_examples(relative, data, review)
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
        target.write_bytes(data)  # Mechanical export; local source is unchanged.
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
        "credential_example_redactions": [{"path": name, "lines": review[name]['lines'],
                                            "spans": review[name].get('spans', [])}
                                           for name in sorted(review) if name in names],
        "private_qa_summary_exclusions": sorted(PRIVATE_QA_SUMMARIES),
        "secret_scan": "targeted-patterns-pass-not-proof"}
    (output / "PUBLICATION_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"files": len(records), "bytes": sum(r["bytes"] for r in records), "required_files": "PASS", "source_stamp": before, "restricted_skill_trees": len(omitted)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument('--credential-review', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.output, args.credential_review)
