"""Reviewed local security tools: lazy, isolated, leased and removable.

Never installs into the app/system interpreter. Only fixed recipes are accepted;
unknown catalog entries remain unavailable, not arbitrary pip/shell requests.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager

RECIPES = {"bandit": "bandit==1.8.6", "detect-secrets": "detect-secrets==1.5.0",
           "yara": "yara-python==4.5.4", "stix-taxii": "stix2==3.0.2", "sigma": "pysigma==1.5.1"}
_LOCK = threading.RLock()


class OptionalToolManager:
    def __init__(self, root=None, ttl_seconds=None):
        from core.paths import RUNTIME_ROOT
        self.root = Path(root or RUNTIME_ROOT / "optional-security-tools").resolve()
        self.ttl = max(0, int(ttl_seconds if ttl_seconds is not None else os.getenv("WISE_SECURITY_TOOL_TTL_SECONDS", "3600")))

    def _path(self, identifier):
        if identifier not in RECIPES: raise ValueError("No reviewed installation recipe for this tool")
        path = self.root / (identifier + "-" + RECIPES[identifier].split("==")[1] + f"-py{sys.version_info.major}{sys.version_info.minor}")
        if path.is_symlink() or path.resolve().parent != self.root: raise ValueError("Invalid managed tool path")
        return path

    @contextmanager
    def _locked(self, identifier, timeout=30):
        self._path(identifier) # Reject arbitrary names before creating lock files.
        self.root.mkdir(parents=True, exist_ok=True)
        with _LOCK, (self.root / (identifier + ".lock")).open("a+b") as stream:
            stream.seek(0); stream.write(b"0"); stream.flush(); stream.seek(0)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline: raise TimeoutError("Tool is busy; retry later") from None
                    time.sleep(.1)
            try: yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _run(args, timeout=180):
        # No provider keys, proxy credentials or pip configuration inherited.
        env = {key: value for key, value in os.environ.items() if key.upper() in
               {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "PATHEXT"}}
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(args, shell=False, env=env, stdout=output, stderr=output,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline or output.seek(0, 2) > 2_000_000:
                        raise TimeoutError("Security tool preparation exceeded its budget")
                    time.sleep(.1)
                if process.returncode: raise RuntimeError("Security tool preparation failed; no availability was inferred")
            finally:
                if process.poll() is None:
                    import psutil
                    children = psutil.Process(process.pid).children(recursive=True)
                    for child in reversed(children):
                        try: child.kill()
                        except psutil.NoSuchProcess: pass
                    process.kill()
                process.wait(timeout=10)

    def _install(self, identifier):
        import httpx
        from packaging.utils import parse_wheel_filename
        path = self._path(identifier)
        python = path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        manifest = path / "wise-install.json"
        if python.is_file() and manifest.is_file(): return python
        if path.exists(): shutil.rmtree(path) # exact, validated manager-owned recipe directory
        try:
            self._run([sys.executable, "-m", "venv", str(path)])
            wheels = path / "wheelhouse"; wheels.mkdir()
            self._run([str(python), "-m", "pip", "--isolated", "download", "--disable-pip-version-check",
                "--no-input", "--timeout", "15", "--retries", "0", "--only-binary=:all:",
                "--index-url", "https://pypi.org/simple", "--dest", str(wheels), RECIPES[identifier]])
            records = []
            for wheel in wheels.iterdir():
                if wheel.suffix != ".whl" or wheel.stat().st_size > 30_000_000:
                    raise ValueError("Only bounded official wheels are supported")
                name, version, _, _ = parse_wheel_filename(wheel.name)
                response = httpx.get(f"https://pypi.org/pypi/{name}/{version}/json", timeout=15, trust_env=False)
                response.raise_for_status()
                digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
                official = next((row for row in response.json()["urls"] if row["filename"] == wheel.name), None)
                if not official or official["digests"]["sha256"] != digest: raise ValueError("Wheel integrity verification failed")
                records.append({"distribution": str(name), "version": str(version), "filename": wheel.name, "sha256": digest})
            if not records or sum(row.stat().st_size for row in wheels.iterdir()) > 80_000_000:
                raise ValueError("Tool dependency budget exceeded")
            self._run([str(python), "-m", "pip", "--isolated", "install", "--disable-pip-version-check",
                       "--no-input", "--no-index", "--no-deps", *[str(row) for row in wheels.iterdir()]])
            shutil.rmtree(wheels)
            manifest.write_text(json.dumps({"recipe": RECIPES[identifier], "wheels": records, "last_used": time.time()}), encoding="utf-8")
            return python
        except Exception:
            if path.exists() and not path.is_symlink() and path.resolve().parent == self.root: shutil.rmtree(path)
            raise

    @contextmanager
    def lease(self, identifier):
        self.cleanup_idle()
        with self._locked(identifier):
            python = self._install(identifier)
            try: yield python
            finally:
                manifest = self._path(identifier) / "wise-install.json"
                data = json.loads(manifest.read_text(encoding="utf-8")); data["last_used"] = time.time()
                manifest.write_text(json.dumps(data), encoding="utf-8")
                if self.ttl == 0:
                    # Still under the lease lock, including exceptional exits.
                    shutil.rmtree(self._path(identifier))

    def remove(self, identifier):
        path = self._path(identifier)
        with self._locked(identifier, timeout=0):
            if path.exists(): shutil.rmtree(path)
        return {"id": identifier, "removed": True, "scope": "WISE_OWNED_CACHE_ONLY"}

    def cleanup_idle(self):
        for identifier in RECIPES:
            path = self._path(identifier); manifest = path / "wise-install.json"
            if not manifest.is_file(): continue
            try:
                if time.time() - json.loads(manifest.read_text(encoding="utf-8"))["last_used"] > self.ttl:
                    self.remove(identifier)
            except (OSError, ValueError, KeyError, TimeoutError): continue

    def status(self):
        self.cleanup_idle()
        return [{"id": identifier, "recipe": recipe, "mode": "ON_DEMAND", "installed": (self._path(identifier) / "wise-install.json").is_file(),
                 "idle_ttl_seconds": self.ttl} for identifier, recipe in RECIPES.items()]
