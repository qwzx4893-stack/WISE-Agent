"""MCP-first extension lifecycle. Catalog entries never grant execution consent.

Reuses WISE's registry, protected credentials, OAuth and capability router.
Only an actual live tools/list handshake produces CONNECTED. Native tools and
skills are included packs, not fake downloadable integrations.
"""
from __future__ import annotations
import copy
import json
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from .catalog import REGISTRY, bounded_json, featured, from_registry, identifier, public_https


class PluginError(ValueError):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        temp = Path(out.name)
        out.write(json.dumps(data, ensure_ascii=False).encode())
        out.flush(); os.fsync(out.fileno())
    from core.atomic_io import replace_file
    try: replace_file(temp, path)
    finally: temp.unlink(missing_ok=True)


class PluginManager:
    def __init__(self, root=None, registry=None, fetch=bounded_json):
        from core.paths import MEMORY_DIR
        from core.mcp import get_registry
        self.root = Path(root or MEMORY_DIR)
        self.registry = registry or get_registry()
        self.fetch = fetch
        self.lock = threading.RLock()
        self.syncing = False
        self.warning = ""
        self.attempts = {}
        self.operations = {}
        self.searches = set()
        self.search_times = {}
        from .market import seed_rows
        defaults=seed_rows() if root is None else []
        self.cache = self._read("plugin_catalog.json", {"time":0, "items":defaults})
        if root is None:
            seeded={row['id']:row for row in defaults}
            seeded.update({row['id']:row for row in self.cache.get('items',[])})
            self.cache['items']=list(seeded.values())[:8000]
        self.installed = self._read("plugins.json", {})

    def _read(self, name, default):
        path = self.root / name
        if not path.exists(): return copy.deepcopy(default)
        try:
            if path.stat().st_size > 12_000_000: raise ValueError()
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict): raise ValueError()
            return data
        except (ValueError, OSError):
            raise PluginError("Extension state is unreadable; restore its backup before changing extensions", 503) from None

    def _save(self): atomic(self.root / "plugins.json", self.installed)

    def refresh(self, force=False):
        with self.lock:
            if self.syncing or not force and time.time()-self.cache.get("time", 0) < 86400: return
            self.syncing = True
        threading.Thread(target=self._sync, daemon=True, name="wise-plugin-catalog").start()

    def _sync(self):
        try:
            from .market import fetch_snapshot,registry_row
            snapshot=fetch_snapshot(self.fetch)
            items={row['id']:row for raw in snapshot['servers'] if (row:=registry_row(raw))}
            if not items: raise ValueError("Empty catalog response")
            with self.lock:
                old = {row["id"]:row for row in self.cache.get("items", [])}
                if snapshot['complete']:
                    old={key:row for key,row in old.items() if row.get('source')!='community' or key in self.installed}
                old.update(items)
                self.cache = {"time":time.time(), "items":list(old.values())[:8000]}
                atomic(self.root / "plugin_catalog.json", self.cache)
                self.warning = "" if snapshot['complete'] else 'Registry refresh is partial; cached entries remain available'
        except Exception:
            with self.lock: self.warning = "Community directory unavailable; cached and featured extensions remain usable"
        finally:
            with self.lock: self.syncing = False

    def search_directory(self, query, force=False):
        query = query.strip().casefold()
        if not 2 <= len(query) <= 100: return
        with self.lock:
            if query in self.searches or len(self.searches)>=3 or not force and time.time()-self.search_times.get(query, 0)<900: return
            self.searches.add(query)
            self.search_times[query] = time.time()
        def run():
            try:
                data = self.fetch(REGISTRY, params={"search":query,"version":"latest","limit":10})
                entries = []
                for raw in data.get("servers", [])[:100]:
                    from .market import registry_row
                    try: row = registry_row(raw)
                    except (ValueError, TypeError, AttributeError): continue
                    if row: entries.append(row)
                with self.lock:
                    old = {r["id"]:r for r in self.cache.get("items", [])}
                    # Keep imports and installed recipes, retain bounded cache.
                    keep = {k:v for k,v in old.items() if v.get("source")=="imported" or k in self.installed}
                    old.update({r["id"]:r for r in entries})
                    self.cache["items"] = list({**dict(list(old.items())[-7800:]),**keep}.values())
                    atomic(self.root/"plugin_catalog.json",self.cache)
                    self.warning = ""
            except Exception as error:
                with self.lock: self.warning = f"Online directory search failed ({type(error).__name__}); showing cached matches. Retry when online."
            finally:
                with self.lock: self.searches.discard(query)
        threading.Thread(target=run,daemon=True,name="wise-plugin-search").start()

    def _included(self):
        from core.capability_router import get_capability_router
        from core.skills.indexer import SkillIndexer
        packs = {}
        for cap in get_capability_router().list_capabilities():
            if str(getattr(cap.source, "value", cap.source)).upper() == "MCP": continue
            category = str(cap.category or "General")
            key = "native_" + identifier(category)[4:]
            pack = packs.setdefault(key, {"id":key, "name":category.title()+" tools", "source":"native",
                "categories":[category], "description":"Built-in WISE capabilities, governed by the existing execution policy.",
                "version":"included", "logo":"", "fields":[], "recipe":None, "unsupported":"", "tools":[], "status":"INCLUDED"})
            pack["tools"].append({"name":cap.name, "description":cap.description, "status":cap.runtime_status,
                                  "available":bool(cap.availability and callable(cap.execution_adapter))})
        for info in SkillIndexer().get_index().values():
            key = "skill_" + identifier(str(info["path"]))[4:]
            packs[key] = {"id":key, "name":info["name"], "source":"skills", "categories":[info.get("category", "general")],
                "description":info.get("description", ""), "version":info.get("version", ""), "logo":"", "fields":[],
                "recipe":None, "unsupported":"", "tools":[], "status":"INCLUDED",
                "skill_name":info["name"], "skill_preview":info.get("excerpt", "")}
        return list(packs.values())

    def _rows(self, include_internal=True):
        from .connectors import service_rows
        services = service_rows()
        rows = {r["id"]:copy.deepcopy(r) for r in self.cache.get("items", [])}
        # Avoid duplicate service cards while retaining installed recipe identity.
        urls = {r["recipe"]["url"] for r in services if r["recipe"].get("url")}
        rows = {k:r for k,r in rows.items() if (r.get("recipe") or {}).get("url") not in urls or k in self.installed}
        rows.update({r["id"]:r for r in services})
        rows.update({k:copy.deepcopy(v["entry"]) for k,v in self.installed.items()})
        for service in services:
            # Refresh display identity without changing an installed endpoint,
            # credentials, ownership or execution recipe.
            for key in ('display_name','publisher','description','logo','category','categories',
                        'connection_type','auth_mode','download_required','setup_note','docs_url'):
                if key in service: rows[service['id']][key]=service[key]
        owned = {r["server"] for r in self.installed.values()}
        represented = {cfg.name for r in rows.values() if (cfg:=self._linked(r)) is not None}
        for cfg in self.registry._configs:
            if cfg.name in owned | represented | {"agent-os"}: continue
            key = "existing_"+identifier(cfg.name)[4:]
            rows[key] = {"id":key, "name":cfg.name, "publisher_id":cfg.name, "source":"existing", "categories":["MCP"],
                "description":cfg.description, "version":"configured", "fields":[], "recipe":None,"logo":"", "docs_url":"",
                "existing_server":cfg.name, "unsupported":"This MCP is already configured in WISE. Manage its connection in MCP Settings."}
        if include_internal: rows.update({r["id"]:r for r in self._included()})
        return list(rows.values())

    def _linked(self, row):
        if row.get("existing_server"): return self.registry._find(row["existing_server"])
        if row["id"] in self.installed: return None
        recipe = row.get("recipe") or {}
        if recipe.get("kind") not in {"remote","local-app"}: return None
        target = recipe["url"].rstrip("/")
        for cfg in self.registry._configs:
            if cfg.transport != "http": continue
            url = cfg.url.rstrip("/")
            if url == target or row["id"] == "context7" and url == "https://mcp.context7.com/mcp/oauth": return cfg
        return None

    def _present(self, row):
        result = {k:v for k,v in row.items() if k != "recipe"}
        if result.get("logo", "").startswith("https://"): result["logo"] = "/api/v2/plugins/logos/"+row["id"]
        record = self.installed.get(row["id"])
        linked = self._linked(row)
        result.update(installed=bool(record), status=row.get("status", "AVAILABLE"), tools=row.get("tools", []), error="")
        recipe = row.get("recipe") or {}
        result["kind"] = recipe.get("kind", row["source"])
        result["local_execution"] = recipe.get("kind") in {"package", "import-stdio", "local-package", "bundle"}
        result["local_access"] = recipe.get("kind") == "local-app"
        result["endpoint"] = recipe.get("url", "")
        result["package"] = recipe.get("package", "")
        result["can_connect"] = bool(recipe) and not row.get("unsupported")
        result["managed_elsewhere"] = bool(linked)
        if linked:
            found = next((s for s in self.registry.list_servers() if s["name"] == linked.name), None)
            result.update(can_connect=True, fields=[], existing_server=linked.name,
                unsupported="", status="CONNECTED" if found and found["alive"] else
                "AUTH_REQUIRED" if found and found.get('oauth_required') else "DISABLED" if not linked.enabled else "INSTALLED")
            result["tools"] = [{"name":t["name"], "description":t.get("description", ""), "classification":t.get("kind", "")}
                for t in self.registry.list_tools(linked.name)] if found and found["alive"] else []
        if record:
            found = next((s for s in self.registry.list_servers() if s["name"] == record["server"]), None)
            result["status"] = "CONNECTED" if found and found.get("alive") else "DISABLED" if found and not found.get("enabled", True) else "INSTALLED"
            result["tools"] = [{"name":t["name"], "description":t.get("description", ""), "classification":t.get("kind", "")}
                               for t in self.registry.list_tools(record["server"])] if found and found.get("alive") else []
            result["error"] = record.get("error", "")
            result["server"] = record["server"]
            if result["status"] == "INSTALLED" and record.get("status") in {"FAILED", "AUTH_REQUIRED"}:
                result["status"] = record["status"]
        return result

    def catalog(self, query="", category="", source="", connected=False, offset=0, limit=48, refresh=False):
        self.refresh(refresh and not query)
        if source in {"", "community"}: self.search_directory(query, refresh)
        with self.lock:
            rows = self._rows()
            categories = sorted({c for r in rows for c in r["categories"]})
            visible = [self._present(r) for r in rows if (not category or category in r["categories"])
                and (not source or source == r["source"] or source == "installed" and (r["id"] in self.installed or self._linked(r)))
                and (not query or query.casefold() in " ".join([r["name"], r.get("publisher_id", ""), r["description"], *r["categories"]]).casefold())]
            if connected: visible = [r for r in visible if r["status"] == "CONNECTED"]
            visible.sort(key=lambda r: (r["source"] != "featured", r["name"].casefold()))
            return {"items":visible[offset:offset+limit], "total":len(visible), "categories":categories,
                "next_offset":offset+limit if offset+limit < len(visible) else None, "syncing":self.syncing or query.strip().casefold() in self.searches,
                "warning":self.warning, "community_count":len(self.cache.get("items", []))}

    def _entry(self, key):
        from .connectors import service_rows
        service=next((r for r in service_rows() if r['id']==key),None)
        if key in self.installed:
            entry=copy.deepcopy(self.installed[key]['entry'])
            if service:
                for field in ('display_name','publisher','description','logo','category','categories','connection_type','auth_mode','download_required','setup_note','docs_url'):
                    if field in service: entry[field]=service[field]
        else:
            entry=service or next((r for r in self.cache.get('items',[]) if r['id']==key),None)
        if entry is not None:
            if entry.get('source')=='community':
                from .market import describe
                entry=describe(entry)
            return copy.deepcopy(entry)
        if not key.startswith(('native_','skill_','existing_')): raise PluginError('Extension not found',404)
        entry = next((r for r in self._rows(include_internal=True) if r["id"] == key), None)
        if entry is None: raise PluginError("Extension not found", 404)
        return entry

    def detail(self, key):
        with self.lock: return self._present(self._entry(key))

    def _operation(self, key):
        with self.lock: return self.operations.setdefault(key, threading.Lock())

    def _config(self, entry, fields, trusted):
        from core.mcp.config_import import protect_config
        recipe = entry.get("recipe")
        if not recipe or entry.get("unsupported"): raise PluginError(entry.get("unsupported") or "This pack is already included")
        if recipe['kind'] in {'bundle','local-package'}:
            if not trusted: raise PluginError('Local connections run code with your account permissions; explicit trust is required',409)
            try:
                if recipe['kind']=='bundle':
                    from .bundles import resolve
                    local=resolve(entry,self.root,fields)
                else:
                    from .local import prepare_package
                    known={f['id']:f for f in entry['fields']}
                    if set(fields)-set(known): raise ValueError('Unknown local connection settings')
                    values={key:fields.get(key,spec['default']) for key,spec in known.items()}
                    if any(not isinstance(v,str) or len(v)>4096 or any(ord(c)<32 for c in v) or '${' in v for v in values.values()): raise ValueError('Invalid local connection setting')
                    local=prepare_package(entry,self.root,values)
            except (ValueError,OSError) as error:
                raise PluginError(str(error),409) from None
            return dict(name='wise_'+identifier(entry['id'])[4:],enabled=False,description=entry['name'],
                require_confirmation=True,auto_approve=[],**local)
        known = {f["id"]:f for f in entry["fields"]}
        if set(fields)-set(known): raise PluginError("Unknown connection fields")
        values = {}
        for key, spec in known.items():
            value = fields.get(key, spec["default"])
            if not isinstance(value, str) or len(value)>4096 or any(ord(c)<32 for c in value) or "${" in value:
                raise PluginError("Invalid field: "+spec["name"])
            if spec["required"] and not value: raise PluginError("Missing field: "+spec["name"])
            if spec["choices"] and value and value not in spec["choices"]: raise PluginError("Invalid choice: "+spec["name"])
            values[key] = value
        def value(raw, group, index):
            v = raw.get("value") or values.get(f"{group}_{index}", "")
            if group == "header" and isinstance(v, str) and "{" in v and "${" not in v:
                v = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]{0,79})\}", lambda m:values.get(f"{group}_{index}_"+m[1], ""),v)
            owned_ref = isinstance(v, str) and re.fullmatch(r"\$\{wise-secret:MCP_wise_"+identifier(entry["id"])[4:]+r"_[A-Za-z0-9_-]+\}", v)
            if not isinstance(v, str) or len(v)>4096 or any(ord(c)<32 for c in v) or "${" in v and not owned_ref:
                raise PluginError("Unsupported recipe value")
            if v and raw.get("format") == "bearer" and not owned_ref and not v.lower().startswith("bearer "): v = "Bearer "+v
            return v
        cfg = {"name":"wise_"+identifier(entry["id"])[4:], "enabled":False, "init_timeout":20, "call_timeout":20,
            "description":entry["name"], "require_confirmation":True, "auto_approve":[]}
        if recipe["kind"] == "remote":
            url = public_https(recipe["url"])
            cfg.update(transport="http", url=url, sandbox=True, allowed_domains=[urlsplit(url).hostname],
                headers={h["name"]:value(h, "header", i) for i,h in enumerate(recipe.get("headers", [])) if value(h,"header",i)})
            endpoint = urlsplit(url)
            if endpoint.hostname == "mcp.context7.com" and endpoint.path.rstrip("/") == "/mcp":
                # Reviewed public documentation lookup methods only. Do not
                # trust arbitrary remote annotations or override other tools.
                cfg["classifications"] = {"resolve-library-id":"read", "query-docs":"read"}
        elif recipe["kind"] == "local-app":
            # Only the reviewed Figma desktop bridge, never a catalog-supplied
            # private URL or arbitrary process. Connecting launches no program.
            if entry['id']!='figma-desktop' or recipe['url']!='http://127.0.0.1:3845/mcp':
                raise PluginError('Unreviewed local application endpoint')
            if not trusted: raise PluginError('Allow access to the local application before connecting',409)
            cfg.update(transport='http',url=recipe['url'],sandbox=True,
                allowed_domains=['127.0.0.1'],headers={})
        elif recipe["kind"] == "import-stdio":
            if not trusted: raise PluginError("Local extensions run code with your user permissions; explicit trust is required", 409)
            cfg.update(copy.deepcopy(recipe["config"]))
            cfg.update(name="wise_"+identifier(entry["id"])[4:], enabled=False, sandbox=False, require_confirmation=True, auto_approve=[])
        else:
            if not trusted: raise PluginError("Local extensions run code with your user permissions; explicit trust is required", 409)
            args = []
            for i,a in enumerate(recipe["arguments"]):
                v = value(a, "arg", i)
                if not v: continue
                if a["type"] == "named": args.append(a["name"])
                args.append(v)
            from .local import prepare_registry_package
            env={e['name']:value(e,'env',i) for i,e in enumerate(recipe['environment']) if value(e,'env',i)}
            try: cfg.update(prepare_registry_package(entry,self.root,args,env))
            except ValueError as error: raise PluginError(str(error),409) from None
        return cfg

    def connect(self, key, fields=None, trusted_local=False):
        lock = self._operation(key)
        if not lock.acquire(blocking=False): raise PluginError("Extension is busy; wait for the current operation", 409)
        try:
            # Dependency downloads must not hold the global catalog lock for
            # minutes. A per-connection lock still guards the whole operation.
            prepared=None
            with self.lock:
                preview=self._entry(key)
                previous=self.installed.get(key)
                prepare_local=(preview.get('recipe') or {}).get('kind') in {'local-package','bundle','package'} and (not previous or bool(fields))
            if prepare_local: prepared=self._config(preview,fields or {},trusted_local)
            with self.lock:
                entry = self._entry(key)
                if prepared and entry['recipe']!=preview['recipe']: raise PluginError('Connection recipe changed; review it again',409)
                linked = self._linked(entry)
                if linked:
                    if fields: raise PluginError('Change credentials for this shared connection in MCP Settings',409)
                    if entry.get('connection_type')=='local' and not trusted_local:
                        raise PluginError('Allow access to the local application before connecting',409)
                    self.registry.set_enabled(linked.name,True)
                    self.registry.start(linked.name)
                    # Borrow the existing connection; do not claim ownership,
                    # delete credentials or create a second configuration.
                    return self.detail(key)
                existing = self.installed.get(key)
                if existing and not fields:
                    cfg = self.registry._find(existing["server"])
                    if cfg and (cfg.transport == "stdio" or entry.get('connection_type')=='local') and not trusted_local: raise PluginError("Confirm local access before starting", 409)
                    if entry['recipe']['kind']=='bundle':
                        # Check persisted bundle integrity on every reconnect.
                        from .bundles import resolve
                        from core.mcp.config_import import resolve_values
                        resolve(entry,self.root,{k:json.loads(v) for k,v in resolve_values(existing.get('bundle_values',{})).items()})
                else:
                    cfg = prepared or self._config(entry, fields or {}, trusted_local)
                    if self.registry._find(cfg["name"]) and (not existing or existing["server"] != cfg["name"]): raise PluginError("Server name is owned by another configuration", 409)
                    from core.mcp.config_import import protect_config
                    cfg = protect_config(cfg)
                    self.registry.add(cfg)
                    self.installed[key] = {"server":cfg["name"], "entry":entry, "error":"", "status":"INSTALLED"}
                    if entry['recipe']['kind']=='bundle':
                        # Reconnect settings are secret-store references, never literal input values.
                        from core.secrets_store import SecretStore
                        import json as _json
                        refs={}
                        for field,value in (fields or {}).items():
                            secret=f'MCP_{cfg["name"]}_bundle_{field}'
                            SecretStore().set(secret,_json.dumps(value))
                            refs[field]='${wise-secret:'+secret+'}'
                        self.installed[key]['bundle_values']=refs
                    self._save()
                name = self.installed[key]["server"]
                self.registry.set_enabled(name, True)
            result = self.registry.start(name)
            with self.lock:
                self.installed[key]["status"] = "CONNECTED" if result.get("alive") else "FAILED"
                error = result.get("error", "")
                # Do not copy server stderr/responses/credential echoes into UI.
                self.installed[key]["error"] = "" if result.get("alive") else "MCP connection failed; review endpoint, credentials or local runtime and retry"
                auth = not result.get("alive") and any(x in error.lower() for x in ("http 401", "http 403", "authorize again"))
                if auth: self.installed[key]["status"] = "AUTH_REQUIRED"
                self._save()
            return self.detail(key)
        finally: lock.release()

    def connect_service(self, key, fields=None, trusted_local=False):
        from .connectors import service_rows
        allowed={row['id'] for row in service_rows()} | {row['id'] for row in self.cache.get('items',[]) if row.get('source') in {'bundle','community'}}
        allowed.update(key for key,record in self.installed.items() if record['entry'].get('source') in {'bundle','community'})
        if key not in allowed: raise PluginError('Service not found',404)
        result=self.connect(key,fields,trusted_local)
        response={'connection':result,'authorization':None}
        if result['status']=='AUTH_REQUIRED' and result.get('auth_mode') not in {'token','local'}:
            try: response['authorization']=self.authorize(key)
            except PluginError as error:
                # Keep the actual durable connection and allow explicit setup
                # or retry. A refused OAuth client is not a connected account.
                response['authorization_error']=str(error)
        return response

    def authorize(self, key, client_id="", client_metadata_url=""):
        lock = self._operation(key)
        if not lock.acquire(blocking=False): raise PluginError("Extension is busy; wait for the current operation", 409)
        try: return self._authorize(key, client_id, client_metadata_url)
        finally: lock.release()

    def _authorize(self, key, client_id="", client_metadata_url=""):
        from core.mcp import oauth
        with self.lock:
            linked = self._linked(self._entry(key)) if key not in self.installed else None
            if key not in self.installed and linked is None: raise PluginError("Connect this extension first", 409)
            if any(a["integration"] == key and oauth.status(a["id"])["status"] in {"pending", "exchanging"} for a in self.attempts.values()): raise PluginError("Authorization already pending", 409)
            name = self.installed[key]["server"] if key in self.installed else linked.name
        try:
            options={'client_id':client_id}
            if client_metadata_url: options['client_metadata_url']=client_metadata_url
            flow = oauth.start(name, self.registry, **options)
        except Exception as error:
            if isinstance(error, ValueError): raise PluginError(str(error)[:240], 409) from None
            raise PluginError("Authorization discovery failed; review the service's MCP setup guide", 502) from None
        if flow.get("requires_auth") is False: return {"status":"PUBLIC", "message":flow["message"]}
        attempt = {"id":flow["state"], "integration":key, "url":flow["auth_url"]}
        with self.lock: self.attempts[attempt["id"]] = attempt
        return {"id":attempt["id"], "integration":key, "status":"PENDING", "browser_opened":self.launch(attempt["id"])}

    def pending(self):
        from core.mcp.oauth import status
        with self.lock:
            return [{"id":a["id"], "integration":a["integration"]} for a in self.attempts.values() if status(a["id"])["status"] in {"pending", "exchanging"}]

    def poll(self, key):
        from core.mcp.oauth import status
        with self.lock:
            if key not in self.attempts: raise PluginError("Authorization expired", 404)
            return {"status":status(key)["status"].upper(), "error":status(key).get("error", "")}

    def launch(self, key):
        from core.mcp.oauth import status
        import sys
        import webbrowser
        with self.lock:
            attempt = self.attempts.get(key)
            if not attempt or status(key)["status"] != "pending": raise PluginError("No pending authorization", 409)
            url = attempt["url"]
        if sys.platform == "win32":
            from wise_desktop import DesktopAPI
            return bool(DesktopAPI().open_external_url(url))
        return bool(webbrowser.open(url))

    def cancel(self, key):
        from core.mcp.oauth import cancel
        cancel(key)
        return {"status":"CANCELLED"}

    def disable(self, key, remove=False):
        from core.mcp import oauth
        with self._operation(key), self.lock:
            record = self.installed.get(key)
            entry = self._entry(key)
            if not record and not (remove and entry["source"] == "imported"): raise PluginError("Extension is not installed", 404)
            for a in list(self.attempts.values()):
                if a["integration"] == key: oauth.cancel(a["id"])
            if record:
                self.registry.set_enabled(record["server"], False)
                self.registry.stop(record["server"])
            if remove:
                name = record["server"] if record else "wise_"+identifier(key)[4:]
                if record: self.registry.remove(name)
                from core.secrets_store import SecretStore
                store = SecretStore()
                for name in store.list():
                    if name.startswith("MCP_"+(record["server"] if record else "wise_"+identifier(key)[4:])+"_"): store.delete(name)
                self.installed.pop(key, None)
                if entry["source"] == "imported":
                    self.cache["items"] = [r for r in self.cache["items"] if r["id"] != key]
                    atomic(self.root/"plugin_catalog.json", self.cache)
            else: record.update(status="DISABLED", error="")
            self._save()
        return {"ok":True}

    def import_bundle(self, data):
        from .bundles import stage
        try:
            with self._operation('bundle-cache'): return stage(self,data)
        except Exception as error:
            if isinstance(error,ValueError): raise PluginError(str(error)) from None
            raise PluginError('Invalid or unreadable MCPB bundle') from error

    def probe_application(self,key):
        from .local import local_app_probe
        try: return local_app_probe(self._entry(key))
        except ValueError as error: raise PluginError(str(error)) from None

    def import_config(self, text, trusted_local=False):
        from core.mcp.config_import import parse_configs
        try: configs = parse_configs(text, trusted_local=trusted_local)
        except ValueError as error:
            # Registry validators can include raw configs in their error text.
            # Never return submitted credential values in an API exception.
            message = "Enter valid MCP JSON" if str(error) == "Enter valid MCP JSON" else "Invalid MCP configuration: use named servers, a valid command or HTTPS URL, and string args/headers/env"
            raise PluginError(message) from None
        for cfg in configs:
            for group in ("env", "headers"):
                if any("${" in v or any(ord(c)<32 for c in v) or len(v)>4096 for v in cfg.get(group, {}).values()): raise PluginError("Import literal credentials only; references to other secrets are not allowed")
            if any(len(a)>4096 or any(ord(c)<32 for c in a) for a in cfg.get("args", [])): raise PluginError("Invalid local arguments")
            if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", k) or k.lower() in {"host", "cookie", "proxy-authorization", "content-length"} for k in cfg.get("headers", {})): raise PluginError("Invalid remote headers")
        rows = []
        for cfg in configs:
            key = "import_"+identifier(cfg["name"])[4:]
            if cfg["transport"] == "http":
                public_https(cfg["url"])
                # Input secrets become protected references only on install.
                headers = [{"name":k, "value":v} for k,v in cfg.get("headers", {}).items()]
                recipe = {"kind":"remote", "url":cfg["url"], "headers":headers}
            else: recipe = {"kind":"import-stdio", "config":cfg}
            rows.append({"id":key, "publisher_id":cfg["name"], "name":cfg["name"], "source":"imported",
                "categories":["Custom"], "version":"owner", "description":cfg.get("description", ""), "fields":[],
                "logo":"", "docs_url":"", "recipe":recipe, "unsupported":""})
        with self.lock:
            if any(row["id"] in self.installed for row in rows): raise PluginError("Uninstall the existing imported extension before replacing it", 409)
            if any(self.registry._find("wise_"+identifier(row["id"])[4:]) for row in rows): raise PluginError("Imported name collides with another MCP configuration", 409)
        # Import does not run any code. Protect values before storing recipes.
        from core.mcp.config_import import protect_config
        for row in rows:
            r = row["recipe"]
            if r["kind"] == "remote":
                safe = protect_config({"name":"wise_"+identifier(row["id"])[4:], "headers":{h["name"]:h["value"] for h in r["headers"]}})
                r["headers"] = [{"name":k, "value":v} for k,v in safe["headers"].items()]
            else: r["config"] = protect_config({**r["config"], "name":"wise_"+identifier(row["id"])[4:]})
        with self.lock:
            for row in rows:
                if row["id"] in self.installed: raise PluginError("Uninstall the existing imported extension before replacing it", 409)
            old = {r["id"]:r for r in self.cache.get("items", [])}
            old.update({r["id"]:r for r in rows})
            self.cache["items"] = list(old.values())
            atomic(self.root / "plugin_catalog.json", self.cache)
        return {"items":[self._present(r) for r in rows]}


_instance = None
_guard = threading.Lock()
def get_plugin_manager():
    global _instance
    with _guard:
        if _instance is None: _instance = PluginManager()
        return _instance
