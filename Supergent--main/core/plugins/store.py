"""Real package store over WISE's existing skill and MCP engines.

The catalog contains plugin packages only. Base skills, base tools and manually
configured MCPs are never manufactured into marketplace cards. Installed
package ownership is separate from legacy MCP connection records.
"""
import copy
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from .catalog import bounded_json
from .manager import PluginError, atomic, get_plugin_manager
from .marketplace import DEFAULT_MARKETPLACES, PACKAGE_BRANDS, read_marketplace, repo_name
from .packages import download_archive, extract_package, inspect_package, resolve_server

STORE_GROUPS = {
    'Development': {'development','developer-tools','engineering','languages','language','api','sdk','testing','quality','utilities','code','database','devops','infrastructure','frameworks','blockchain','documentation','programming'},
    'Security': {'security','compliance','audit'},
    'Design': {'design','creative','frontend','ui-ux','content'},
    'Research': {'research','science','life-sciences','healthcare','education','learning'},
    'Data & AI': {'data','analytics','data-science','ai-ml','ai','machine-learning'},
    'Automation': {'automation','workflows','orchestration','monitoring'},
    'Productivity': {'productivity','business','sales','marketing','finance','support','communication','collaboration','project-management'},
}
KNOWN_SERVICES = {'github','gitlab','slack','notion','linear','asana','figma','stripe','sentry','vercel','supabase','cloudflare','atlassian','context7','playwright','google-drive','google-workspace','microsoft-365','azure','terraform','hubspot','salesforce','dropbox','box'}


def store_group(entry):
    if entry['category'] in STORE_GROUPS: return entry['category']
    category = entry['category'].casefold()
    for name,values in STORE_GROUPS.items():
        if category in values: return name
    return 'More plugins'


class PackageStore:
    def __init__(self, root=None, components=None, fetch=bounded_json, archive=download_archive):
        from core.paths import MEMORY_DIR
        self.root = Path(root or MEMORY_DIR)
        self.components = components or get_plugin_manager()
        self.fetch, self.archive = fetch, archive
        self.lock = threading.RLock()
        self.operations = {}
        self.syncing, self.warning = False, ""
        self.last_sync_attempt = 0
        self.sources = self._read("package_marketplaces.json", {"sources":DEFAULT_MARKETPLACES})["sources"]
        # Upgrade the built-in catalog without erasing user-added marketplaces.
        known = {s['repo'].casefold() for s in self.sources}
        self.sources += [copy.deepcopy(s) for s in DEFAULT_MARKETPLACES if s['repo'].casefold() not in known]
        self.cache = self._read("package_marketplace_cache.json", {"time":0,"snapshots":[]})
        self.installed = self._read("plugin_packages.json", {"packages":{}})["packages"]

    def _read(self,name,default):
        path = self.root/name
        if not path.exists(): return copy.deepcopy(default)
        try:
            if path.stat().st_size>8_000_000: raise ValueError()
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data,dict): raise ValueError()
            return data
        except (ValueError,OSError): raise PluginError("Package state is unreadable; restore its backup before changing plugins",503) from None

    def _save(self): atomic(self.root/"plugin_packages.json",{"packages":self.installed})

    def refresh(self,force=False):
        with self.lock:
            cached = {snapshot['repo'].casefold() for snapshot in self.cache['snapshots']}
            missing = any(s['repo'].casefold() not in cached for s in self.sources)
            recent_attempt = self.last_sync_attempt and time.time()-self.last_sync_attempt<300
            if self.syncing or not force and (recent_attempt or not missing and time.time()-self.cache.get("time",0)<86400): return
            self.syncing = True
            self.last_sync_attempt = time.time()
        threading.Thread(target=self._sync,daemon=True,name="wise-package-marketplaces").start()

    def _sync(self):
        failures = []
        with self.lock: snapshots = {s["repo"]:s for s in self.cache["snapshots"]}; sources = copy.deepcopy(self.sources)
        for source in sources:
            try: snapshots[source["repo"]] = read_marketplace(source,self.fetch)
            except Exception: failures.append(source["repo"])
        with self.lock:
            self.cache = {"time":time.time(),"snapshots":list(snapshots.values())}
            self.warning = ("Cannot refresh: "+", ".join(failures)+". Cached packages remain available; retry when online.") if failures else ""
            try: atomic(self.root/"package_marketplace_cache.json",self.cache)
            finally: self.syncing = False

    def _rows(self):
        rows = {}
        for snapshot in self.cache["snapshots"]:
            for entry in snapshot["items"]: rows.setdefault(entry["id"],entry)
        for key,record in self.installed.items(): rows.setdefault(key,record["entry"])
        return [{**r,'category':store_group(r)} for r in rows.values()]

    def _entry(self,key):
        with self.lock: row = next((r for r in self._rows() if r["id"]==key),None)
        if not row: raise PluginError("Plugin package not found",404)
        return copy.deepcopy(row)

    def _present(self,entry):
        record = self.installed.get(entry["id"])
        owner = repo_name(entry['origin']['repo']).split('/')[0]
        assets = Path(__file__).resolve().parents[2]/'ui/wise_web/assets/integrations'
        brand = entry['name'].casefold().removesuffix('-plugin')
        local = assets/(brand+'.svg')
        logo = '/app/assets/integrations/'+brand+'.svg' if local.is_file() else '/api/v2/plugins/packages/'+entry['id']+'/logo' if brand in PACKAGE_BRANDS else ''
        return {k:copy.deepcopy(entry[k]) for k in ("id","name","display_name","description","publisher","category","version","marketplace","marketplace_repo","curated","kind")} | {
            "status":("ENABLED" if record.get("enabled") else "DISABLED") if record else "AVAILABLE",
            "installed":bool(record),"installed_version":record["plan"]["version"] if record else "",
            "repository":"https://github.com/"+entry["origin"]["repo"], "logo":logo,
            "logo_kind":"brand" if logo else "neutral", "publisher":entry['publisher'] or owner,
            "components": {"skills":len(record["plan"]["workflows"]),"mcp":len(record["plan"]["servers"])} if record else None}

    def catalog(self,query="",category="",source="",offset=0,limit=48,refresh=False,**ignored):
        self.refresh(refresh)
        with self.lock:
            rows = self._rows()
            categories = sorted({r["category"] for r in rows})
            marketplaces = [{"repo":s["repo"],"name":s["name"],"count":len(s["items"]),"skipped":s["skipped"]} for s in self.cache["snapshots"]]
            if source=="installed": rows = [r for r in rows if r["id"] in self.installed]
            elif source=="curated": rows = [r for r in rows if r["curated"]]
            elif source=="community": rows = [r for r in rows if not r["curated"]]
            elif source not in {"","all"}: rows = []  # No internal inventory tabs.
            if category=='Featured': rows = [r for r in rows if r['name'].casefold() in KNOWN_SERVICES]
            elif category: rows = [r for r in rows if r["category"]==category]
            if query: rows = [r for r in rows if query.casefold() in " ".join(str(r.get(k,"")) for k in ("name","display_name","description","publisher","category")).casefold()]
            rows.sort(key=lambda r:(r['name'].casefold() not in KNOWN_SERVICES,not r["curated"],r["display_name"].casefold()))
            total = len(rows)
            groups = []
            if not query and not category:
                featured = [r for r in rows if r['name'].casefold() in KNOWN_SERVICES]
                if featured: groups.append({'name':'Featured','total':len(featured),'items':[self._present(r) for r in featured[:6]],'featured':True})
                for name in categories:
                    members = [r for r in rows if r['category']==name]
                    if members: groups.append({'name':name,'total':len(members),'items':[self._present(r) for r in members[:6]]})
            return {"items":[self._present(r) for r in rows[offset:offset+limit]],"total":total,"categories":categories,
                "sections":groups,
                "marketplaces":marketplaces,"next_offset":offset+limit if offset+limit<total else None,"syncing":self.syncing,"warning":self.warning}

    def add_marketplace(self,repository):
        repo = repo_name(repository)
        with self.lock:
            if len(self.sources)>=20: raise PluginError("At most 20 marketplaces may be registered")
            if any(s["repo"].lower()==repo.lower() for s in self.sources): raise PluginError("Marketplace already registered",409)
        # Verify a real marketplace before persisting, not a fabricated service.
        snapshot = read_marketplace({"repo":repo},self.fetch)
        with self.lock:
            if any(s["repo"].lower()==repo.lower() for s in self.sources): raise PluginError("Marketplace already registered",409)
            self.sources.append({"repo":repo,"label":snapshot["name"]})
            atomic(self.root/"package_marketplaces.json",{"sources":self.sources})
            self.cache["snapshots"].append(snapshot)
            atomic(self.root/"package_marketplace_cache.json",self.cache)
        return {"name":snapshot["name"],"count":len(snapshot["items"])}

    def _operation(self,key):
        with self.lock: return self.operations.setdefault(key,threading.Lock())

    def _package_dir(self,key,sha):
        if not re.fullmatch(r"pkg_[a-f0-9]{24}",key) or not re.fullmatch(r"[a-fA-F0-9]{40}",sha): raise PluginError("Invalid package location")
        base = self.root/"plugin-package-cache"
        target = base/key/sha
        if base.is_symlink() or target.is_symlink() or (base/key).is_symlink() or not target.resolve().is_relative_to(self.root.resolve()):
            raise PluginError("Unsafe package cache location")
        # Junctions/reparse points can escape even without being symlinks.
        for path in (base,base/key,target):
            if path.exists() and getattr(path.stat(),"st_file_attributes",0)&0x400: raise PluginError("Reparse-point package cache is not supported")
        return target

    def _prepare(self,entry):
        origin = entry["origin"]
        sha = origin.get("sha")
        if not sha:
            sha = self.fetch(f"https://api.github.com/repos/{repo_name(origin['repo'])}/commits/{origin['ref']}").get("sha","")
            origin["sha"] = sha
        target = self._package_dir(entry["id"],sha)
        marker = target/".wise-package.json"
        if marker.exists():
            try: valid = json.loads(marker.read_text(encoding="utf-8")) == {"id":entry["id"],"sha":sha}
            except (ValueError,OSError): valid = False
            if not valid: raise PluginError("Package cache marker is invalid; do not activate it",409)
        if not marker.exists():
            # A failed download/extraction is never activated. Keep an incomplete
            # directory out of the active version and use a fresh staging path.
            import tempfile
            target.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="preview-",dir=target.parent) as tmp:
                staged = Path(tmp)/"package"
                extract_package(self.archive(origin["repo"],sha),origin["path"],staged)
                plan = inspect_package(staged,entry)
                if target.exists(): raise PluginError("Incomplete package cache; inspect it before retrying",409)
                # Windows/OneDrive can deny directory renames while the indexer
                # observes freshly written files. Copy the inert validated data;
                # write the completion marker LAST. Nothing is active yet.
                import shutil
                shutil.copytree(staged,target)
                atomic(target/".wise-package.json",{"id":entry["id"],"sha":sha})
        plan = inspect_package(target,entry)
        return entry,target,plan

    @staticmethod
    def _public_plan(plan):
        return {k:copy.deepcopy(v) for k,v in plan.items() if k!="servers"} | {"servers":[{k:copy.deepcopy(c[k]) for k in ("name","fields","local_execution")} for c in plan["servers"]]}

    def detail(self,key):
        lock = self._operation(key)
        if not lock.acquire(False): raise PluginError("Plugin operation is already in progress",409)
        try:
            warning = ""
            try: entry,target,plan = self._prepare(self._entry(key))
            except Exception:
                with self.lock: previous = copy.deepcopy(self.installed.get(key))
                if not previous: raise
                entry,target,plan = self._prepare(previous["entry"])
                warning = "The newer package could not be fetched. Showing the cached installed version."
            with self.lock:
                record = copy.deepcopy(self.installed.get(key))
                result = self._present(entry)
            result.update(plan=self._public_plan(plan),commit=entry["origin"]["sha"],connections=[],warning=warning)
            if record:
                result["installed_plan"] = self._public_plan(record["plan"])
                result["installed_commit"] = record["entry"]["origin"]["sha"]
                for component,identifier in record.get("connections",{}).items():
                    try: status = self.components.detail(identifier)
                    except PluginError: continue
                    result["connections"].append({"name":component,"id":identifier,"status":status["status"],"tools":status["tools"],"error":status["error"]})
            return result
        finally: lock.release()

    def overview(self,key):
        """Catalog information is available without fetching executable packages.

        An unavailable archive must never turn a valid store listing into a blank
        page. Installation still uses detail(), pins the commit and inspects all
        supported components before any activation.
        """
        entry = self._entry(key)
        with self.lock:
            result = self._present(entry)
            record = copy.deepcopy(self.installed.get(key))
        result.update(commit=entry['origin'].get('sha',''), connections=[])
        if record:
            result['installed_plan'] = self._public_plan(record['plan'])
            result['installed_commit'] = record['entry']['origin']['sha']
        return result

    def install(self,key,commit,trust=False,accept_partial=False):
        if not trust: raise PluginError("Review and trust the plugin publisher before installing",409)
        lock = self._operation(key)
        if not lock.acquire(False): raise PluginError("Plugin operation is already in progress",409)
        try:
            candidate = self._entry(key)
            if candidate["origin"].get("sha") and candidate["origin"]["sha"]!=commit:
                raise PluginError("Package changed since preview; review its current commit",409)
            entry,target,plan = self._prepare(candidate)
            if entry["origin"]["sha"]!=commit: raise PluginError("Package changed since preview; review its current commit",409)
            if not plan["can_install"]: raise PluginError("No supported components in this plugin",409)
            if plan["unsupported"] and not accept_partial: raise PluginError("Review unsupported components and explicitly accept a partial install",409)
            with self.lock:
                old = self.installed.get(key)
                if old and old["entry"]["origin"]["sha"]!=commit:
                    # Updates never run code or migrate credentials silently.
                    if old.get("connections"): raise PluginError("Remove the connected old version before installing this update",409)
                record = {"entry":entry,"plan":plan,"enabled":True,"connections":old.get("connections",{}) if old else {},"installed_at":time.time()}
                self.installed[key] = record
                try: self._save()
                except Exception:
                    if old: self.installed[key] = old
                    else: self.installed.pop(key,None)
                    raise
            return {"ok":True,"id":key,"status":"ENABLED"}
        finally: lock.release()

    def connect(self,key,name,fields=None,trusted_local=False):
        lock = self._operation(key)
        if not lock.acquire(False): raise PluginError("Plugin operation is already in progress",409)
        try:
            with self.lock:
                record = self.installed.get(key)
                if not record or not record["enabled"]: raise PluginError("Install and enable the plugin first",409)
                component = next((s for s in record["plan"]["servers"] if s["name"]==name),None)
                if not component: raise PluginError("Bundled connection not found",404)
                target = self._package_dir(key,record["entry"]["origin"]["sha"])
                identifier = record["connections"].get(name)
                if not identifier:
                    cfg = resolve_server(component,target,fields or {})
                    if component["local_execution"] and not trusted_local: raise PluginError("Explicit local code execution approval is required",409)
                    owned_name = "pkg_"+hashlib.sha256((key+":"+name).encode()).hexdigest()[:24]
                    identifier = self.components.import_config(json.dumps({"mcpServers":{owned_name:cfg}}))["items"][0]["id"]
                    record["connections"][name] = identifier; self._save()
            # This is the only path that starts a bundled process or connection.
            return self.components.connect(identifier,trusted_local=trusted_local)
        finally: lock.release()

    def authorize(self,key,name,client_id=""):
        with self.lock:
            record = self.installed.get(key)
            if not record or not record["enabled"]: raise PluginError("Enable the plugin first",409)
            identifier = record["connections"].get(name)
            if not identifier: raise PluginError("Connect this bundled service before authorizing",409)
        return self.components.authorize(identifier,client_id)

    def reset_connection(self,key,name):
        lock = self._operation(key)
        if not lock.acquire(False): raise PluginError("Plugin operation is already in progress",409)
        try:
            with self.lock:
                record = self.installed.get(key)
                if not record: raise PluginError("Plugin is not installed",404)
                identifier = record["connections"].get(name)
                if not identifier: raise PluginError("Connection is not configured",404)
            self.components.disable(identifier,True)
            with self.lock:
                record["connections"].pop(name,None);self._save()
            return {"ok":True}
        finally: lock.release()

    def disable(self,key,remove=False):
        lock = self._operation(key)
        if not lock.acquire(False): raise PluginError("Plugin operation is already in progress",409)
        try:
            with self.lock: record = self.installed.get(key)
            if not record: raise PluginError("Plugin is not installed",404)
            for identifier in list(record["connections"].values()):
                self.components.disable(identifier,remove)
            with self.lock:
                record["enabled"] = False
                if remove: self.installed.pop(key)
                self._save()
            # Cache is intentionally not recursively deleted from state-supplied
            # paths. Uninstalled files are inert and no longer indexed/started.
            return {"ok":True,"status":"REMOVED" if remove else "DISABLED"}
        finally: lock.release()


def installed_workflows(root=None):
    """Lightweight metadata overlay; no directory fetch or engine construction.

    Called by WISE's existing skill browser. Disabled/uninstalled plugins vanish
    immediately, including after a process restart. Base skills are untouched.
    """
    from core.paths import MEMORY_DIR
    root = Path(root or MEMORY_DIR)
    path = root/"plugin_packages.json"
    if not path.exists(): return {}
    try:
        if path.stat().st_size>8_000_000: return {}
        packages = json.loads(path.read_text(encoding="utf-8"))["packages"]
        result = {}
        for key,record in packages.items():
            if not record.get("enabled") or not re.fullmatch(r"pkg_[a-f0-9]{24}",key): continue
            sha = record["entry"]["origin"]["sha"]
            if not re.fullmatch(r"[a-fA-F0-9]{40}",sha): continue
            package = root/"plugin-package-cache"/key/sha
            if not package.resolve().is_relative_to(root.resolve()) or not (package/".wise-package.json").is_file(): continue
            marker = package/'.wise-package.json'
            members = (root/'plugin-package-cache',package.parent,package,marker)
            if any(p.is_symlink() or getattr(p.stat(),'st_file_attributes',0)&0x400 for p in members): continue
            if marker.stat().st_size>1024 or json.loads(marker.read_text(encoding='utf-8'))!={'id':key,'sha':sha}: continue
            for item in record["plan"]["workflows"]:
                from .marketplace import package_path
                file = package/package_path(item["file"])
                if not file.is_file() or not file.resolve().is_relative_to(package.resolve()): continue
                name = record["entry"]["name"]+"@"+key[4:12]+":"+item["name"]
                result[name] = {"name":name,"path":str(file.parent),"file":str(file),"plugin_root":str(package),
                    "version":record["plan"]["version"],"category":record["entry"]["category"],
                    "description":item["description"],"excerpt":item["description"][:300],
                    "last_modified":file.stat().st_mtime,"metadata":{"source":"plugin","plugin_id":key}}
        return result
    except (ValueError,OSError,KeyError,TypeError): return {}


_instance = None
_guard = threading.Lock()
def get_package_store():
    global _instance
    with _guard:
        if _instance is None: _instance = PackageStore()
        return _instance
