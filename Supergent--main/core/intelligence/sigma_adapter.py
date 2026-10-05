"""Typed local Sigma inspection in a leased, killable upstream parser worker."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from .sigma_worker import MAX_INPUT, MAX_DOCUMENTS, EXCLUDED


def validated_result(result: object) -> dict:
    if not isinstance(result, dict) or result.get("success") is not True or result.get("resource_id") != "sigma":
        raise ValueError("Offline Sigma parser did not return a validated result")
    records = result.get("records")
    total = result.get("total")
    if (type(result.get("valid")) is not bool or type(result.get("limited")) is not bool
        or type(total) is not int or not 1 <= total <= MAX_DOCUMENTS or not isinstance(records, list)
        or not 1 <= len(records) <= total or result["limited"] != (len(records) < total)
        or result.get("parser") != "pysigma" or result.get("parser_version") != "1.5.1"
        or result.get("coverage") != "OFFLINE_SIGMA_PARSE_AND_CONDITION_VALIDATION_ONLY"
        or not isinstance(result.get("content_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", result["content_sha256"])):
        raise ValueError("Offline Sigma result violated its schema")
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict) or record.get("document") != index or type(record.get("valid")) is not bool:
            raise ValueError("Offline Sigma document identity/validity is malformed")
        diagnostics = record.get("diagnostics")
        if (not isinstance(diagnostics, list) or len(diagnostics) > 16
            or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,100}", item) for item in diagnostics)
            or record["valid"] != (not diagnostics)):
            raise ValueError("Offline Sigma diagnostics are malformed")
    if result["valid"] and any(not record["valid"] for record in records):
        raise ValueError("Offline Sigma validity conflicts with its diagnostics")
    if not result["limited"] and result["valid"] != all(record["valid"] for record in records):
        raise ValueError("Offline Sigma aggregate validity is inconsistent")
    return result


def inspect_sigma(path: str, *, limit=20) -> dict:
    from core.tools_bridge import _resolve, WORKSPACE_DIR
    from core.paths import REPO_ROOT
    from core.security.optional_tools import OptionalToolManager
    if not isinstance(path, str) or not path or len(path) > 1000 or any(ord(char) < 32 for char in path):
        raise ValueError("A bounded workspace Sigma YAML path is required")
    if (WORKSPACE_DIR / path).is_symlink(): raise ValueError("Symlink Sigma rule input excluded")
    target = _resolve(path)
    relative = target.relative_to(WORKSPACE_DIR.resolve())
    if any(part.casefold() in EXCLUDED for part in relative.parts) or target.is_symlink():
        raise ValueError("Sensitive/runtime Sigma path excluded")
    if not target.is_file() or target.suffix.casefold() not in {".yaml", ".yml"} or target.stat().st_size > MAX_INPUT:
        raise ValueError("An authorized YAML rule file up to 256 KB is required")
    payload = json.dumps({"root": str(WORKSPACE_DIR.resolve()), "path": str(target),
                          "limit": max(1, min(MAX_DOCUMENTS, int(limit)))})
    if len(payload) > 4096: raise ValueError("Sigma worker request exceeds its budget")
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    # Installation is a reviewed separate lease operation. The actual parser
    # has no provider keys/proxy config and blocks Python socket access.
    with OptionalToolManager().lease("sigma") as python, tempfile.TemporaryFile() as output:
        process = subprocess.Popen([str(python), "-I", str(Path(REPO_ROOT) / "core/intelligence/sigma_worker.py")],
            stdin=subprocess.PIPE, stdout=output, stderr=output, shell=False, env=env,
            cwd=str(WORKSPACE_DIR), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 15
        try:
            process.stdin.write(payload.encode("utf-8")); process.stdin.close()
            while process.poll() is None:
                if time.monotonic() >= deadline or output.seek(0, 2) > 100_000:
                    raise TimeoutError("Offline Sigma validation exceeded its time/output budget")
                time.sleep(.05)
        finally:
            if process.poll() is None: process.kill()
            process.wait(timeout=5)
        output.seek(0); raw = output.read(100_001)
        if process.returncode or len(raw) > 100_000:
            raise ValueError("Offline Sigma parser unavailable or rejected bounded input")
    return validated_result(json.loads(raw))
