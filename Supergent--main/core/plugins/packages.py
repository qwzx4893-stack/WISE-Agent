"""Bounded package retrieval and capability preview. No lifecycle scripts run.

The archive is data. Paths, symlinks, Windows aliases, sizes and manifests are
checked before activation. Host-specific hooks/agents/LSP/apps are not silently
pretended to be supported by WISE.
"""
import io
import json
import re
import stat
import time
import zipfile
from pathlib import Path
import httpx
from core.public_http import PublicTransport
from .catalog import bounded_json, public_https
from .marketplace import package_path, repo_name

MAX_ARCHIVE = 64_000_000
MAX_PACKAGE = 25_000_000
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def download_archive(repo, sha):
    repo_name(repo)
    if not re.fullmatch(r"[a-fA-F0-9]{40}",sha): raise ValueError("An immutable package commit is required")
    started = time.monotonic()
    with httpx.Client(transport=PublicTransport(),timeout=30,trust_env=False,follow_redirects=False) as client:
        with client.stream("GET",f"https://codeload.github.com/{repo}/zip/{sha}") as response:
            response.raise_for_status()
            data = bytearray()
            for part in response.iter_bytes():
                data.extend(part)
                if len(data)>MAX_ARCHIVE or time.monotonic()-started>120: raise ValueError("Package download exceeds budget")
    return bytes(data)


def extract_package(data, subtree, destination):
    if len(data)>MAX_ARCHIVE: raise ValueError("Archive exceeds budget")
    subtree = package_path(subtree)
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()): raise ValueError("Package destination must be empty")
    destination.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members)>40000: raise ValueError("Too many archive members")
        roots = {m.filename.split("/")[0] for m in members}
        if len(roots)!=1: raise ValueError("Ambiguous repository archive")
        root = next(iter(roots))
        prefix = root + "/" + (subtree+"/" if subtree else "")
        selected, seen, size = [], set(), 0
        for member in members:
            if not member.filename.startswith(prefix) or member.is_dir(): continue
            relative = package_path(member.filename[len(prefix):])
            if not relative: continue
            if stat.S_ISLNK(member.external_attr>>16): raise ValueError("Symlinks are not installable plugin files")
            # Case-insensitive collisions matter even on a POSIX QA host.
            if relative.casefold() in seen: raise ValueError("Duplicate plugin file path")
            seen.add(relative.casefold()); size += member.file_size
            if size>MAX_PACKAGE or member.file_size>5_000_000 or len(selected)>=2500:
                raise ValueError("Plugin contents exceed budget")
            selected.append((member,relative))
        if not selected: raise ValueError("Plugin directory was not found at this commit")
        for member,relative in selected:
            target = destination / relative
            if not target.resolve().is_relative_to(destination.resolve()): raise ValueError("Escaping archive member")
            target.parent.mkdir(parents=True,exist_ok=True)
            # CRC and bounded declared sizes checked by zipfile; never extractall.
            content = archive.read(member)
            if len(content)!=member.file_size: raise ValueError("Invalid archive member size")
            target.write_bytes(content)
    return len(selected)


def read_json(root, path):
    target = root / package_path(path)
    if not target.is_file() or target.stat().st_size>1_000_000: raise ValueError("Missing or oversized plugin manifest")
    result = json.loads(target.read_text(encoding="utf-8-sig"))
    if not isinstance(result,dict): raise ValueError("Invalid plugin manifest")
    return result


def locations(root, declared, default):
    values = ([declared] if isinstance(declared,str) else declared) if declared is not None else [default]
    if not isinstance(values,list) or len(values)>100: raise ValueError("Unsupported component path declaration")
    paths = []
    for value in values:
        path = root / package_path(value)
        if not path.resolve().is_relative_to(root.resolve()): raise ValueError("Component outside plugin")
        if declared is not None and not path.exists(): raise ValueError("Declared plugin component is missing")
        if path.exists(): paths.append(path)
    return paths


def inspect_package(root, entry):
    root = Path(root)
    manifest = None
    for path in ("plugin.json", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json", ".agent-plugin/plugin.json"):
        if (root/path).is_file():
            manifest = read_json(root,path); break
    if manifest is None:
        # Claude's manifest is optional for conventional marketplace packages.
        manifest = {"name":entry["name"],"version":entry.get("version","")}
    if manifest.get("name") != entry["name"]: raise ValueError("Manifest identity does not match marketplace package")
    overlay = manifest.get("extensions",{}).get("com.openai",{})
    if not isinstance(overlay,dict): raise ValueError("Invalid plugin overlay")
    manifest = {**manifest,**{k:v for k,v in overlay.items() if k in {
        "skills","commands","agents","hooks","mcpServers","lspServers","apps","monitors","userConfig","dependencies","interface"}}}
    if entry.get("strict") is False: manifest.update(entry.get("overrides",{}))
    workflows, unsupported, seen = [], [], set()
    for kind, filename, default in (("skills","SKILL.md","skills"),("commands","*.md","commands")):
        try: roots = locations(root,manifest.get(kind),default)
        except ValueError as error:
            unsupported.append(str(error)+" ("+kind+")"); continue
        for directory in roots:
            files = ([directory] if directory.is_file() else sorted(directory.rglob(filename)))
            for file in files:
                if kind=="skills" and file.name!="SKILL.md" or kind=="commands" and file.suffix!=".md":
                    unsupported.append("Unsupported workflow file: "+file.relative_to(root).as_posix());continue
                rel = file.relative_to(root).as_posix()
                if rel in seen: continue
                if file.stat().st_size>256000: raise ValueError("Workflow instructions exceed budget")
                content = file.read_text(encoding="utf-8-sig")
                if not content.strip(): raise ValueError("Empty workflow instructions")
                meta = {}
                if content.startswith("---"):
                    import yaml
                    pieces = content.split("---",2)
                    if len(pieces)==3:
                        parsed = yaml.safe_load(pieces[1]); meta = parsed if isinstance(parsed,dict) else {}
                        if any(k in meta and not isinstance(meta[k],str) for k in ("name","description","context","agent")):
                            raise ValueError("Workflow metadata values must be text")
                if meta.get("context")=="fork" or meta.get("agent"):
                    unsupported.append("Forked skill execution: "+rel); continue
                seen.add(rel)
                workflows.append({"file":rel,"name":str(meta.get("name") or (file.parent.name if kind=="skills" else file.stem))[:100],
                    "description":str(meta.get("description") or entry.get("description") or file.stem)[:1500],"kind":kind})
    for kind, default in (("hooks","hooks/hooks.json"),("agents","agents"),("lspServers",".lsp.json"),("apps",".app.json"),("monitors","monitors")):
        if manifest.get(kind) or (root/default).exists(): unsupported.append("Host-specific component: "+kind)
    if manifest.get("dependencies"): unsupported.append("Plugin dependencies need separate installation")
    if manifest.get("userConfig"): unsupported.append("Host-specific userConfig is not supported")
    mcp = {}
    declared = manifest.get("mcpServers")
    if isinstance(declared,dict): mcp = declared.get("mcpServers",declared)
    else:
        default = "mcp.json" if (root/"mcp.json").exists() else ".mcp.json"
        try:
            for file in locations(root,declared,default):
                raw = read_json(root,file.relative_to(root).as_posix())
                raw = raw.get("mcpServers",raw.get("servers",raw))
                if not isinstance(raw,dict): raise ValueError("Invalid bundled MCP configuration")
                if set(mcp).intersection(raw): raise ValueError("Duplicate bundled server")
                mcp.update(raw)
        except ValueError as error: unsupported.append(str(error)+" (MCP)")
    servers = []
    if len(mcp)>12: raise ValueError("Too many bundled servers")
    for name,config in mcp.items():
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",name) or not isinstance(config,dict): raise ValueError("Invalid bundled server")
            cfg = {k:config[k] for k in ("command","args","env","url","headers") if k in config}
            if config.get("type") not in (None,"http","stdio","streamable-http"): raise ValueError("Unsupported MCP transport")
            if not cfg.get("url") and not cfg.get("command"): raise ValueError("Server has no transport")
            if not isinstance(cfg.get("args",[]),list) or any(not isinstance(a,str) for a in cfg.get("args",[])): raise ValueError("Invalid server arguments")
            if any(not isinstance(cfg.get(g,{}),dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in cfg.get(g,{}).items()) for g in ("env","headers")): raise ValueError("Invalid server fields")
            text = json.dumps(cfg)
            variables = sorted(set(VARIABLE.findall(text))-{"CLAUDE_PLUGIN_ROOT","CODEX_PLUGIN_ROOT","PLUGIN_ROOT"})
            if "${" in VARIABLE.sub("",text): raise ValueError("Unsupported host variable")
            if len(variables)>40: raise ValueError("Too many connection fields")
            if cfg.get("url") and not VARIABLE.search(cfg["url"]): public_https(cfg["url"])
            if any("${wise-secret:" in v for g in ("env","headers") for v in cfg.get(g,{}).values()): raise ValueError("Foreign credential reference")
            servers.append({"name":name,"config":cfg,"local_execution":not bool(cfg.get("url")),
                "fields":[{"id":v,"label":v,"secret":True,"required":True} for v in variables]})
        except (ValueError,TypeError) as error: unsupported.append(f"{name}: {error}")
    interface = manifest.get("interface",{})
    if not isinstance(interface,dict): interface = {}
    return {"name":entry["name"], "display_name":str(interface.get("displayName") or entry["display_name"])[:150],
        "version":str(manifest.get("version") or entry.get("version") or "commit")[:80],
        "workflows":workflows,"servers":servers,"unsupported":list(dict.fromkeys(unsupported)),
        "can_install":bool(workflows or servers),"compatibility":"partial" if unsupported else "compatible",
        "file_count":sum(1 for p in root.rglob("*") if p.is_file())}


def resolve_server(component, root, values):
    expected = {f["id"] for f in component["fields"]}
    if set(values)!=expected: raise ValueError("Supply the required connection fields only")
    if any(not isinstance(v,str) or not v or len(v)>4096 or "${" in v or any(ord(c)<32 for c in v) for v in values.values()):
        raise ValueError("Invalid connection field")
    values = {**values,**{k:str(root) for k in ("CLAUDE_PLUGIN_ROOT","CODEX_PLUGIN_ROOT","PLUGIN_ROOT")}}
    def replace(value):
        if isinstance(value,str): return VARIABLE.sub(lambda m:values[m[1]],value)
        if isinstance(value,dict): return {k:replace(v) for k,v in value.items()}
        if isinstance(value,list): return [replace(v) for v in value]
        return value
    cfg = replace(component["config"])
    if cfg.get("url"): public_https(cfg["url"])
    return cfg
