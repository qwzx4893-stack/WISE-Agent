"""Compatibility facade for WISE's canonical persistent memory service.

Legacy callers use add/search; all entry points now share the same ledger.
Existing Chroma databases are left untouched rather than silently deleted.
"""
from pathlib import Path
from .memory_service import MemoryService, get_memory_service


class Memory:
    def __init__(self, db_path: str = None):
        self.service = (
            MemoryService(storage_path=Path(db_path) / "canonical_memory.json")
            if db_path else get_memory_service()
        )

    def add(self, key: str, value: str):
        return self.service.store(key=key, content=value)

    def search(self, query: str, k: int = 3) -> list:
        return [item.content for item in self.service.query(query, limit=k).items]
