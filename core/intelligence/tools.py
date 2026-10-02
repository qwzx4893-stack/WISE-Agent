"""Bounded typed local inspections. No arbitrary commands, live capture or exploits."""
from __future__ import annotations
import json
import shutil
import sqlite3
import subprocess
import time
from .inventory import profile, CLI_TOOLS

def execute_tool(identifier, *, path="", action="inspect", operation="sha256", text="", rules_path="", limit=20):
    resource=profile(identifier)
    if action != "inspect" or not resource.get("available"): raise ValueError("Tool adapter/dependency unavailable or action unsupported")
    limit=max(1,min(100,int(limit)))
    if identifier in {"detect-secrets","bandit"}:
        from core.security.local_scanners import scan
        return scan(identifier,path or ".")
    if identifier == "psutil":
        import psutil
        return {"cpu_percent":psutil.cpu_percent(interval=None),"memory":psutil.virtual_memory()._asdict(),
            "coverage":"AGGREGATE_RESOURCES_ONLY","success":True} # no process names/command lines or account data
    from core.tools_bridge import _resolve, WORKSPACE_DIR
    target=_resolve(path)
    relative=target.relative_to(WORKSPACE_DIR.resolve())
    if any(part.lower() in {"memory","sessions",".git",".tooling",".venv","node_modules"} for part in relative.parts): raise ValueError("Sensitive/runtime directories are excluded")
    if target.is_symlink() or not target.is_file(): raise ValueError("An authorized regular workspace file is required")
    if target.stat().st_size>64_000_000: raise ValueError("Inspection exceeds 64 MB file budget")
    started=time.perf_counter()
    if identifier == "sqlite":
        # immutable=1 prevents SQLite sidecars; no SQL supplied by the model.
        with sqlite3.connect(target.as_uri()+"?mode=ro&immutable=1",uri=True) as connection:
            connection.set_progress_handler(lambda:int(time.perf_counter()-started>3),1000)
            records=[{"name":row[0],"type":row[1]} for row in connection.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT ?",(limit,))]
        return {"success":True,"records":records,"coverage":"SQLITE_SCHEMA_ONLY"}
    if identifier == "stix-taxii":
        if target.stat().st_size>2_000_000: raise ValueError("STIX inspection exceeds JSON budget")
    if identifier in {"duckdb","yara","stix-taxii"}:
        # Native parsers run in an owned, offline, killable worker rather than
        # tying up the server event loop. The worker repeats all path checks.
        import sys
        from core.paths import REPO_ROOT
        payload={"tool":identifier,"root":str(WORKSPACE_DIR.resolve()),"path":str(target),"rules_path":str(_resolve(rules_path)) if rules_path else "","limit":limit}
        from contextlib import nullcontext
        from core.security.optional_tools import OptionalToolManager
        lease=OptionalToolManager().lease(identifier) if identifier != "duckdb" else nullcontext(sys.executable)
        with lease as python:
            process=subprocess.run([str(python),str(REPO_ROOT/"core/intelligence/local_worker.py")],input=json.dumps(payload),
                cwd=str(REPO_ROOT),capture_output=True,text=True,encoding="utf-8",timeout=15,
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if process.returncode or len(process.stdout)>100_000: raise ValueError("Offline parser failed or exceeded output budget")
        return json.loads(process.stdout)
    executable=shutil.which(CLI_TOOLS[identifier])
    if not executable: raise ValueError("Required executable is not installed")
    filename=str(target)
    formats={".wav":"wav",".mp3":"mp3",".mp4":"mov",".m4a":"mov",".flac":"flac",".ogg":"ogg",".webm":"matroska"}
    if identifier == "ffmpeg" and target.suffix.lower() not in formats: raise ValueError("Unsupported media format; playlists and network streams are not permitted")
    commands={"exiftool":["-json",filename],"ffmpeg":["-v","error","-protocol_whitelist","file,pipe","-f",formats.get(target.suffix.lower(),"wav"),"-show_format","-show_streams","-of","json",filename],
        "tesseract-ocr":[filename,"stdout","-l","eng"],"gdal":["-json",filename],
        "wireshark":["-M",filename],"radare2":["-I","-j",filename],"the-sleuth-kit":[filename]}
    if identifier == "yara":
        rules=_resolve(rules_path)
        if not rules.is_file() or rules.stat().st_size>256_000: raise ValueError("Small authorized YARA rule file required")
        commands[identifier]=[str(rules),filename] # no -s: matched bytes may contain secrets
    # Redirect output to an owned bounded temporary file, not unbounded PIPE RAM.
    import tempfile
    with tempfile.TemporaryFile() as output:
        process=subprocess.Popen([executable,*commands[identifier]],cwd=str(WORKSPACE_DIR),shell=False,stdout=output,stderr=output,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        deadline=time.monotonic()+20
        try:
            while process.poll() is None:
                if time.monotonic()>deadline or output.seek(0,2)>1_000_000:
                    raise TimeoutError("Tool time/output budget exceeded")
                time.sleep(.05)
        finally:
            if process.poll() is None: process.kill()
            process.wait(timeout=5)
        if process.returncode: raise ValueError(f"Tool exited with code {process.returncode}; no success was inferred")
        output.seek(0); raw=output.read(1_000_001)
        if len(raw)>1_000_000: raise ValueError("Tool output exceeds bounded budget")
    return {"success":True,"resource_id":identifier,"path":path,"output":raw.decode("utf-8",errors="replace")[:20000],
        "truncated":len(raw)>20000,"coverage":"OFFLINE_FILE_INSPECTION", "notice":"Metadata/OCR may contain personal data; use only authorized files"}
