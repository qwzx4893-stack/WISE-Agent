"""Local integration lifecycle with provider-verified ownership, not UI assertions.

No provider credential, Nango connect token or connect URL is stored in SQLite.
This is a single local WISE installation identity, not multi-tenant web hosting.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit
import uuid
import webbrowser

from .provider import IntegrationError, NangoProvider

_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}")
READ_TOOLS = {
    "search_repositories": "Search GitHub repositories",
    "list_repositories": "List repositories accessible to the connected GitHub account",
    "read_file": "Read a GitHub repository file",
}


class IntegrationService:
    def __init__(self, provider=None, database=None, opener=None, clock=time.time):
        from core.paths import MEMORY_DIR
        self.provider = provider or NangoProvider()
        self.database = Path(database or MEMORY_DIR / "integrations.sqlite3")
        self.opener = opener or webbrowser.open
        self.clock = clock
        self.lock = threading.RLock()
        self.links = {}  # short-lived backend memory only
        self.cached = None
        self.cached_at = 0
        self.metadata = json.loads(Path(__file__).with_name("metadata.json").read_text(encoding="utf-8"))
        self.public_catalog = json.loads(Path(__file__).with_name("provider_catalog.json").read_text(encoding="utf-8"))
        self.catalog_warning = ""
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS installation (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS connections (
                  id TEXT PRIMARY KEY, remote TEXT, integration TEXT, provider TEXT,
                  label TEXT, status TEXT, verified REAL, UNIQUE(remote,integration));
                CREATE TABLE IF NOT EXISTS attempts (
                  id TEXT PRIMARY KEY, integration TEXT, provider TEXT, connection TEXT,
                  status TEXT, expires REAL, account TEXT);
            """)
            row = db.execute("SELECT id FROM installation LIMIT 1").fetchone()
            self.owner = row[0] if row else uuid.uuid4().hex
            if not row: db.execute("INSERT INTO installation VALUES (?)", (self.owner,))

    @contextmanager
    def db(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection: yield connection
        finally: connection.close()

    @property
    def configured(self): return bool(self.provider.configured)

    def _rows(self, table):
        if table not in {"connections", "attempts"}: raise ValueError("Invalid table")
        with self.db() as db: return [dict(row) for row in db.execute("SELECT * FROM " + table)]

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not _ID.fullmatch(value): raise IntegrationError("Invalid integration identifier", 400)
        return value

    def _logo(self, url):
        parsed = urlsplit(str(url or ""))
        allowed = {"https://app.nango.dev", "https://nango.dev"}
        allowed.update(filter(None, os.getenv("NANGO_LOGO_ALLOWED_ORIGINS", "").split(",")))
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in allowed or parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment: return ""
        return parsed.geturl()

    def _public_entries(self):
        """Offline display metadata, never proof of configured OAuth or tools."""
        revision = self.public_catalog["revision"]
        if not re.fullmatch(r"[a-f0-9]{40}", revision): raise IntegrationError("Invalid bundled provider revision")
        assets = Path(__file__).resolve().parents[2] / "ui" / "wise_web" / "assets" / "integrations"
        entries = []
        for provider, name, categories, mode, docs, logo_file in self.public_catalog["providers"]:
            self._identifier(provider)
            logo = ""
            if re.fullmatch(r"[a-zA-Z0-9_-]+\.(svg|png|webp)", logo_file):
                logo = "/app/assets/integrations/" + logo_file if (assets / logo_file).is_file() else f"https://raw.githubusercontent.com/NangoHQ/nango/{revision}/packages/webapp/public/images/template-logos/{logo_file}"
            extra = self.metadata.get(provider, {})
            entries.append({"id": "catalog_" + provider, "provider": provider, "name": name,
                "logo": logo, "description": str(extra.get("description") or "")[:500],
                "categories": categories, "auth_mode": mode, "docs_url": docs,
                "tools": [], "catalog_only": True, "can_connect": False,
                "availability": "setup_required"})
        return entries

    def _catalog(self, refresh=False):
        with self.lock:
            if self.cached is not None and not refresh and self.clock() - self.cached_at < 300: return self.cached
            public = self._public_entries()
            self.catalog_warning = ""
            if not self.configured:
                self.cached = public
                self.cached_at = self.clock()
                return public
            try:
                integrations, providers = self.provider.catalog()
            except IntegrationError as error:
                # Public library remains browsable while account authorization
                # is unavailable. Do not fabricate configured/connected apps.
                self.catalog_warning = str(error)
                self.cached = public
                self.cached_at = self.clock()
                return public
            if not isinstance(integrations, list) or not isinstance(providers, list): raise IntegrationError("Invalid integration catalog")
            by_provider = {row["name"]: row for row in providers if isinstance(row, dict) and isinstance(row.get("name"), str)}
            bundled = {row["provider"]: row for row in public}
            configured_providers = set()
            catalog = []
            for row in integrations[:2000]:
                identifier = self._identifier(row.get("unique_key"))
                provider = row.get("provider", "")
                self._identifier(provider)
                configured_providers.add(provider)
                info = by_provider.get(provider, {})
                fallback = bundled.get(provider, {})
                extra = self.metadata.get(provider, {})
                categories = info.get("categories") or fallback.get("categories") or [extra.get("category", "Other")]
                catalog.append({"id": identifier, "provider": provider,
                    "name": str(row.get("display_name") or info.get("display_name") or fallback.get("name") or provider)[:160],
                    "logo": fallback.get("logo") or self._logo(row.get("logo")) or self._logo(info.get("logo_url")),
                    "description": str(extra.get("description") or info.get("description") or "Authorize this service to manage its connection in WISE.")[:500],
                    "categories": [str(value)[:80] for value in categories[:10]],
                    "auth_mode": str(info.get("auth_mode") or fallback.get("auth_mode") or "")[:50],
                    "docs_url": fallback.get("docs_url", ""), "catalog_only": False,
                    "can_connect": True, "availability": "configured",
                    "tools": list(READ_TOOLS.values()) if provider == "github" else []})
            # Nango integration IDs are user-defined. Keep catalog entries
            # unambiguous even if an environment key uses our catalog prefix.
            taken = {row["id"] for row in catalog}
            for row in public:
                if row["provider"] in configured_providers: continue
                base_id = row["id"]
                counter = 0
                while row["id"] in taken:
                    counter += 1
                    row["id"] = "catalog_" + hashlib.sha256((base_id + str(counter)).encode()).hexdigest()[:32]
                taken.add(row["id"])
                catalog.append(row)
            self.cached = sorted(catalog, key=lambda row: row["name"].casefold())
            self.cached_at = self.clock()
            return self.cached

    def catalog(self, query="", category="", connected=False, offset=0, limit=24, refresh=False):
        entries = self._catalog(refresh)
        try:
            accounts = self.accounts(refresh=refresh)
        except IntegrationError as error:
            self.catalog_warning = str(error)
            accounts = [{key: row[key] for key in ("id", "integration", "provider", "label")} | {"status": "UNVERIFIED"} for row in self._rows("connections")]
        result = [{**row, "accounts": [account for account in accounts if account["integration"] == row["id"]]} for row in entries]
        categories = sorted({value for row in entries for value in row["categories"]})
        result = [row for row in result if (not query or query.casefold() in (row["name"] + " " + row["description"] + " " + row["provider"] + " " + row["id"]).casefold())
                  and (not category or category in row["categories"]) and (not connected or any(a["status"] == "CONNECTED" for a in row["accounts"]))]
        offset = max(0, offset); limit = max(1, min(limit, 48))
        return {"configured": self.configured, "items": result[offset:offset + limit], "categories": categories,
                "total": len(result), "next_offset": offset + limit if len(result) > offset + limit else None,
                "catalog_source": self.public_catalog["source"], "catalog_revision": self.public_catalog["revision"],
                "catalog_generated_at": self.public_catalog["generated_at"], "warning": self.catalog_warning}

    def detail(self, identifier):
        self._identifier(identifier)
        item = next((row for row in self._catalog() if row["id"] == identifier), None)
        if item is None: raise IntegrationError("This integration was not found in the provider catalog", 404)
        return {**item, "accounts": [row for row in self.accounts() if row["integration"] == identifier]}

    def _account(self, identifier):
        self._identifier(identifier)
        row = next((row for row in self._rows("connections") if row["id"] == identifier), None)
        if not row: raise IntegrationError("Connected account was not found", 404)
        return row

    def _owned_remote(self, account, attempt=None):
        rows = self.provider.connections(self.owner, attempt=attempt, connection=account["remote"])
        return next((row for row in rows if row.get("connection_id") == account["remote"]
            and row.get("provider_config_key") == account["integration"] and row.get("provider") == account["provider"]
            and row.get("tags", {}).get("end_user_id") == self.owner
            and (not attempt or row.get("tags", {}).get("wise_attempt_id") == attempt)), None)

    def accounts(self, refresh=False):
        rows = self._rows("connections")
        if self.configured:
            stale = any(refresh or self.clock() - row["verified"] > 60 for row in rows)
            remote_rows = self.provider.connections(self.owner) if stale else []
            for row in rows:
                if refresh or self.clock() - row["verified"] > 60:
                    remote = next((item for item in remote_rows if item.get("connection_id") == row["remote"]
                        and item.get("provider_config_key") == row["integration"] and item.get("provider") == row["provider"]
                        and item.get("tags", {}).get("end_user_id") == self.owner), None)
                    status = "DISCONNECTED" if remote is None else "REAUTH_REQUIRED" if remote.get("errors") else "CONNECTED"
                    with self.db() as db: db.execute("UPDATE connections SET status=?, verified=? WHERE id=?", (status, self.clock(), row["id"]))
                    row["status"] = status
        return [{key: row[key] for key in ("id", "integration", "provider", "label", "status")} for row in rows]

    def pending(self):
        rows = self._rows("attempts")
        for row in rows:
            if row["expires"] <= self.clock(): self.links.pop(row["id"], None)
        return [{key: row[key] for key in ("id", "integration", "expires")} for row in rows
                if row["status"] == "PENDING" and row["expires"] > self.clock()]

    def connect(self, integration, account=None):
        with self.lock:
            self.pending()  # Evict expired authorization links from backend memory.
            with self.db() as db: db.execute("DELETE FROM attempts WHERE expires < ?", (self.clock() - 604800,))
            item = self.detail(integration)
            if not item["can_connect"]:
                raise IntegrationError("Configure this integration in the Nango backend before connecting an account", 503)
            pending = [row for row in self._rows("attempts") if row["status"] == "PENDING" and row["expires"] > self.clock()]
            if len(pending) >= 5 or any(row["integration"] == integration for row in pending):
                raise IntegrationError("An authorization attempt is already pending; cancel it first", 409)
            if len(self._rows("connections")) >= 100: raise IntegrationError("Connected account limit reached", 409)
            previous = self._account(account) if account else None
            if previous and previous["integration"] != integration: raise IntegrationError("Account belongs to another integration", 400)
            attempt = uuid.uuid4().hex
            session = self.provider.connect_session(integration, {"end_user_id": self.owner, "wise_attempt_id": attempt},
                                                    previous["remote"] if previous else None)
            url = str(session.get("connect_link", ""))
            parsed = urlsplit(url)
            allowed = {"https://connect.nango.dev"}
            allowed.update(filter(None, os.getenv("NANGO_CONNECT_ALLOWED_ORIGINS", "").split(",")))
            if f"{parsed.scheme}://{parsed.netloc}" not in allowed or parsed.username or parsed.password or parsed.fragment or parsed.scheme != "https":
                raise IntegrationError("The authorization link is not on an allowed HTTPS origin")
            try: expires = datetime.fromisoformat(session["expires_at"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError, TypeError): raise IntegrationError("Invalid authorization expiry") from None
            if not self.clock() < expires <= self.clock() + 3600: raise IntegrationError("Authorization session is expired or invalid")
            with self.db() as db:
                db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?)", (attempt, integration, item["provider"],
                    previous["remote"] if previous else None, "PENDING", expires, None))
            self.links[attempt] = url
            result = self.launch(attempt)
            return {"id": attempt, "status": "PENDING", "expires_at": expires, "browser_opened": result}

    def launch(self, attempt):
        row = self._attempt(attempt)
        if row["status"] != "PENDING" or row["expires"] <= self.clock() or attempt not in self.links:
            raise IntegrationError("Authorization link expired; start a new connection", 409)
        try: return bool(self.opener(self.links[attempt], new=2))
        except Exception: return False

    def _attempt(self, identifier):
        self._identifier(identifier)
        with self.db() as db: row = db.execute("SELECT * FROM attempts WHERE id=?", (identifier,)).fetchone()
        if row is None: raise IntegrationError("Authorization attempt was not found", 404)
        return dict(row)

    def poll(self, attempt):
        with self.lock:
            row = self._attempt(attempt)
            if row["status"] == "PENDING":
                if row["expires"] <= self.clock():
                    row["status"] = "EXPIRED"
                else:
                    candidates = self.provider.connections(self.owner, attempt=attempt, connection=row["connection"])
                    verified = next((item for item in candidates if item.get("provider_config_key") == row["integration"]
                        and item.get("provider") == row["provider"] and item.get("tags", {}).get("end_user_id") == self.owner
                        and item.get("tags", {}).get("wise_attempt_id") == attempt
                        and (not row["connection"] or item.get("connection_id") == row["connection"])), None)
                    if verified:
                        remote = verified.get("connection_id")
                        if not isinstance(remote, str) or not 0 < len(remote) <= 256: raise IntegrationError("Invalid remote connection")
                        row["status"] = "FAILED" if verified.get("errors") else "CONNECTED"
                        if row["status"] == "CONNECTED":
                            with self.db() as db:
                                existing = db.execute("SELECT id FROM connections WHERE remote=? AND integration=?", (remote, row["integration"])).fetchone()
                                account = existing[0] if existing else uuid.uuid4().hex
                                # Only explicit safe profile fields, never entire metadata/credentials.
                                metadata = verified.get("metadata") or {}
                                label = str(metadata.get("username") or metadata.get("email") or row["integration"])[:160]
                                db.execute("INSERT OR REPLACE INTO connections VALUES (?,?,?,?,?,?,?)", (account, remote,
                                    row["integration"], row["provider"], label, "CONNECTED", self.clock()))
                                row["account"] = account
                with self.db() as db: db.execute("UPDATE attempts SET status=?, account=? WHERE id=?", (row["status"], row["account"], attempt))
            if row["status"] != "PENDING": self.links.pop(attempt, None)
            return {"id": attempt, "status": row["status"], "account": row["account"], "expires_at": row["expires"]}

    def cancel(self, attempt):
        with self.lock:
            row = self._attempt(attempt)
            if row["status"] == "PENDING":
                with self.db() as db: db.execute("UPDATE attempts SET status='CANCELLED' WHERE id=?", (attempt,))
            self.links.pop(attempt, None)
            return self.poll(attempt)

    def disconnect(self, account):
        with self.lock:
            row = self._account(account)
            remote = self._owned_remote(row)
            if remote is not None: self.provider.disconnect(row["integration"], row["remote"])
            with self.db() as db: db.execute("DELETE FROM connections WHERE id=?", (account,))
            return {"disconnected": True, "account": account}

    def execute_read(self, operation, args):
        if operation not in READ_TOOLS: raise IntegrationError("No reviewed tool adapter for this operation", 400)
        row = self._account(args.get("account"))
        if row["provider"] != "github": raise IntegrationError("This tool requires a connected GitHub account", 400)
        remote = self._owned_remote(row)
        if remote is None or remote.get("errors"):
            with self.db() as db: db.execute("UPDATE connections SET status='REAUTH_REQUIRED', verified=? WHERE id=?", (self.clock(), row["id"]))
            raise IntegrationError("This account requires reconnection", 409)
        if operation == "search_repositories":
            query = args.get("query")
            if not isinstance(query, str) or not 1 <= len(query) <= 300: raise IntegrationError("A bounded repository query is required", 400)
            result = self.provider.read(row["integration"], row["remote"], "/search/repositories", {"q": query, "per_page": 10})
            records = result.get("items", [])
        elif operation == "list_repositories":
            records = self.provider.read(row["integration"], row["remote"], "/user/repos", {"per_page": 20, "sort": "updated"})
        else:
            owner, repo, path = args.get("owner"), args.get("repo"), args.get("path")
            if not all(isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", value) and value not in {".", ".."} for value in (owner, repo)):
                raise IntegrationError("Invalid repository owner or name", 400)
            if not isinstance(path, str) or not 1 <= len(path) <= 400 or any(not re.fullmatch(r"[a-zA-Z0-9_. -]{1,100}", part) or part in {".", ".."} for part in path.split("/")):
                raise IntegrationError("Invalid repository-relative file path", 400)
            from urllib.parse import quote
            result = self.provider.read(row["integration"], row["remote"], f"/repos/{owner}/{repo}/contents/{quote(path, safe='/')}", {})
            if not isinstance(result, dict) or result.get("type") != "file" or result.get("size", 1_000_001) > 200_000:
                raise IntegrationError("Only files up to 200 KB can be read", 400)
            return {"success": True, "trust": "UNTRUSTED_EXTERNAL", "file": {key: result.get(key) for key in ("name", "path", "sha", "encoding", "content", "html_url")}}
        if not isinstance(records, list): raise IntegrationError("Invalid repository response")
        return {"success": True, "trust": "UNTRUSTED_EXTERNAL", "repositories": [
            {key: item.get(key) for key in ("full_name", "description", "html_url", "private", "default_branch")} for item in records[:20]]}


_service = None
_service_key = None
_service_lock = threading.RLock()


def get_integration_service():
    from core.paths import MEMORY_DIR
    global _service, _service_key
    fingerprint = (str(MEMORY_DIR), hashlib.sha256(os.getenv("NANGO_SECRET_KEY", "").encode()).hexdigest(), os.getenv("NANGO_BASE_URL", ""))
    with _service_lock:
        if _service is None or fingerprint != _service_key or not _service.database.exists():
            _service = IntegrationService()
            _service_key = fingerprint
        return _service
