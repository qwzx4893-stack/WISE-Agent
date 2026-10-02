"""Fallback search engine when Meilisearch is not available."""
from typing import List, Dict, Any

class MeiliSearchEngine:
    def __init__(self, host: str = "http://localhost:7700", api_key: str = None):
        self.host = host
        self.api_key = api_key

    async def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        return []

    async def add_documents(self, documents: List[Dict[str, Any]]) -> bool:
        return True

class SearchFallback:
    def __init__(self, db_path: str = None):
        self.db_path = db_path

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        return []
