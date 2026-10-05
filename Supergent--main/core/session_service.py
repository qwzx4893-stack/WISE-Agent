"""
Canonical Unified Session Service for WISE.

Provides authoritative session lifecycle management, persistence, and correlation
across Desktop, API, Voice, Scheduler, and WebSocket endpoints.
Ensures every subsystem shares coherent SessionContext instances.
"""

from __future__ import annotations

import json
import copy
import hashlib
import logging
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.contracts import SessionContext
from core.paths import SESSIONS_DIR
from core.durable_io import storage_writer_lock

LOG = logging.getLogger("WISE.Session.Service")


class SessionPersistenceError(OSError):
    """A session update was not acknowledged by durable storage."""


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
        # Keep established valid filenames, but never alias a/b to ab or let
        # Windows device names / oversized names escape the identity boundary.
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        if safe_id != session_id or not safe_id or len(safe_id) > 120 or safe_id.upper() in reserved:
            safe_id = f"session_{hashlib.sha256(session_id.encode('utf-8')).hexdigest()}"
        return self.storage_dir / f"{safe_id}.json"

    @contextmanager
    def _disk_lock(self):
        """Serialize writers across instances and processes, with a bounded wait.

        The lock file contains no user data. OS locks are released on process
        death; it is deliberately not unlinked, avoiding the inode-lock race.
        """
        with storage_writer_lock(self.storage_dir, lock_name=".sessions.lock"):
            yield

    @staticmethod
    def _validate_data(data: Any, fallback_id: str) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("session must be an object")
        sid = data.get("session_id") or fallback_id
        if not isinstance(sid, str) or not sid.strip():
            raise ValueError("invalid session identity")
        result = dict(data, session_id=sid)
        for name in ("history", "working_items", "active_tasks", "artifacts", "memory_context", "voice_turns"):
            value = data.get(name, data.get("messages", []) if name == "history" else [])
            if not isinstance(value, list):
                raise ValueError(f"session {name} must be a list")
            result[name] = value
        for entry in result["history"]:
            if not isinstance(entry, dict) or entry.get("role") not in {"user", "assistant", "system", "tool"} or not isinstance(entry.get("content"), str):
                raise ValueError("invalid session message")
            for identity_name in ("message_id", "turn_id"):
                if entry.get(identity_name) is not None and not isinstance(entry[identity_name], str):
                    raise ValueError("invalid message identity")
            if "timestamp" in entry and (isinstance(entry["timestamp"], bool) or not isinstance(entry["timestamp"], (int, float)) or not math.isfinite(entry["timestamp"])):
                raise ValueError("invalid message timestamp")
            if "metadata" in entry and not isinstance(entry["metadata"], dict):
                raise ValueError("invalid message metadata")
        for name in ("hitl_state", "metadata", "model_context"):
            if not isinstance(data.get(name, {}), dict):
                raise ValueError(f"session {name} must be an object")
        for name in ("created_at", "updated_at"):
            value = data.get(name, time.time())
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"invalid session {name}")
        for name in ("last_query", "last_response"):
            if not isinstance(data.get(name, ""), str):
                raise ValueError(f"invalid session {name}")
        return result

    def _load_file(self, p: Path, expected_id: Optional[str] = None) -> Optional[SessionContext]:
        for candidate in (p, p.with_suffix(".json.bak")):
            if not candidate.exists():
                continue
            ctx = self._load_candidate(candidate, p.stem)
            if ctx is not None and (expected_id is None or ctx.session_id == expected_id):
                if candidate != p:
                    LOG.warning("Recovered session state from backup: %s", p.name)
                    ctx.metadata["storage_recovery"] = {"source": "backup", "requires_review": True}
                return ctx
        return None

    def _load_candidate(self, p: Path, fallback_id: str) -> Optional[SessionContext]:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = self._validate_data(json.load(f), fallback_id)
            sid = data["session_id"]
            return SessionContext(
                session_id=sid,
                history=data["history"],
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
            paths = set(self.storage_dir.glob("*.json"))
            paths.update(p.with_suffix("") for p in self.storage_dir.glob("*.json.bak"))
            for p in paths:
                ctx = self._load_file(p)
                if ctx:
                    with self._lock:
                        self._sessions[ctx.session_id] = ctx
        except Exception as e:
            LOG.warning("Error reading sessions directory %s: %s", self.storage_dir, e)

    def save_session(self, session: SessionContext) -> None:
        """Acknowledge only an atomic, fsynced write; propagate failure."""
        tmp = None
        try:
            with self._lock, self._disk_lock():
                p = self._get_path(session.session_id)
                data = self._validate_data(session.to_dict(), session.session_id)
                content = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False)
                tmp = p.parent / f".{p.name}.{uuid.uuid4().hex}.tmp"
                with tmp.open("w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                # Never replace a good backup with a corrupt primary.
                previous = self._load_candidate(p, p.stem) if p.exists() else None
                if previous is not None and previous.session_id == session.session_id:
                    backup_tmp = p.parent / f".{p.name}.{uuid.uuid4().hex}.bak.tmp"
                    try:
                        with backup_tmp.open("wb") as f:
                            f.write(p.read_bytes())
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(backup_tmp, p.with_suffix(".json.bak"))
                    finally:
                        backup_tmp.unlink(missing_ok=True)
                os.replace(tmp, p)
        except Exception as ex:
            LOG.error("Failed to persist session %s: %s", session.session_id, ex)
            raise SessionPersistenceError(f"Could not persist session {session.session_id}") from ex
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

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
        if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
            raise ValueError("Messages must be a list of objects")
        # Preflight serialization before any in-memory mutation.
        json.dumps(messages, ensure_ascii=False, allow_nan=False)
        with self._lock, self._disk_lock():
            session = self.get_or_create_session(session_id)
            disk = self._load_file(self._get_path(session.session_id), session.session_id)
            if disk is not None:
                session.__dict__.update(disk.__dict__)
            before = copy.deepcopy(session.__dict__)
            identities = {
                ("message", entry["message_id"]) if entry.get("message_id") else ("turn", entry["turn_id"], entry["role"])
                for entry in session.history if entry.get("message_id") or (entry.get("turn_id") and entry.get("role") in {"user", "assistant"})
            }
            for message in messages:
                role = str(message.get("role") or "assistant")
                content = str(message.get("content") or "")
                if role not in {"user", "assistant", "system", "tool"} or not content:
                    continue
                identity = (("message", str(message["message_id"])) if message.get("message_id")
                            else ("turn", str(message["turn_id"]), role) if message.get("turn_id") and role in {"user", "assistant"} else None)
                if identity is not None and identity in identities:
                    continue
                if identity is not None:
                    identities.add(identity)
                entry: Dict[str, Any] = {"role": role, "content": content}
                for key in ("modality", "timestamp", "attachments", "metadata", "model_name", "message_id", "turn_id"):
                    if key in message:
                        if key in {"message_id", "turn_id"} and not message[key]:
                            continue
                        entry[key] = str(message[key]) if key in {"message_id", "turn_id"} else copy.deepcopy(message[key])
                entry.setdefault("timestamp", time.time())
                session.history.append(entry)
                if role == "user":
                    session.last_query = content
                    if not session.metadata.get("title"):
                        session.metadata["title"] = self._clean_title(content) or "Session"
                elif role == "assistant":
                    session.last_response = content
            session.updated_at = time.time()
            try:
                self.save_session(session)
            except Exception:
                session.__dict__.clear()
                session.__dict__.update(before)
                raise
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
        with self._lock, self._disk_lock():
            session = self.get_session(session_id)
            if session is None:
                return None
            before = copy.deepcopy(session.__dict__)
            if title is not None:
                clean_title = self._clean_title(title)
                if not clean_title:
                    raise ValueError("Session title cannot be empty")
                session.metadata["title"] = clean_title
            if archived is not None:
                session.metadata["archived"] = bool(archived)
                session.metadata["archived_at"] = time.time() if archived else None
            session.updated_at = time.time()
            try:
                self.save_session(session)
            except Exception:
                session.__dict__.clear()
                session.__dict__.update(before)
                raise
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

        with self._lock, self._disk_lock():
            # Check in-memory
            if sid in self._sessions:
                sess = self._sessions[sid]
                # Cached objects retain identity for active callers, but the
                # file remains authoritative across cooperating writers.
                # Never overwrite another process's newer messages while
                # saving metadata, or repair corrupt storage from stale RAM.
                p = self._get_path(sid)
                if p.exists() or p.with_suffix(".json.bak").exists():
                    disk_sess = self._load_file(p, sid)
                    if disk_sess is None:
                        raise SessionPersistenceError("Existing session state is unreadable; restore or explicitly remove it before updating")
                    sess.__dict__.update(disk_sess.__dict__)
                else:
                    self._sessions.pop(sid)
                    return self.get_or_create_session(sid, metadata)
                if metadata:
                    before = copy.deepcopy(sess.__dict__)
                    sess.metadata.update(metadata)
                    try:
                        self.save_session(sess)
                    except Exception:
                        sess.__dict__.clear()
                        sess.__dict__.update(before)
                        raise
                return sess

            # Check disk
            p = self._get_path(sid)
            if p.exists() or p.with_suffix(".json.bak").exists():
                disk_sess = self._load_file(p, sid)
                if disk_sess:
                    if metadata:
                        disk_sess.metadata.update(metadata)
                        self.save_session(disk_sess)
                    self._sessions[sid] = disk_sess
                    return disk_sess
                raise SessionPersistenceError("Existing session state is unreadable; restore or explicitly remove it before creating a replacement")

            # Create new canonical session
            sess = SessionContext(
                session_id=sid,
                metadata=copy.deepcopy(metadata or {}),
                created_at=time.time(),
                updated_at=time.time(),
            )
            self.save_session(sess)
            self._sessions[sid] = sess
            LOG.info("Created canonical session: %s", sid)
            return sess

    def get_session(self, session_id: str) -> Optional[SessionContext]:
        with self._lock:
            # Always check disk if missing or if disk file has newer content
            p = self._get_path(session_id)
            if p.exists() or p.with_suffix(".json.bak").exists():
                disk_sess = self._load_file(p, session_id)
                if disk_sess:
                    current = self._sessions.get(session_id)
                    if current is not None:
                        current.__dict__.update(disk_sess.__dict__)
                        return current
                    self._sessions[session_id] = disk_sess
                    return disk_sess
                raise SessionPersistenceError("Existing session state is unreadable; restore or explicitly remove it before updating")
            # An external delete must not be resurrected from the cache.
            self._sessions.pop(session_id, None)
            return None

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
        with self._lock, self._disk_lock():
            p = self._get_path(session_id)
            # Explicit deletion must work for corrupt state too, so do not
            # require successfully loading the file first.
            sess = self._sessions.get(session_id)
            exists = p.exists() or p.with_suffix(".json.bak").exists()
            if sess or exists:
                try:
                    # Remove the recovery copy first. If deleting the primary
                    # then fails, the original remains visible instead of an
                    # old backup silently resurrecting a deleted timeline.
                    p.with_suffix(".json.bak").unlink(missing_ok=True)
                    if p.exists():
                        p.unlink()
                except Exception as ex:
                    LOG.warning("Could not delete session file for %s: %s", session_id, ex)
                    raise SessionPersistenceError("Could not delete session") from ex
                self._sessions.pop(session_id, None)
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
