"""Freeze a source-only pre-Plugins publication without touching the user's Git.

This creates a new owned output once. No history, credentials, runtime files,
voice recordings, weights or training data are copied. Publishing is separate.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".tooling/publication-wise-agent-pre-plugins"
DIRS = ("api", "core", "modes", "policies", "tests", "skills", "ui/wise_web", "tools/packs", "sandbox", "qa/acceptance")
EXCLUDED = {".git", ".archive", ".venv", "venv", ".tooling", "node_modules", "__pycache__", "uploaded", "rootfs", "logs", "memory", "sessions", "workspace", "checkpoints", "models", "training"}
# Reviewed upstream dummy credentials / private-key headers, not WISE runtime.
SAMPLE_EXCLUSIONS = {
    "skills/general/evolver/1.0.0/test/sanitize.test.js",
    "skills/general/k8s-manifest-generator/1.0.0/resources/implementation-playbook.md",
    "skills/security/1.0.0/devsecops/secrets-gitleaks/references/detection_rules.md",
    "skills/security/1.0.0/devsecops/secrets-gitleaks/references/false_positives.md",
}
SECRETS = re.compile(r"sk-or-v1-[a-zA-Z0-9]{40,}|ghp_[a-zA-Z0-9]{30,}|github_pat_[a-zA-Z0-9_]{50,}|AIza[a-zA-Z0-9_-]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".sh", ".ps1", ".csv", ".svg", ".woff2", ".ttf", ".png", ".jpg"}


def main():
    if OUT.exists(): raise ValueError("Snapshot already exists; it is sealed and will not be overwritten")
    restricted = []
    for license_path in (ROOT / "skills").rglob("*LICENSE*"):
        if license_path.is_file() and "Distribute, sublicense, or transfer" in license_path.read_text(encoding="utf-8", errors="ignore"):
            restricted.append(license_path.parent)
    candidates = []
    for relative in DIRS:
        for directory, folders, names in os.walk(ROOT / relative, followlinks=False):
            folders[:] = [name for name in folders if name.lower() not in EXCLUDED and not (Path(directory)/name).is_symlink()]
            for name in names:
                path=Path(directory)/name
                if path.relative_to(ROOT).as_posix() in SAMPLE_EXCLUSIONS: continue
                if any(parent == path.parent or parent in path.parents for parent in restricted): continue
                license_file = any(label in name.upper() for label in ("LICENSE", "COPYING", "NOTICE"))
                if path.is_symlink() or (path.suffix.lower() not in SUFFIXES and not license_file) or name.startswith(".env"): continue
                if path.stat().st_size > 4_000_000: continue
                candidates.append(path)
    candidates += list(ROOT.glob("*.py")) + list(ROOT.glob("requirements*.txt")) + list(ROOT.glob("Start-WISE*.ps1"))
    candidates += [ROOT/".gitignore", ROOT/".gitattributes", ROOT/"config/intelligence_resources.json", ROOT/"config/server.json.example", ROOT/"config/skills_config.json"]
    failures=[]
    candidates = [path for path in candidates if path.is_file() and not path.is_symlink()]
    for path in set(candidates):
        if SECRETS.search(path.read_bytes().decode("utf-8",errors="ignore")): failures.append(path.relative_to(ROOT).as_posix())
    if failures: raise RuntimeError("Potential sensitive literals require review before publication: " + ", ".join(sorted(failures)))
    OUT.mkdir(parents=True)
    files=[]
    for path in sorted(set(candidates)):
        target=OUT/path.relative_to(ROOT); target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(path,target)
    shutil.copy2(ROOT/"docs/releases/README_PRE_PLUGINS.md",OUT/"README.md")
    docs=OUT/"docs";docs.mkdir(exist_ok=True)
    for name in ("OPTIONAL_SECURITY_TOOLS.md", "WISE_FINAL_VALIDATION_PLAN.md"):
        shutil.copy2(ROOT/"docs"/name,docs/name)
    for path in sorted(OUT.rglob("*")):
        if path.is_file():
            files.append({"path":path.relative_to(OUT).as_posix(),"sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
    # Intentionally omit inherited CI rather than publish an unverified badge.
    manifest={"schema":"wise.pre-plugins-snapshot.v1","scope":"Source-only; subsequent Nango Plugins changes excluded",
              "files":files,"sensitive_literal_scan":"PASS; targeted check, not proof of all secret absence",
              "excluded":"Runtime data, .env, caches, histories, voice weights/recordings and training data",
              "redistribution_exclusions":[path.relative_to(ROOT).as_posix() for path in restricted]}
    manifest["reviewed_upstream_example_exclusions"] = sorted(SAMPLE_EXCLUSIONS)
    (OUT/"PUBLICATION_MANIFEST.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    print(json.dumps({"snapshot":str(OUT),"files":len(files),"bytes":sum(path.stat().st_size for path in OUT.rglob("*") if path.is_file())}))


if __name__=="__main__":main()
