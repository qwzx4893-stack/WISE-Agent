"""
Canonical Unified Session Service for WISE.

Provides authoritative session lifecycle management, persistence, and correlation
across Desktop, API, Voice, Scheduler, and WebSocket endpoints.
Ensures every subsystem shares coherent SessionContext instances.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.contracts import SessionContext
from core.paths import SESSIONS_DIR

LOG = logging.getLogger("WISE.Session.Service")


class SessionService:
    """
    Authoritative Session Service for WISE.
    Maintains active SessionContext states with thread-safe file persistence.
    """

    def __init__(self, storage_dir: Optional[Path] = None) -> None:
        self.storage_dir = storage_dir or SESSIONS_DIR
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            LOG.warning("Could not create sessions directory %s: %s", self.storage_dir, e)
        self._sessions: Dict[str, SessionContext] = {}
        self._lock = threading.RLock()
        self._load_all()

    def _get_path(self, session_id: str) -> Path:
        safe_id = "".join(c for c in session_id if c.isalnum() or c in ("-", "_")).strip()
        if not safe_id:
            safe_id = "default_session"
        return self.storage_dir / f"{safe_id}.json"

    def _load_file(self, p: Path) -> Optional[SessionContext]:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            sid = data.get("session_id") or p.stem
            return SessionContext(
                session_id=sid,
                history=data.get("history") or data.get("messages") or [],
                working_items=data.get("working_items", []),
                active_tasks=data.get("active_tasks", []),
                artifacts=data.get("artifacts", []),
                memory_context=data.get("memory_context", []),
                voice_turns=data.get("voice_turns", []),
                hitl_state=data.get("hitl_state", {}),
                metadata=data.get("metadata", {}),
                model_context=data.get("model_context", {}),
                last_query=data.get("last_query", ""),
                last_response=data.get("last_response", ""),
                created_at=data.get("created_at", time.time()),
                updated_at=data.get("updated_at", time.time()),
            )
        except Exception as ex:
            LOG.debug("Failed loading session from %s: %s", p, ex)
            return None

    def _load_all(self) -> None:
        if not self.storage_dir.exists():
            return
        try:
            for p in self.storage_dir.glob("*.json"):
                ctx = self._load_file(p)
                if ctx:
                    with self._lock:
                        self._sessions[ctx.session_id] = ctx
        except Exception as e:
            LOG.warning("Error reading sessions directory %s: %s", self.storage_dir, e)

    def save_session(self, session: SessionContext) -> None:
        """Persists session state atomically to disk."""
        try:
            p = self._get_path(session.session_id)
            tmp = p.with_suffix(".tmp")
            data = session.to_dict()
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception as ex:
            LOG.error("Failed to persist session %s: %s", session.session_id, ex)

    @staticmethod
    def _clean_title(title: str) -> str:
        """Return a compact, single-line title suitable for a session list."""
        return " ".join((title or "").split())[:120]

    def append_messages(self, session_id: str, messages: List[Dict[str, Any]]) -> SessionContext:
        """Append one or more durable conversation messages as one atomic update.

        Conversation producers must use this method instead of mutating
        ``SessionContext.history`` themselves.  It keeps the timeline, title,
        and recency order correct for every surface (chat, voice, scheduler,
        and the desktop UI).
        """
        with self._lock:
            session = self.get_or_create_session(session_id)
            for message in messages:
                role = str(message.get("role") or "assistant")
                content = str(message.get("content") or "")
                if role not in {"user", "assistant", "system", "tool"} or not content:
                    continue
                entry: Dict[str, Any] = {"role": role, "content": content}
                for key in ("modality", "timestamp", "attachments", "metadata", "model_name"):
                    if key in message:
                        entry[key] = message[key]
                entry.setdefault("timestamp", time.time())
                session.history.append(entry)
                if role == "user":
                    session.last_query = content
                    if not session.metadata.get("title"):
                        session.metadata["title"] = self._clean_title(content) or "Session"
                elif role == "assistant":
                    session.last_response = content
            session.updated_at = time.time()
            self.save_session(session)
            return session

    def add_history_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        modality: Optional[str] = None,
    ) -> SessionContext:
        """Compatibility entry point for voice and external channel adapters."""
        message: Dict[str, Any] = {"role": role, "content": content}
        if modality:
            message["modality"] = modality
        return self.append_messages(session_id, [message])

    def update_session(
        self,
        session_id: str,
        *,
        title: Optional[str] = None,
        archived: Optional[bool] = None,
    ) -> Optional[SessionContext]:
        """Persist user-owned session metadata used by the desktop UI."""
        with self._lock:
            session = self.get_session(session_id)
            if session is None:
                return None
            if title is not None:
                clean_title = self._clean_title(title)
                if not clean_title:
                    raise ValueError("Session title cannot be empty")
                session.metadata["title"] = clean_title
            if archived is not None:
                session.metadata["archived"] = bool(archived)
                session.metadata["archived_at"] = time.time() if archived else None
            session.updated_at = time.time()
            self.save_session(session)
            return session

    def get_or_create_session(
        self,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SessionContext:
        """Retrieves an existing session or creates a new canonical SessionContext."""
        sid = (session_id or "default_session").strip()
        if not sid:
            sid = "default_session"

        with self._lock:
            # Check in-memory
            if sid in self._sessions:
                sess = self._sessions[sid]
                if metadata:
                    sess.metadata.update(metadata)
                return sess

            # Check disk
            p = self._get_path(sid)
            if p.exists():
                disk_sess = self._load_file(p)
                if disk_sess:
                    if metadata:
                        disk_sess.metadata.update(metadata)
                    self._sessions[sid] = disk_sess
                    return disk_sess

            # Create new canonical session
            sess = SessionContext(
                session_id=sid,
                metadata=metadata or {},
                created_at=time.time(),
                updated_at=time.time(),
            )
            self._sessions[sid] = sess
            self.save_session(sess)
            LOG.info("Created canonical session: %s", sid)
            return sess

    def get_session(self, session_id: str) -> Optional[SessionContext]:
        with self._lock:
            # Always check disk if missing or if disk file has newer content
            p = self._get_path(session_id)
            if p.exists():
                disk_sess = self._load_file(p)
                if disk_sess:
                    self._sessions[session_id] = disk_sess
                    return disk_sess
            return self._sessions.get(session_id)

    def list_sessions(self, *, include_archived: bool = False) -> List[SessionContext]:
        with self._lock:
            sessions = list(self._sessions.values())
            if not include_archived:
                sessions = [s for s in sessions if not bool(s.metadata.get("archived"))]
            return sorted(
                sessions,
                key=lambda s: s.updated_at,
                reverse=True,
            )

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            sess = self._sessions.pop(session_id, None)
            if sess:
                try:
                    p = self._get_path(session_id)
                    if p.exists():
                        p.unlink()
                except Exception as ex:
                    LOG.warning("Could not delete session file for %s: %s", session_id, ex)
                return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


# Global Authoritative Singleton
_GLOBAL_SESSION_SERVICE: Optional[SessionService] = None
_SESS_LOCK = threading.Lock()


def get_session_service() -> SessionService:
    global _GLOBAL_SESSION_SERVICE
    if _GLOBAL_SESSION_SERVICE is None:
        with _SESS_LOCK:
            if _GLOBAL_SESSION_SERVICE is None:
                _GLOBAL_SESSION_SERVICE = SessionService()
    return _GLOBAL_SESSION_SERVICE
