# ==============================================================================
# WISE Cognitive Brain - Task Engine & Hierarchical Long-Horizon State
# Architecture: Decouples procedural task progression from environmental World State.
# Manages:
# - First-class Task, Goal, Subgoal, and TaskStep structures
# - Checkpointing at subgoal boundaries and human intervention barriers
# - Clean pause / resume states (PAUSED_FOR_HUMAN) with exact step preservation
# - Anti-TOCTOU confirmation tracking and secret-redacted task persistence
# - Deterministic step-by-step progression and rollback
# ==============================================================================

from __future__ import annotations

import time
import uuid
import logging
import threading
import json
import os
import copy
import math
from enum import Enum
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

from core.hands import ComputerActionType
from core.security.transient_vault import redact_sensitive_payload
from core.paths import CONFIG_DIR

LOG = logging.getLogger("WISE.Brain.TaskEngine")


class TaskPersistenceError(OSError):
    """No durable acknowledgement; caller must not continue executing steps."""


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    PAUSED_FOR_HUMAN = "PAUSED_FOR_HUMAN"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    RECOVERING = "RECOVERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SubgoalStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    RECOVERING = "RECOVERING"


class TaskDomain(str, Enum):
    WINDOWS_OS = "WINDOWS_OS"
    BROWSER = "BROWSER"
    FILESYSTEM = "FILESYSTEM"
    SYSTEM = "SYSTEM"
    MULTI_DOMAIN = "MULTI_DOMAIN"


@dataclass
class TaskCheckpoint:
    checkpoint_id: str = field(default_factory=lambda: f"chk_{uuid.uuid4().hex[:8]}")
    subgoal_id: str = ""
    timestamp: float = field(default_factory=time.time)
    active_window_title: Optional[str] = None
    active_hwnd: Optional[int] = None
    browser_url: Optional[str] = None
    created_artifacts: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "subgoal_id": self.subgoal_id,
            "timestamp": self.timestamp,
            "active_window_title": self.active_window_title,
            "active_hwnd": self.active_hwnd,
            "browser_url": self.browser_url,
            "created_artifacts": list(self.created_artifacts),
            "metadata": redact_sensitive_payload(self.metadata),
        }


@dataclass
class TaskStep:
    step_id: str = field(default_factory=lambda: f"step_{uuid.uuid4().hex[:8]}")
    action_type: ComputerActionType = ComputerActionType.WAIT
    params: Dict[str, Any] = field(default_factory=dict)
    description: str = ""
    verification_spec: Optional[Dict[str, Any]] = None
    security_tier: Optional[str] = None
    max_retries: int = 2
    status: StepStatus = StepStatus.PENDING
    result: Optional[Dict[str, Any]] = None
    is_corrective: bool = False
    requires_human: bool = False
    human_intervention_type: Optional[str] = None
    human_intervention_reason: Optional[str] = None
    human_prompt: Optional[str] = None
    confirmation_token: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "action_type": self.action_type.value,
            "params": redact_sensitive_payload(self.params),
            "description": self.description,
            "verification_spec": self.verification_spec,
            "security_tier": self.security_tier,
            "max_retries": self.max_retries,
            "status": self.status.value,
            "result": redact_sensitive_payload(self.result) if self.result else None,
            "is_corrective": self.is_corrective,
            "requires_human": self.requires_human,
            "human_intervention_type": self.human_intervention_type,
            "human_intervention_reason": self.human_intervention_reason,
            "human_prompt": self.human_prompt,
            "confirmation_token": self.confirmation_token,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


@dataclass
class Subgoal:
    subgoal_id: str = field(default_factory=lambda: f"sg_{uuid.uuid4().hex[:8]}")
    title: str = ""
    description: str = ""
    domain: TaskDomain = TaskDomain.WINDOWS_OS
    steps: List[TaskStep] = field(default_factory=list)
    status: SubgoalStatus = SubgoalStatus.PENDING
    checkpoint: Optional[TaskCheckpoint] = None
    current_step_idx: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subgoal_id": self.subgoal_id,
            "title": self.title,
            "description": self.description,
            "domain": self.domain.value,
            "steps": [s.to_dict() for s in self.steps],
            "status": self.status.value,
            "checkpoint": self.checkpoint.to_dict() if self.checkpoint else None,
            "current_step_idx": self.current_step_idx,
        }


TaskSubgoal = Subgoal


@dataclass
class Task:
    task_id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:10]}")
    session_id: Optional[str] = None
    user_intent: str = ""
    semantic_goal: str = ""
    milestones: List[Any] = field(default_factory=list)
    current_milestone_idx: int = 0
    subgoals: List[Subgoal] = field(default_factory=list)
    current_subgoal_idx: int = 0
    status: TaskStatus = TaskStatus.PENDING
    artifacts: List[Any] = field(default_factory=list)
    checkpoints: List[TaskCheckpoint] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    pause_reason: Optional[str] = None
    active_intervention: Optional[Dict[str, Any]] = None
    context_variables: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def total_steps(self) -> int:
        return sum(len(sg.steps) for sg in self.subgoals)

    @property
    def completed_steps_count(self) -> int:
        return sum(
            1 for sg in self.subgoals for s in sg.steps if s.status == StepStatus.COMPLETED
        )

    def get_current_step(self) -> Optional[TaskStep]:
        if self.current_subgoal_idx < 0 or self.current_subgoal_idx >= len(self.subgoals):
            return None
        sg = self.subgoals[self.current_subgoal_idx]
        if sg.current_step_idx < 0 or sg.current_step_idx >= len(sg.steps):
            return None
        return sg.steps[sg.current_step_idx]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "user_intent": self.user_intent,
            "semantic_goal": self.semantic_goal,
            "milestones": [m.to_dict() if hasattr(m, "to_dict") else m for m in self.milestones],
            "current_milestone_idx": self.current_milestone_idx,
            "subgoals": [sg.to_dict() for sg in self.subgoals],
            "current_subgoal_idx": self.current_subgoal_idx,
            "total_steps": self.total_steps,
            "completed_steps": self.completed_steps_count,
            "status": self.status.value,
            "artifacts": [a.to_dict() if hasattr(a, "to_dict") else a for a in self.artifacts],
            "checkpoints": [c.to_dict() for c in self.checkpoints],
            "error": self.error,
            "pause_reason": self.pause_reason,
            "active_intervention": redact_sensitive_payload(self.active_intervention) if self.active_intervention else None,
            "context_variables": redact_sensitive_payload(self.context_variables),
            "history": redact_sensitive_payload(self.history),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TaskEngine:
    """
    Central Task State Governor for WISE.
    Manages long-horizon execution state, subgoal progression, checkpoints,
    and pause/resume transitions completely decoupled from World State facts.
    """

    def __init__(self, storage_path: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self._tasks: Dict[str, Task] = {}
        self._active_task_id: Optional[str] = None
        self._storage_path = Path(storage_path) if storage_path else CONFIG_DIR / "tasks.json"
        self._load_persisted()
        self._committed_tasks = copy.deepcopy(self._tasks)
        self._committed_active_id = self._active_task_id

    @staticmethod
    def _validate_task_record(raw: Any) -> None:
        if not isinstance(raw, dict):
            raise ValueError("task record must be an object")
        for field_name in ("subgoals", "checkpoints", "history", "milestones", "artifacts"):
            if not isinstance(raw.get(field_name, []), list):
                raise ValueError(f"invalid task {field_name}")
        if not isinstance(raw.get("context_variables", {}), dict):
            raise ValueError("invalid task context")
        if raw.get("active_intervention") is not None and not isinstance(raw["active_intervention"], dict):
            raise ValueError("invalid task intervention")
        for name in ("current_subgoal_idx", "current_milestone_idx"):
            if isinstance(raw.get(name, 0), bool) or not isinstance(raw.get(name, 0), int):
                raise ValueError(f"invalid task {name}")
        for name in ("created_at", "updated_at"):
            value = raw.get(name, 0.0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"invalid task {name}")
        for sg in raw.get("subgoals", []):
            if not isinstance(sg, dict) or not isinstance(sg.get("steps", []), list):
                raise ValueError("invalid task subgoal")
            if sg.get("checkpoint") is not None and not isinstance(sg["checkpoint"], dict):
                raise ValueError("invalid subgoal checkpoint")
            if isinstance(sg.get("current_step_idx", 0), bool) or not isinstance(sg.get("current_step_idx", 0), int):
                raise ValueError("invalid subgoal cursor")
            for st in sg.get("steps", []):
                if not isinstance(st, dict) or not isinstance(st.get("params", {}), dict):
                    raise ValueError("invalid task step")
                for name in ("result", "verification_spec"):
                    if st.get(name) is not None and not isinstance(st[name], dict):
                        raise ValueError(f"invalid step {name}")
        for chk in raw.get("checkpoints", []):
            if not isinstance(chk, dict) or not isinstance(chk.get("metadata", {}), dict) or not isinstance(chk.get("created_artifacts", []), list):
                raise ValueError("invalid task checkpoint")

    @staticmethod
    def _validate_cursors(task: Task) -> None:
        if not 0 <= task.current_subgoal_idx <= len(task.subgoals):
            raise ValueError("task subgoal cursor out of bounds")
        if not 0 <= task.current_milestone_idx <= len(task.milestones):
            raise ValueError("task milestone cursor out of bounds")
        for sg in task.subgoals:
            if not 0 <= sg.current_step_idx <= len(sg.steps):
                raise ValueError("task step cursor out of bounds")

    @staticmethod
    def _checkpoint_from_dict(raw: Dict[str, Any]) -> TaskCheckpoint:
        if not isinstance(raw, dict) or not isinstance(raw.get("metadata", {}), dict) or not isinstance(raw.get("created_artifacts", []), list):
            raise ValueError("invalid checkpoint schema")
        return TaskCheckpoint(
            checkpoint_id=str(raw.get("checkpoint_id") or f"chk_{uuid.uuid4().hex[:8]}"),
            subgoal_id=str(raw.get("subgoal_id") or ""),
            timestamp=float(raw.get("timestamp") or time.time()),
            active_window_title=raw.get("active_window_title"),
            active_hwnd=raw.get("active_hwnd"),
            browser_url=raw.get("browser_url"),
            created_artifacts=list(raw.get("created_artifacts") or []),
            metadata=dict(raw.get("metadata") or {}),
        )

    @classmethod
    def _task_from_dict(cls, raw: Dict[str, Any]) -> Optional[Task]:
        try:
            cls._validate_task_record(raw)
            subgoals: List[Subgoal] = []
            for sg_raw in raw.get("subgoals") or []:
                steps = [TaskStep(
                    step_id=str(st.get("step_id") or f"step_{uuid.uuid4().hex[:8]}"),
                    action_type=ComputerActionType(st.get("action_type", ComputerActionType.WAIT.value)),
                    params=dict(st.get("params") or {}), description=str(st.get("description") or ""),
                    verification_spec=st.get("verification_spec"), security_tier=st.get("security_tier"),
                    max_retries=int(st.get("max_retries", 2)), status=StepStatus(st.get("status", StepStatus.PENDING.value)),
                    result=st.get("result"), is_corrective=bool(st.get("is_corrective")),
                    requires_human=bool(st.get("requires_human")),
                    human_intervention_type=st.get("human_intervention_type"),
                    human_intervention_reason=st.get("human_intervention_reason"), human_prompt=st.get("human_prompt"),
                    confirmation_token=st.get("confirmation_token"), created_at=float(st.get("created_at") or time.time()),
                    completed_at=st.get("completed_at"),
                ) for st in sg_raw.get("steps") or []]
                checkpoint = sg_raw.get("checkpoint")
                subgoals.append(Subgoal(
                    subgoal_id=str(sg_raw.get("subgoal_id") or f"sg_{uuid.uuid4().hex[:8]}"),
                    title=str(sg_raw.get("title") or ""), description=str(sg_raw.get("description") or ""),
                    domain=TaskDomain(sg_raw.get("domain", TaskDomain.WINDOWS_OS.value)), steps=steps,
                    status=SubgoalStatus(sg_raw.get("status", SubgoalStatus.PENDING.value)),
                    checkpoint=cls._checkpoint_from_dict(checkpoint) if checkpoint else None,
                    current_step_idx=int(sg_raw.get("current_step_idx", 0)),
                ))
            task = Task(
                task_id=str(raw.get("task_id") or f"task_{uuid.uuid4().hex[:10]}"), session_id=raw.get("session_id"),
                user_intent=str(raw.get("user_intent") or ""), semantic_goal=str(raw.get("semantic_goal") or ""),
                milestones=list(raw.get("milestones") or []), current_milestone_idx=int(raw.get("current_milestone_idx", 0)),
                subgoals=subgoals, current_subgoal_idx=int(raw.get("current_subgoal_idx", 0)),
                status=TaskStatus(raw.get("status", TaskStatus.PENDING.value)), artifacts=list(raw.get("artifacts") or []),
                checkpoints=[cls._checkpoint_from_dict(c) for c in raw.get("checkpoints") or []],
                history=list(raw.get("history") or []), error=raw.get("error"), pause_reason=raw.get("pause_reason"),
                active_intervention=raw.get("active_intervention"), context_variables=dict(raw.get("context_variables") or {}),
                created_at=float(raw.get("created_at") or time.time()),
                updated_at=float(raw.get("updated_at") or time.time()),
            )
            cls._validate_cursors(task)
            return task
        except (TypeError, ValueError) as exc:
            LOG.warning("Skipping invalid persisted task: %s", exc)
            return None

    def _load_persisted(self) -> None:
        try:
            backup_path = self._storage_path.with_suffix(".json.bak")
            if not self._storage_path.exists() and not backup_path.exists():
                return
            raw: Optional[Dict[str, Any]] = None
            loaded_from_backup = False
            load_error: Optional[Exception] = None
            for candidate in (self._storage_path, backup_path):
                if not candidate.exists():
                    continue
                try:
                    decoded = json.loads(candidate.read_text(encoding="utf-8"))
                    if not isinstance(decoded, dict) or not isinstance(decoded.get("tasks", []), list):
                        raise ValueError("task persistence payload has an invalid shape")
                    if decoded.get("version", 1) != 1:
                        raise ValueError("unsupported task persistence version")
                    if decoded.get("active_task_id") is not None and not isinstance(decoded["active_task_id"], str):
                        raise ValueError("invalid active task identity")
                    parsed = [self._task_from_dict(item) for item in decoded.get("tasks", [])]
                    if any(task is None for task in parsed):
                        raise ValueError("task persistence contains invalid nested records")
                    if len({task.task_id for task in parsed}) != len(parsed):
                        raise ValueError("task persistence contains duplicate identities")
                    raw = decoded
                    if candidate == backup_path:
                        loaded_from_backup = True
                        LOG.warning("Recovered task state from backup after primary persistence could not be read")
                    break
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    load_error = exc
                    LOG.warning("Could not read persisted task state from %s: %s", candidate, exc)
            if raw is None:
                if load_error:
                    self._storage_corrupt = True
                    LOG.error("No valid persisted task state is available; leaving in-memory state empty")
                return
            recovered_running_task = False
            for item in raw.get("tasks") or []:
                task = self._task_from_dict(item)
                if task:
                    # A process restart means the previous in-flight action
                    # may have partially completed.  Preserve its exact step,
                    # but never claim it is still executing or replay it
                    # silently; the orchestrator can verify and resume it.
                    if task.status in (TaskStatus.RUNNING, TaskStatus.RECOVERING) or (loaded_from_backup and task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)):
                        task.status = TaskStatus.PAUSED_FOR_HUMAN
                        task.pause_reason = ("Task backup restored; newer action outcomes may be unknown" if loaded_from_backup
                                             else "WISE runtime restarted while this task was running")
                        task.active_intervention = {
                            "type": "RUNTIME_RECOVERY",
                            "reason": task.pause_reason,
                            "prompt": "WISE restarted. Review the current state, then resume this task when ready.",
                            "paused_at": time.time(),
                            "current_subgoal_idx": task.current_subgoal_idx,
                            "current_step_idx": (
                                task.subgoals[task.current_subgoal_idx].current_step_idx
                                if task.current_subgoal_idx < len(task.subgoals) else 0
                            ),
                        }
                        task.updated_at = time.time()
                        recovered_running_task = True
                    self._tasks[task.task_id] = task
            candidate = raw.get("active_task_id")
            self._active_task_id = candidate if candidate in self._tasks else next(
                (t.task_id for t in self._tasks.values() if t.status in (TaskStatus.RUNNING, TaskStatus.PAUSED_FOR_HUMAN)), None)
            if recovered_running_task:
                self._persist_locked()
        except Exception as exc:
            LOG.warning("Could not load persisted tasks: %s", exc)

    def _persist_locked(self) -> None:
        temporary: Optional[Path] = None
        try:
            if getattr(self, "_storage_corrupt", False):
                raise ValueError("Unrecoverable task storage; restore or explicitly remove it before writing new state")
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "active_task_id": self._active_task_id,
                       "tasks": [task.to_dict() for task in self._tasks.values()]}
            for task in self._tasks.values():
                self._validate_cursors(task)
            # A distinct temp file prevents two independently-created engines
            # from trampling each other's in-progress write.  ``replace`` keeps
            # readers from ever observing a partially-written JSON document.
            temporary = self._storage_path.parent / f".{self._storage_path.name}.{uuid.uuid4().hex}.tmp"
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            # Preserve the previous known-good state as a recovery fallback
            # before atomically installing the new payload.
            if self._storage_path.exists():
                try:
                    previous = json.loads(self._storage_path.read_text(encoding="utf-8"))
                    valid_previous = isinstance(previous, dict) and isinstance(previous.get("tasks"), list) and all(self._task_from_dict(item) is not None for item in previous["tasks"])
                except (OSError, ValueError):
                    valid_previous = False
                if valid_previous:
                    backup_tmp = self._storage_path.parent / f".{self._storage_path.name}.{uuid.uuid4().hex}.bak.tmp"
                    try:
                        with backup_tmp.open("wb") as backup:
                            backup.write(self._storage_path.read_bytes())
                            backup.flush()
                            os.fsync(backup.fileno())
                        self._replace_state_file(backup_tmp, self._storage_path.with_suffix(".json.bak"))
                    finally:
                        backup_tmp.unlink(missing_ok=True)
            self._replace_state_file(temporary, self._storage_path)
            self._committed_tasks = copy.deepcopy(self._tasks)
            self._committed_active_id = self._active_task_id
        except Exception as exc:
            LOG.error("Could not persist tasks: %s", exc)
            if temporary:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            # Restore acknowledged state rather than report a successful
            # transition that only happened in RAM. Preserve Task references.
            if hasattr(self, "_committed_tasks"):
                restored = {}
                for key, snapshot in self._committed_tasks.items():
                    task = self._tasks.get(key, copy.deepcopy(snapshot))
                    task.__dict__.clear()
                    task.__dict__.update(copy.deepcopy(snapshot.__dict__))
                    restored[key] = task
                self._tasks = restored
                self._active_task_id = self._committed_active_id
            raise TaskPersistenceError("Could not persist task transition") from exc

    @staticmethod
    def _replace_state_file(source: Path, destination: Path) -> None:
        """Retry a short Windows sharing/access denial, never acknowledge it.

        An API reader or filesystem scanner may briefly hold the destination
        while a team publishes events. Retain the same fsynced candidate and
        retry only the atomic rename, not the task action or mutation. A real
        permission failure still propagates after at most 375 ms of backoff.
        """
        for attempt in range(5):
            try:
                os.replace(source, destination)
                return
            except OSError as exc:
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 4:
                    raise
                time.sleep(.025 * (2 ** attempt))

    def create_task(
        self,
        user_intent: str,
        semantic_goal: str,
        subgoals: Optional[List[Subgoal]] = None,
        session_id: Optional[str] = None,
    ) -> Task:
        """Initializes a new top-level Task and sets it as active."""
        with self._lock:
            task = Task(
                session_id=session_id,
                user_intent=user_intent,
                semantic_goal=semantic_goal,
                subgoals=subgoals or [],
                status=TaskStatus.PENDING,
            )
            self._tasks[task.task_id] = task
            self._active_task_id = task.task_id
            self._persist_locked()
            LOG.info("Created Task '%s' (session '%s') for intent: '%s'", task.task_id, session_id, user_intent[:50])
            return task

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def create_capability_task(self, goal: str, session_id: str) -> Task:
        """Track dynamic tool work without inventing a GUI action plan."""
        with self._lock:
            task = self.create_task(goal, goal, session_id=session_id)
            task.context_variables["execution_kind"] = "capability"
            self.start_task(task.task_id)
            return task

    def record_capability_event(self, task_id: str, tool: str, status: str,
                                *, error: Optional[str] = None) -> None:
        """Durable, redacted operational evidence; never a claim of verification."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            event = {"tool": tool, "status": status, "timestamp": time.time(), "error": error}
            task.history.append(redact_sensitive_payload(event))
            if status == "IN_PROGRESS":
                task.milestones.append({"title": tool, "description": tool, "status": status})
            else:
                pending = next((m for m in reversed(task.milestones) if isinstance(m, dict)
                    and m.get("title") == tool and m.get("status") == "IN_PROGRESS"), None)
                if pending is not None:
                    pending["status"] = status
                else:
                    task.milestones.append({"title": tool, "description": tool, "status": status})
            task.updated_at = time.time()
            self._persist_locked()

    def start_task(self, task_id: str) -> bool:
        """Transitions task to RUNNING and sets first subgoal to IN_PROGRESS."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                LOG.warning("Cannot start terminal task '%s' from status %s", task_id, task.status.value)
                return False
            task.status = TaskStatus.RUNNING
            task.updated_at = time.time()
            if task.current_subgoal_idx < len(task.subgoals):
                task.subgoals[task.current_subgoal_idx].status = SubgoalStatus.IN_PROGRESS
            self._persist_locked()
            LOG.info("Task '%s' started.", task_id)
            return True

    def record_step_result(
        self,
        task_id: str,
        step_id: str,
        success: bool,
        result_payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Records the outcome of a step execution with sensitive parameter scrubbing."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            clean_result = redact_sensitive_payload(result_payload or {})
            for sg in task.subgoals:
                for st in sg.steps:
                    if st.step_id == step_id:
                        if st.status == StepStatus.COMPLETED and not success:
                            return False
                        st.status = StepStatus.COMPLETED if success else StepStatus.FAILED
                        st.result = clean_result
                        st.completed_at = time.time()
                        task.updated_at = time.time()
                        self._persist_locked()
                        return True
            return False

    def advance_step(self, task_id: str) -> Tuple[Optional[TaskStep], bool]:
        """
        Advances the current pointer to the next step.
        If a subgoal finishes, creates a checkpoint and advances to the next subgoal.
        Returns: (next_step, is_task_completed)
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None, False

            self._validate_cursors(task)
            if task.status != TaskStatus.RUNNING:
                return task.get_current_step(), task.status == TaskStatus.COMPLETED
            current = task.get_current_step()
            if current is not None and current.status not in (StepStatus.COMPLETED, StepStatus.SKIPPED):
                return current, False

            if task.current_subgoal_idx >= len(task.subgoals):
                if any(step.status not in (StepStatus.COMPLETED, StepStatus.SKIPPED) for sg in task.subgoals for step in sg.steps):
                    return None, False
                task.status = TaskStatus.COMPLETED
                self._persist_locked()
                return None, True

            current_sg = task.subgoals[task.current_subgoal_idx]
            if any(st.status not in (StepStatus.COMPLETED, StepStatus.SKIPPED) for st in current_sg.steps[:current_sg.current_step_idx]):
                return current, False
            if current is not None:
                current_sg.current_step_idx += 1

            # Check if current subgoal is complete
            if current_sg.current_step_idx >= len(current_sg.steps):
                current_sg.status = SubgoalStatus.COMPLETED
                # Auto-create checkpoint at subgoal boundary
                chk = TaskCheckpoint(
                    subgoal_id=current_sg.subgoal_id,
                    metadata={"subgoal_title": current_sg.title, "current_step_idx": current_sg.current_step_idx},
                )
                current_sg.checkpoint = chk
                task.checkpoints.append(chk)
                LOG.info("Subgoal '%s' COMPLETED. Checkpoint saved: %s", current_sg.title, chk.checkpoint_id)

                # Move to next subgoal
                task.current_subgoal_idx += 1
                if task.current_subgoal_idx >= len(task.subgoals):
                    task.status = TaskStatus.COMPLETED
                    task.updated_at = time.time()
                    self._persist_locked()
                    LOG.info("All subgoals completed. Task '%s' COMPLETED.", task_id)
                    return None, True
                else:
                    task.subgoals[task.current_subgoal_idx].status = SubgoalStatus.IN_PROGRESS
                    current_sg = task.subgoals[task.current_subgoal_idx]

            # Empty subgoals are legal planning placeholders, not indexable
            # actions. Walk them without incrementing any executed step twice.
            while current_sg.current_step_idx >= len(current_sg.steps):
                current_sg.status = SubgoalStatus.COMPLETED
                task.current_subgoal_idx += 1
                if task.current_subgoal_idx >= len(task.subgoals):
                    task.status = TaskStatus.COMPLETED
                    task.updated_at = time.time()
                    self._persist_locked()
                    return None, True
                current_sg = task.subgoals[task.current_subgoal_idx]
                current_sg.status = SubgoalStatus.IN_PROGRESS

            next_step = current_sg.steps[current_sg.current_step_idx]
            # Do not reset an already completed action during restart/retry.
            task.updated_at = time.time()
            self._persist_locked()
            return next_step, False

    def pause_task(
        self,
        task_id: str,
        reason: str = "WAITING_FOR_HUMAN",
        intervention_type: str = "POLICY",
        prompt: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> Optional[TaskCheckpoint]:
        """
        Pauses the task cleanly for human intervention (CAPTCHA, MFA, user confirmation).
        Preserves the exact current step and subgoal index, captures an explicit checkpoint,
        and records structured intervention requirements.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None

            task.status = TaskStatus.PAUSED_FOR_HUMAN
            task.pause_reason = reason
            task.updated_at = time.time()

            sg_id = ""
            current_step_idx = 0
            if task.current_subgoal_idx < len(task.subgoals):
                current_sg = task.subgoals[task.current_subgoal_idx]
                sg_id = current_sg.subgoal_id
                current_step_idx = current_sg.current_step_idx

            clean_details = redact_sensitive_payload(details or {})
            task.active_intervention = {
                "type": intervention_type,
                "reason": reason,
                "prompt": prompt or reason,
                "details": clean_details,
                "paused_at": time.time(),
                "current_subgoal_idx": task.current_subgoal_idx,
                "current_step_idx": current_step_idx,
            }

            chk = TaskCheckpoint(
                subgoal_id=sg_id,
                metadata={
                    "pause_reason": reason,
                    "intervention_type": intervention_type,
                    "current_subgoal_idx": task.current_subgoal_idx,
                    "current_step_idx": current_step_idx,
                    "paused_at": time.time(),
                },
            )
            task.checkpoints.append(chk)
            self._persist_locked()
            LOG.warning(
                "Task '%s' PAUSED_FOR_HUMAN. Reason: %s (Type: %s, Subgoal: %d, Step: %d)",
                task_id,
                reason,
                intervention_type,
                task.current_subgoal_idx,
                current_step_idx,
            )
            return chk

    def pause_for_human(
        self,
        task_id: str,
        intervention_type: str = "POLICY",
        reason: str = "WAITING_FOR_HUMAN",
        prompt: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> Optional[TaskCheckpoint]:
        """Convenience alias for pause_task with rich human intervention fields."""
        return self.pause_task(
            task_id=task_id,
            reason=reason,
            intervention_type=intervention_type,
            prompt=prompt,
            details=details,
        )

    def resume_task(self, task_id: str) -> bool:
        """
        Resumes a paused task from its exact current step and subgoal.
        Guarantees that the task does NOT restart from the beginning.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            if task.status not in (TaskStatus.PAUSED, TaskStatus.PAUSED_FOR_HUMAN):
                LOG.warning("Cannot resume task '%s' from status %s", task_id, task.status)
                return False
            self._validate_cursors(task)

            task.status = TaskStatus.RUNNING
            task.pause_reason = None
            task.active_intervention = None
            task.updated_at = time.time()
            self._persist_locked()
            LOG.info(
                "Task '%s' resumed successfully at Subgoal %d, Step %d.",
                task_id,
                task.current_subgoal_idx,
                task.subgoals[task.current_subgoal_idx].current_step_idx if task.current_subgoal_idx < len(task.subgoals) else 0,
            )
            return True

    def resolve_human_intervention(
        self,
        task_id: str,
        action: str = "completed",
        resolution_payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Applies human intervention resolution (completed/approved vs cancelled/rejected).
        On completion, restores RUNNING status on the exact current step without resetting progress.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            if task.status != TaskStatus.PAUSED_FOR_HUMAN:
                LOG.warning("Cannot resolve human intervention on task '%s' in status %s", task_id, task.status)
                return False

            action_lower = action.strip().lower()
            if action_lower in ("completed", "approved", "done", "resume"):
                return self.resume_task(task_id)
            elif action_lower in ("cancelled", "rejected", "abort", "cancel"):
                return self.cancel_task(task_id, reason=f"Human intervention cancelled by user ({action})")
            else:
                LOG.error("Unknown intervention resolution action: '%s'", action)
                return False

    def rollback_to_checkpoint(self, task_id: str, checkpoint_id: str) -> bool:
        """Reposition a logical cursor, pausing for review; no external undo."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            target_chk = next((c for c in task.checkpoints if c.checkpoint_id == checkpoint_id), None)
            if not target_chk:
                LOG.error("Checkpoint '%s' not found in task '%s'", checkpoint_id, task_id)
                return False

            # Locate the subgoal corresponding to this checkpoint
            for idx, sg in enumerate(task.subgoals):
                if sg.subgoal_id == target_chk.subgoal_id:
                    task.current_subgoal_idx = idx
                    saved_step = target_chk.metadata.get("current_step_idx", len(sg.steps))
                    if not isinstance(saved_step, int) or not 0 <= saved_step <= len(sg.steps):
                        raise ValueError("checkpoint step cursor out of bounds")
                    sg.current_step_idx = saved_step
                    sg.status = SubgoalStatus.IN_PROGRESS
                    # Reset subsequent subgoals to PENDING
                    for later_sg in task.subgoals[idx + 1:]:
                        later_sg.status = SubgoalStatus.PENDING
                        later_sg.current_step_idx = 0
                        for st in later_sg.steps:
                            st.status = StepStatus.PENDING
                    task.status = TaskStatus.PAUSED_FOR_HUMAN
                    task.pause_reason = "Logical checkpoint selected; review external effects before resuming"
                    task.active_intervention = {"type": "CHECKPOINT_REVIEW", "reason": task.pause_reason}
                    task.updated_at = time.time()
                    self._persist_locked()
                    LOG.info("Rolled back Task '%s' to Subgoal '%s' via checkpoint '%s'", task_id, sg.title, checkpoint_id)
                    return True
            return False

    def cancel_task(self, task_id: str, reason: str = "User cancelled") -> bool:
        """Cancels an active task cleanly."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.status = TaskStatus.CANCELLED
            task.error = reason
            task.pause_reason = None
            task.active_intervention = None
            task.updated_at = time.time()
            self._persist_locked()
            LOG.info("Task '%s' CANCELLED: %s", task_id, reason)
            return True

    def complete_task(self, task_id: str) -> bool:
        """Mark a verified plan complete without fabricating per-step results."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status in (TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.PAUSED, TaskStatus.PAUSED_FOR_HUMAN, TaskStatus.WAITING_FOR_USER, TaskStatus.RECOVERING):
                return False
            if any(st.status not in (StepStatus.COMPLETED, StepStatus.SKIPPED) for sg in task.subgoals for st in sg.steps):
                return False
            task.status = TaskStatus.COMPLETED
            for sg in task.subgoals:
                sg.status = SubgoalStatus.COMPLETED
            task.updated_at = time.time()
            self._persist_locked()
            LOG.info("Task '%s' marked COMPLETED.", task_id)
            return True

    def fail_task(self, task_id: str, error: str) -> bool:
        """Marks task as failed."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.status = TaskStatus.FAILED
            task.error = error
            task.updated_at = time.time()
            self._persist_locked()
            LOG.error("Task '%s' FAILED: %s", task_id, error)
            return True

    def set_task_recovering(self, task_id: str, reason: str = "") -> bool:
        """Transitions task into RECOVERING status while preserving completed checkpoints."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.status = TaskStatus.RECOVERING
            task.updated_at = time.time()
            if reason:
                task.pause_reason = f"Recovery in progress: {reason}"
            self._persist_locked()
            LOG.info("Task '%s' entered RECOVERING status: %s", task_id, reason)
            return True

    def stop_task(self, task_id: str, reason: str = "Stopped by user") -> bool:
        """Stops/pauses an active task."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.status = TaskStatus.PAUSED
            task.pause_reason = reason
            task.updated_at = time.time()
            self._persist_locked()
            LOG.info("Task '%s' STOPPED/PAUSED: %s", task_id, reason)
            return True

    def modify_task(
        self,
        task_id: str,
        new_criteria: str,
        new_subgoals: Optional[List[TaskSubgoal]] = None,
        context_update: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Dynamically modifies an active or paused task with new criteria or additional subgoals.
        Preserves all completed subgoals and checkpoints; updates pending/future work.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            # Update semantic goal or append new criteria
            task.semantic_goal = f"{task.semantic_goal} | Modification: {new_criteria}"
            if context_update:
                clean_ctx = redact_sensitive_payload(context_update)
                task.context_variables.update(clean_ctx)

            if new_subgoals:
                # Retain all completed subgoals, append or replace pending ones
                completed_sgs = [sg for sg in task.subgoals if sg.status == SubgoalStatus.COMPLETED]
                task.subgoals = completed_sgs + new_subgoals
                task.current_subgoal_idx = len(completed_sgs)

            task.status = TaskStatus.RUNNING
            task.pause_reason = None
            task.active_intervention = None
            task.updated_at = time.time()
            self._persist_locked()
            LOG.info("Task '%s' modified with new criteria: '%s'", task_id, new_criteria)
            return True

    def get_active_task(self, session_id: Optional[str] = None) -> Optional[Task]:
        """Returns the most recent active or paused task if any."""
        with self._lock:
            active_statuses = {
                TaskStatus.RUNNING,
                TaskStatus.PAUSED,
                TaskStatus.PAUSED_FOR_HUMAN,
                TaskStatus.WAITING_FOR_USER,
                TaskStatus.RECOVERING,
            }
            for task in reversed(list(self._tasks.values())):
                if task.status in active_statuses and (session_id is None or task.session_id == session_id):
                    return task
            return None

    def list_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns summarized dictionaries of recent tasks."""
        with self._lock:
            tasks_list = list(self._tasks.values())
            tasks_list.sort(key=lambda t: t.created_at, reverse=True)
            return [t.to_dict() for t in tasks_list[:limit]]

    def add_milestone(
        self,
        task_id: str,
        title: str,
        description: str,
        subgoals: Optional[List[str]] = None,
        dependencies: Optional[List[str]] = None,
        success_criteria: Optional[List[str]] = None,
    ) -> Optional[Any]:
        """Hierarchical long-horizon planning: adds a milestone node to a task."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            from core.contracts import Milestone
            ms = Milestone(
                title=title,
                description=description,
                subgoals=subgoals or [],
                dependencies=dependencies or [],
                success_criteria=success_criteria or [],
            )
            task.milestones.append(ms)
            task.updated_at = time.time()
            self._persist_locked()
            return ms

    def register_artifact(
        self,
        task_id: str,
        path: str,
        artifact_type: str = "FILE",
        step_id: Optional[str] = None,
    ) -> Optional[Any]:
        """Registers a created physical artifact with size and modification timestamp."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            from core.contracts import ArtifactRecord
            from pathlib import Path
            p = Path(path)
            size = p.stat().st_size if p.is_file() else 0
            mtime = p.stat().st_mtime if p.exists() else time.time()
            rec = ArtifactRecord(
                path=str(p.resolve()) if p.exists() else path,
                artifact_type=artifact_type,
                size_bytes=size,
                mtime=mtime,
                is_verified=False,
                created_by_step_id=step_id,
            )
            task.artifacts.append(rec)
            task.updated_at = time.time()
            self._persist_locked()
            return rec

    def verify_artifact(
        self,
        task_id: str,
        artifact_path: str,
        spec: Optional[Any] = None,
    ) -> bool:
        """
        Physical Artifact Verification:
        Check exact file identity, size, optional freshness and literal patterns.
        This is not a guarantee of semantic correctness or all file structures.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            from pathlib import Path
            p = Path(artifact_path)
            if not p.is_file():
                return False
            size = p.stat().st_size
            min_size = getattr(spec, "min_size_bytes", 1) if spec else 1
            if size < min_size:
                return False
            if spec and getattr(spec, "verify_recent_mtime", False):
                age = time.time() - p.stat().st_mtime
                if age < -60 or age > getattr(spec, "mtime_threshold_seconds", 600.0):
                    return False

            patterns = getattr(spec, "required_patterns", []) if spec else []
            if patterns:
                try:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                    if not all(pat in content for pat in patterns):
                        return False
                except Exception:
                    return False

            # Mark matching record verified
            for art in task.artifacts:
                art_p = art.get("path", "") if isinstance(art, dict) else getattr(art, "path", "")
                if art_p and Path(art_p).resolve() == p.resolve():
                    values = {"is_verified": True, "size_bytes": size, "mtime": p.stat().st_mtime, "verification_notes": f"Verified physically: {size} bytes"}
                    if isinstance(art, dict):
                        art.update(values)
                    else:
                        art.__dict__.update(values)
                    task.updated_at = time.time()
                    self._persist_locked()
                    return True

            # If not in records, add as verified record
            from core.contracts import ArtifactRecord
            rec = ArtifactRecord(
                path=str(p.resolve()),
                size_bytes=size,
                mtime=p.stat().st_mtime,
                is_verified=True,
                verification_notes=f"Verified physically: {size} bytes",
            )
            task.artifacts.append(rec)
            task.updated_at = time.time()
            self._persist_locked()
            return True

    def extend_plan_with_steps(
        self,
        task_id: str,
        subgoal_id: str,
        new_steps: List[TaskStep],
    ) -> bool:
        """Rolling planning support: dynamically extends an existing subgoal with additional steps."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            for sg in task.subgoals:
                if sg.subgoal_id == subgoal_id:
                    sg.steps.extend(new_steps)
                    task.updated_at = time.time()
                    self._persist_locked()
                    LOG.info("Subgoal '%s' extended with %d new steps (total: %d)", subgoal_id, len(new_steps), len(sg.steps))
                    return True
            return False



# Global Singleton Task Engine maintained for backward compatibility
_TASK_ENGINE: Optional[TaskEngine] = None
_TE_LOCK = threading.Lock()


def get_task_engine() -> TaskEngine:
    global _TASK_ENGINE
    if _TASK_ENGINE is None:
        with _TE_LOCK:
            if _TASK_ENGINE is None:
                _TASK_ENGINE = TaskEngine()
    return _TASK_ENGINE


def create_task_from_steps(
    user_intent: str,
    steps: List[Any],
    semantic_goal: str = "",
) -> Task:
    """Convenience factory to create a structured Task from an existing list of PlannedSteps."""
    engine = get_task_engine()
    task_steps: List[TaskStep] = []

    for s in steps:
        if isinstance(s, TaskStep):
            task_steps.append(s)
        else:
            task_steps.append(
                TaskStep(
                    action_type=getattr(s, "action_type", ComputerActionType.WAIT),
                    params=dict(getattr(s, "params", {})),
                    description=getattr(s, "description", ""),
                    verification_spec=getattr(s, "verification_spec", None),
                    is_corrective=getattr(s, "is_corrective", False),
                    requires_human=getattr(s, "requires_human", False),
                    human_intervention_type=getattr(s, "human_intervention_type", None),
                    human_intervention_reason=getattr(s, "human_intervention_reason", None),
                    human_prompt=getattr(s, "human_prompt", None),
                    confirmation_token=getattr(s, "confirmation_token", None),
                )
            )

    domain = TaskDomain.WINDOWS_OS
    if any("browser" in s.action_type.value.lower() for s in task_steps):
        domain = TaskDomain.BROWSER

    subgoal = Subgoal(
        title=f"Execute: {user_intent[:40]}",
        description=semantic_goal or user_intent,
        domain=domain,
        steps=task_steps,
        status=SubgoalStatus.PENDING,
    )

    return engine.create_task(
        user_intent=user_intent,
        semantic_goal=semantic_goal or user_intent,
        subgoals=[subgoal],
    )
