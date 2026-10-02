"""Fixed-command, offline defensive scanners. No host/network scan or shell."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

SCANNERS = {"detect-secrets":"detect_secrets", "bandit":"bandit"}
EXCLUDED = {".git",".tooling","node_modules",".venv","venv","__pycache__","memory","sessions"}


def available_scanners():
    # Reviewed recipes, not a claim that the package is globally installed.
    return {name: True for name in SCANNERS}


def scan(scanner, path="."):
    from core.tools_bridge import _resolve
    if scanner not in SCANNERS: raise ValueError("Unsupported local scanner")
    if not available_scanners()[scanner]: raise ValueError("Scanner is not installed: " + scanner)
    target = _resolve(path)
    if not target.exists(): raise ValueError("Scan target does not exist")
    files, skipped = [], 0
    def collect(candidate):
        nonlocal skipped
        resolved = _resolve(str(candidate))
        if candidate.is_symlink() or not candidate.is_file(): skipped += 1; return
        from core.tools_bridge import WORKSPACE_DIR
        if any(part.lower() in EXCLUDED for part in candidate.relative_to(WORKSPACE_DIR.resolve()).parts): skipped += 1; return
        if scanner == "bandit" and candidate.suffix.lower() != ".py": skipped += 1; return
        if resolved.stat().st_size > 256_000: skipped += 1; return
        files.append(resolved)
        if len(files) > 100: raise ValueError("Narrow the scan target; maximum 100 files per call")
    if target.is_file(): collect(target)
    else:
        for directory, folders, names in os.walk(target, followlinks=False):
            folders[:] = [name for name in folders if name.lower() not in EXCLUDED and not (Path(directory)/name).is_symlink()]
            for name in names: collect(Path(directory)/name)
    if not files: raise ValueError("No eligible files; use a smaller text/Python source directory")
    paths = list(map(str,files))
    from core.paths import REPO_ROOT
    try:
        from core.tools_bridge import WORKSPACE_DIR
        from .optional_tools import OptionalToolManager
        with OptionalToolManager().lease(scanner) as python:
            args = ([str(python),str(REPO_ROOT/"core/security/secret_scan_worker.py")] if scanner == "detect-secrets"
                    else [str(python),"-m","bandit","-f","json","--ignore-nosec",*paths])
            process = subprocess.run(args, shell=False, cwd=str(REPO_ROOT), capture_output=True, text=True,
                input=json.dumps({"root":str(WORKSPACE_DIR.resolve()),"files":paths}) if scanner == "detect-secrets" else None,
                encoding="utf-8", errors="replace",timeout=25,
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    except subprocess.TimeoutExpired:
        raise ValueError("Local scanner timed out; narrow the target") from None
    if process.returncode not in (0,1) or len(process.stdout) > 5_000_000:
        raise ValueError("Local scanner failed; inspect local diagnostics without exposing source contents")
    try: data = json.loads(process.stdout)
    except ValueError: raise ValueError("Scanner did not return valid JSON") from None
    findings = []
    if scanner == "detect-secrets":
        for filename, rows in data.get("results",{}).items():
            for row in rows:
                # Neither secret value nor hash is returned to the model.
                findings.append({"path":filename,"line":row.get("line_number"),"type":row.get("type"),"status":"REVIEW_REQUIRED"})
    else:
        if data.get("errors"): raise ValueError("Bandit could not analyze all requested files")
        for row in data.get("results",[]):
            findings.append({"path":row.get("filename"),"line":row.get("line_number"),"rule":row.get("test_id"),
                "severity":row.get("issue_severity"),"confidence":row.get("issue_confidence"),"status":"REVIEW_REQUIRED"})
    return {"scanner":scanner,"offline":True,"files_submitted":len(files),"files_skipped":skipped,
        "findings":findings[:200],"finding_count":len(findings),"truncated":len(findings)>200,
        "coverage":"BOUNDED_LOCAL_SCAN", "note":"Findings require review; zero findings is not proof of security. No secrets were verified online."}
