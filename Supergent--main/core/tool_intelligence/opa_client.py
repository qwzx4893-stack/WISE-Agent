import json
import hashlib
import asyncio
import sqlite3
import time
import httpx
from typing import Dict, Any, Optional
from cachetools import TTLCache
from .observability import get_logger

class CircuitBreaker:
    def __init__(self, failure_threshold=3, recovery_timeout=30, success_threshold=2):
        self.failure_count = 0
        self.success_count = 0
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.recovery_timeout = recovery_timeout
        self.last_failure_time = 0
        self.state = "CLOSED"
        self._lock = asyncio.Lock()

    async def call(self, func, *args, **kwargs):
        async with self._lock:
            if self.state == "OPEN":
                if time.time() - self.last_failure_time > self.recovery_timeout:
                    self.state = "HALF_OPEN"
                    self.success_count = 0
                else:
                    raise Exception("Circuit breaker is OPEN")

        try:
            result = await func(*args, **kwargs)
            async with self._lock:
                if self.state == "HALF_OPEN":
                    self.success_count += 1
                    if self.success_count >= self.success_threshold:
                        self.state = "CLOSED"
                        self.failure_count = 0
            return result
        except Exception as e:
            async with self._lock:
                self.failure_count += 1
                self.last_failure_time = time.time()
                if self.failure_count >= self.failure_threshold or self.state == "HALF_OPEN":
                    self.state = "OPEN"
            raise e

class OPAClient:
    def __init__(self, base_url: str = "http://localhost:8181", timeout: float = 5.0, cache_ttl: int = 300, db_path: str = None):
        from ..paths import MEMORY_DIR
        self.base_url = base_url
        self.timeout = timeout
        self.logger = get_logger()
        self.client = httpx.AsyncClient(timeout=timeout)
        self.cache = TTLCache(maxsize=2000, ttl=cache_ttl)
        self.circuit_breaker = CircuitBreaker()
        if db_path is None:
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            db_path = str(MEMORY_DIR / "opa_cache.db")
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS policy_cache (
                key TEXT PRIMARY KEY,
                value TEXT,
                created_at REAL
            )
        """)
        conn.commit()
        conn.close()

    def _cache_key(self, query: str, input_data: Dict) -> str:
        raw = f"{query}:{json.dumps(input_data, sort_keys=True)}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _load_from_db(self, key: str) -> Optional[Any]:
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute("SELECT value FROM policy_cache WHERE key = ?", (key,))
            row = cursor.fetchone()
            conn.close()
            if row:
                return json.loads(row[0])
        except:
            pass
        return None

    def _save_to_db(self, key: str, value: Any):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("INSERT OR REPLACE INTO policy_cache (key, value, created_at) VALUES (?, ?, ?)", (key, json.dumps(value), time.time()))
            conn.commit()
            conn.close()
        except:
            pass

    async def evaluate(self, query: str, input_data: Dict[str, Any]) -> Optional[Any]:
        key = self._cache_key(query, input_data)

        cached = self.cache.get(key)
        if cached is not None:
            return cached

        db_cached = self._load_from_db(key)
        if db_cached is not None:
            self.cache[key] = db_cached
            return db_cached

        async def do_request():
            resp = await self.client.post(
                f"{self.base_url}/v1/data/{query}",
                json={"input": input_data},
                headers={"Content-Type": "application/json"}
            )
            resp.raise_for_status()
            return resp.json().get("result")

        for attempt in range(3):
            try:
                result = await self.circuit_breaker.call(do_request)
                self.cache[key] = result
                self._save_to_db(key, result)
                return result
            except Exception as e:
                self.logger.warning(f"OPA request failed (attempt {attempt+1})", error=str(e))
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                else:
                    raise Exception(f"OPA evaluation failed: {e}")

        return False

    async def allow(self, input_data: Dict) -> bool:
        res = await self.evaluate("agent/tool/allow", input_data)
        return res is True

    async def is_dangerous_task(self, input_data: Dict) -> bool:
        res = await self.evaluate("agent/tool/is_dangerous_task", input_data)
        return res is True
