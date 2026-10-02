import json
import sqlite3
import asyncio
from pathlib import Path
from typing import Dict, List, Optional
from .manifest import ToolManifest
from .meili_search import MeiliSearchEngine, SearchFallback
from .observability import trace, get_logger

class ToolRegistry:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, tools_dir: str = None, db_path: str = None):
        if self._initialized:
            return
        from ..paths import TOOLS_DIR, MEMORY_DIR
        self.tools_dir = Path(tools_dir) if tools_dir else TOOLS_DIR
        self.tools: Dict[str, Dict[str, ToolManifest]] = {}
        if db_path is None:
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            db_path = str(MEMORY_DIR / "tools.db")
        self.db_path = db_path
        self.logger = get_logger()
        self._init_db()
        self.search_engine = MeiliSearchEngine()
        self.fallback_search = SearchFallback(db_path)
        self._load_all_tools()
        self._initialized = True

    def _init_db(self):
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tools_index (
                name TEXT,
                version TEXT,
                description TEXT,
                use_cases TEXT,
                capabilities TEXT,
                category TEXT,
                PRIMARY KEY (name, version)
            )
        """)
        self.conn.commit()

    @trace
    def _load_all_tools(self):
        for pack_file in (self.tools_dir / "packs").glob("*.json"):
            try:
                with open(pack_file, 'r') as f:
                    tools_data = json.load(f)
                for data in tools_data:
                    manifest = ToolManifest(**data)
                    self.register(manifest)
            except Exception as e:
                print(f"⚠️ Failed to load pack {pack_file}: {e}")

        for json_file in self.tools_dir.glob("*.json"):
            try:
                with open(json_file, 'r') as f:
                    data = json.load(f)
                manifest = ToolManifest(**data)
                self.register(manifest)
            except Exception as e:
                print(f"⚠️ Failed to load {json_file}: {e}")

    def register(self, manifest: ToolManifest) -> ToolManifest:
        name, version = manifest.name, manifest.version
        self.tools.setdefault(name, {})[version] = manifest
        self._index_tool(manifest)
        self._schedule_async_index(manifest)
        return manifest

    def _schedule_async_index(self, manifest: ToolManifest):
        doc = manifest.model_dump()
        doc['id'] = f"{manifest.name}@{manifest.version}"
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.search_engine.add_documents([doc]))
        except RuntimeError:
            import threading
            def run_async():
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                new_loop.run_until_complete(self.search_engine.add_documents([doc]))
                new_loop.close()
            threading.Thread(target=run_async, daemon=True).start()

    def _index_tool(self, manifest: ToolManifest):
        self.conn.execute("DELETE FROM tools_index WHERE name = ? AND version = ?", (manifest.name, manifest.version))
        self.conn.execute(
            "INSERT INTO tools_index (name, version, description, use_cases, capabilities, category) VALUES (?, ?, ?, ?, ?, ?)",
            (manifest.name, manifest.version, manifest.description, " ".join(manifest.use_cases), " ".join(manifest.capabilities), manifest.category.value)
        )
        self.conn.commit()

    def get(self, name: str, version: str = None) -> Optional[ToolManifest]:
        versions = self.tools.get(name, {})
        if not versions:
            return None
        if version:
            return versions.get(version)
        latest = sorted(versions.keys(), reverse=True)[0]
        return versions[latest]

    def list_all(self) -> List[str]:
        return list(self.tools.keys())

    @trace
    async def search(self, query: str, limit: int = 5) -> List[ToolManifest]:
        hits = await self.search_engine.search(query, limit)
        if hits:
            results = []
            for hit in hits:
                name, version = hit['name'], hit['version']
                tool = self.get(name, version)
                if tool:
                    results.append(tool)
            if results:
                return results

        hits = self.fallback_search.search(query, limit)
        results = []
        for hit in hits:
            name, version = hit['name'], hit['version']
            tool = self.get(name, version)
            if tool:
                results.append(tool)
        return results
