"""Persistent plan / answer cache backed by sqlite.

A successful ``Final Answer`` from ``react_loop`` is stored under a key
that mixes:

- the user input,
- the **list of tool names** available at call time,
- the **system overview hash** (so the cache invalidates automatically
  when the agent gains/loses tools or the prompt changes),
- and an optional ``model_name`` so different models don't share answers.

The TTL defaults to 24h. Hits/misses are tracked for ``/optimization/stats``.

This module also exposes ``InMemoryCache`` for tests / ephemeral use.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional


def _hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _normalise_task(task: str) -> str:
    """Make harmless whitespace differences share a cache entry.

    This is deliberately not semantic matching: a guessed semantic cache can
    return an answer for a materially different request. Exact content after
    whitespace normalisation is safe and still captures UI retry/re-send.
    """
    return " ".join((task or "").split())


class RequestCoalescer:
    """Coalesce identical in-flight model calls for one process.

    The leader executes the callable once; concurrent followers wait for its
    completed value. Failures are propagated but never cached. This prevents a
    double-click or two API workers from paying for the same prompt twice.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flights: Dict[str, Dict[str, Any]] = {}
        self.leaders = 0
        self.followers = 0

    def run(self, key: str, call):
        with self._lock:
            flight = self._flights.get(key)
            if flight is None:
                flight = {"event": threading.Event(), "value": None, "error": None}
                self._flights[key] = flight
                leader = True
                self.leaders += 1
            else:
                leader = False
                self.followers += 1
        if not leader:
            flight["event"].wait()
            if flight["error"] is not None:
                raise flight["error"]
            return flight["value"]
        try:
            flight["value"] = call()
            return flight["value"]
        except BaseException as exc:
            flight["error"] = exc
            raise
        finally:
            flight["event"].set()
            with self._lock:
                self._flights.pop(key, None)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "in_flight": len(self._flights),
                "leaders": self.leaders,
                "coalesced_requests": self.followers,
            }


class PlanCache:
    """Sqlite-backed answer cache with TTL + hit/miss metrics."""

    _conn_lock = threading.Lock()

    def __init__(self, db_path: str | None = None,
                 ttl_hours: int | None = None,
                 enabled: bool | None = None):
        if db_path is None:
            from ..paths import MEMORY_DIR

            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            db_path = str(MEMORY_DIR / "plan_cache.db")
        self.db_path = Path(db_path)
        self.ttl = timedelta(
            hours=int(os.environ.get("AGENT_CACHE_TTL_HOURS", str(ttl_hours or 24)))
        )
        env = os.environ.get("AGENT_CACHE_ENABLED", "1").lower()
        self.enabled = env in {"1", "true", "yes"} if enabled is None else enabled

        # Stats kept in-memory; persistent ones come from sqlite COUNT.
        self.hits = 0
        self.misses = 0
        self.writes = 0

        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plan_cache (
                key         TEXT PRIMARY KEY,
                response    TEXT NOT NULL,
                tokens_in   INTEGER DEFAULT 0,
                tokens_out  INTEGER DEFAULT 0,
                created_at  REAL    NOT NULL,
                hits        INTEGER DEFAULT 0
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_created_at ON plan_cache(created_at)"
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Key derivation
    # ------------------------------------------------------------------
    @staticmethod
    def derive_key(task: str, *, tools: Optional[list] = None,
                   overview: str = "", model_name: str = "") -> str:
        tool_sig = "|".join(sorted(tools)) if tools else ""
        return _hash("plan-cache-v2", _normalise_task(task), tool_sig, overview, model_name)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def get(self, task: str, *, context: str = "",
            tools: Optional[list] = None, model_name: str = "",
            overview: str = "") -> Optional[str]:
        if not self.enabled:
            return None
        # Backwards-compat: the older API took a plain ``context`` string.
        if context and not overview:
            overview = context
        key = self.derive_key(task, tools=tools, overview=overview, model_name=model_name)
        with self._conn_lock:
            row = self.conn.execute(
                "SELECT response, created_at, hits FROM plan_cache WHERE key = ?",
                (key,),
            ).fetchone()
        if not row:
            self.misses += 1
            return None
        if datetime.now() - datetime.fromtimestamp(row[1]) > self.ttl:
            with self._conn_lock:
                self.conn.execute("DELETE FROM plan_cache WHERE key = ?", (key,))
                self.conn.commit()
            self.misses += 1
            return None
        with self._conn_lock:
            self.conn.execute(
                "UPDATE plan_cache SET hits = hits + 1 WHERE key = ?", (key,)
            )
            self.conn.commit()
        self.hits += 1
        return row[0]

    def set(self, task: str, response: str, *,
            tools: Optional[list] = None, overview: str = "",
            model_name: str = "", context: str = "",
            tokens_in: int = 0, tokens_out: int = 0) -> None:
        if not self.enabled:
            return
        if context and not overview:
            overview = context
        key = self.derive_key(task, tools=tools, overview=overview, model_name=model_name)
        with self._conn_lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO plan_cache "
                "(key, response, tokens_in, tokens_out, created_at, hits) "
                "VALUES (?, ?, ?, ?, ?, COALESCE("
                "  (SELECT hits FROM plan_cache WHERE key = ?), 0))",
                (key, response, tokens_in, tokens_out, time.time(), key),
            )
            self.conn.commit()
        self.writes += 1

    def cleanup(self) -> int:
        cutoff = (datetime.now() - self.ttl).timestamp()
        with self._conn_lock:
            cur = self.conn.execute(
                "DELETE FROM plan_cache WHERE created_at < ?", (cutoff,)
            )
            self.conn.commit()
            return cur.rowcount or 0

    def stats(self) -> Dict[str, Any]:
        with self._conn_lock:
            row = self.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(hits), 0), "
                "COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0) "
                "FROM plan_cache"
            ).fetchone()
        size, total_hits, t_in, t_out = row or (0, 0, 0, 0)
        return {
            "enabled": self.enabled,
            "ttl_hours": self.ttl.total_seconds() / 3600,
            "size": size,
            "session_hits": self.hits,
            "session_misses": self.misses,
            "session_writes": self.writes,
            "lifetime_hits": total_hits,
            "tokens_in_saved": t_in,
            "tokens_out_saved": t_out,
            "db_path": str(self.db_path),
        }

    def clear(self) -> int:
        with self._conn_lock:
            cur = self.conn.execute("DELETE FROM plan_cache")
            self.conn.commit()
            return cur.rowcount or 0


class InMemoryCache:
    """Lightweight cache for tests."""

    def __init__(self, ttl_hours: int = 24):
        self.ttl = timedelta(hours=ttl_hours)
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0
        self.enabled = True

    def get(self, task: str, *, tools=None, model_name: str = "",
            overview: str = "", context: str = "") -> Optional[str]:
        if context and not overview:
            overview = context
        key = PlanCache.derive_key(
            task, tools=tools, overview=overview, model_name=model_name
        )
        e = self.entries.get(key)
        if not e:
            self.misses += 1
            return None
        if datetime.now() - e["created_at"] > self.ttl:
            del self.entries[key]
            self.misses += 1
            return None
        self.hits += 1
        return e["response"]

    def set(self, task: str, response: str, *,
            tools=None, overview: str = "", model_name: str = "",
            context: str = "", tokens_in: int = 0, tokens_out: int = 0) -> None:
        if context and not overview:
            overview = context
        key = PlanCache.derive_key(
            task, tools=tools, overview=overview, model_name=model_name
        )
        self.entries[key] = {
            "response": response,
            "created_at": datetime.now(),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }


__all__ = ["PlanCache", "InMemoryCache", "RequestCoalescer"]
