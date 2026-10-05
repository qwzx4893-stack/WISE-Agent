"""Read-only public-tree checks; hashes authenticate neither authors nor rights."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess


def verify(root):
    root = root.resolve()
    manifest = json.loads((root / "PUBLICATION_MANIFEST.json").read_text(encoding="utf-8"))
    issues = []
    python_count = 0
    links = 0
    credential = re.compile(r"sk-or-v1-[a-zA-Z0-9]{40,}|ghp_[a-zA-Z0-9]{30,}|github_pat_[a-zA-Z0-9_]{50,}|AIza[a-zA-Z0-9_-]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
    for record in manifest["files"]:
        path = root / record["path"]
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            issues.append("unsafe_path:" + record["path"])
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            issues.append("hash:" + record["path"])
        if credential.search(data.decode("utf-8", errors="ignore")):
            issues.append("credential_pattern:" + record["path"])
        if path.suffix == ".py" and (record["path"].startswith("Supergent--main/core/") or record["path"].startswith("Supergent--main/api/") or record["path"].startswith("tools/")):
            try:
                ast.parse(data.decode("utf-8-sig"), filename=record["path"], feature_version=(3, 11))
                python_count += 1
            except SyntaxError:
                issues.append("syntax:" + record["path"])
    for name in ("README.md", "Supergent--main/README.md", "docs/INSTALLATION.md", "docs/ARCHITECTURE.md", "docs/VALIDATION.md", "docs/DISTRIBUTION.md"):
        path = root / name
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if target.startswith(("http:", "https:", "#")):
                continue
            target = target.split("#")[0]
            if not (path.parent / target).resolve().is_file():
                issues.append("documentation_link:" + name + ":" + target)
            links += 1
    required = set(manifest["required"])
    if required - {r["path"] for r in manifest["files"]}:
        issues.append("required_files")
    print(json.dumps({"verdict": "FAIL" if issues else "PASS", "files": len(manifest["files"]), "python_311_syntax_checked": python_count, "documentation_links_checked": links, "issues": issues}))
    return bool(issues)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    raise SystemExit(verify(parser.parse_args().root))
