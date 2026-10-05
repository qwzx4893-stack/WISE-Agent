# ==============================================================================
# WISE Browser Subsystem — Pydantic v2 Data Models
# All typed contracts for browser session, page observation, actions,
# dialog events, and download records.
# ==============================================================================

from __future__ import annotations

import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class BrowserLaunchMode(str, Enum):
    HEADED = "HEADED"
    HEADLESS = "HEADLESS"


class BrowserProfileMode(str, Enum):
    """Where a browser session obtains its profile and cookies.

    ``ISOLATED`` is deliberately the default: it creates a WISE-owned context.
    ``HOST_CDP`` is opt-in and attaches to an already-running local Brave
    instance through a loopback CDP endpoint, so it can use the user's tabs and
    signed-in accounts without copying a browser profile.
    """

    ISOLATED = "isolated"
    HOST_CDP = "host_cdp"


class BrowserSessionConfig(BaseModel):
    """Configuration for a Playwright browser session."""
    channel: str = Field(default="brave", description="Browser channel: brave, chrome, msedge, or chromium")
    executable_path: Optional[str] = Field(default=None, description="Optional explicit browser executable path")
    headless: bool = Field(default=False, description="Run headless (True only for tests/CI)")
    default_timeout_ms: float = Field(default=30_000, description="Default action timeout in ms")
    download_dir: Optional[str] = Field(default=None, description="Controlled download directory path")
    viewport_width: int = Field(default=1280, description="Browser viewport width")
    viewport_height: int = Field(default=800, description="Browser viewport height")
    user_agent: Optional[str] = Field(default=None, description="Custom user agent override")
    profile_mode: BrowserProfileMode = Field(
        default=BrowserProfileMode.ISOLATED,
        description="isolated (default) or host_cdp (explicit access to local Brave)",
    )
    cdp_url: Optional[str] = Field(
        default=None,
        description="Local CDP endpoint for host_cdp; defaults to WISE_BRAVE_CDP_URL or localhost:9222",
    )
    persist_storage: bool = Field(
        default=False,
        description="Persist cookies only for an explicitly opted-in isolated WISE profile",
    )


class ElementRef(BaseModel):
    """A reference to an element observed in the ARIA snapshot."""
    ref_id: str = Field(description="ARIA ref identifier (from boxes=True)")
    role: str = Field(default="", description="ARIA role (textbox, button, link, etc.)")
    name: str = Field(default="", description="Accessible name of the element")
    description: str = Field(default="", description="Additional accessible description")
    value: str = Field(default="", description="Current value if applicable")
    bounding_box: Optional[Dict[str, float]] = Field(default=None, description="Bounding box {x, y, width, height}")


class PageObservation(BaseModel):
    """Result of observing a page via aria_snapshot."""
    url: str = Field(default="", description="Current page URL")
    title: str = Field(default="", description="Page title")
    aria_snapshot: str = Field(default="", description="Raw ARIA snapshot text")
    elements: List[ElementRef] = Field(default_factory=list, description="Parsed element references")
    timestamp: float = Field(default_factory=time.time, description="Observation timestamp")
    observation_method: str = Field(default="aria_snapshot", description="Method used for observation")

    @property
    def is_stale(self) -> bool:
        """An observation older than 10 seconds is considered potentially stale."""
        return (time.time() - self.timestamp) > 10.0


class BrowserActionResult(BaseModel):
    """Typed result of a browser action."""
    success: bool = Field(default=False)
    action: str = Field(default="", description="Action name executed")
    url: Optional[str] = Field(default=None, description="Current URL after action")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    extracted_text: Optional[str] = Field(default=None, description="Extracted text content")
    observation: Optional[PageObservation] = Field(default=None, description="Post-action observation")
    latency_ms: float = Field(default=0.0, description="Action execution latency")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional action metadata")


class DialogEventType(str, Enum):
    ALERT = "alert"
    CONFIRM = "confirm"
    PROMPT = "prompt"
    BEFOREUNLOAD = "beforeunload"


class DialogEvent(BaseModel):
    """Captured browser dialog event."""
    dialog_type: DialogEventType
    message: str = Field(default="")
    default_value: str = Field(default="", description="Default value for prompt dialogs")
    action_taken: str = Field(default="", description="dismissed / accepted / pending")
    timestamp: float = Field(default_factory=time.time)
    is_high_impact: bool = Field(default=False, description="True if dialog relates to delete/payment/account/security")


class DownloadState(str, Enum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class DownloadRecord(BaseModel):
    """Tracks a browser download lifecycle."""
    filename: str = Field(default="")
    url: str = Field(default="", description="Source URL of the download")
    download_dir: str = Field(default="", description="Controlled download directory")
    file_path: Optional[str] = Field(default=None, description="Final file path on disk")
    state: DownloadState = Field(default=DownloadState.STARTED)
    sha256_hash: Optional[str] = Field(default=None, description="SHA-256 hash of completed download")
    size_bytes: Optional[int] = Field(default=None, description="File size in bytes")
    started_at: float = Field(default_factory=time.time)
    completed_at: Optional[float] = Field(default=None)
    error: Optional[str] = Field(default=None)


class BrowserSessionState(BaseModel):
    """Browser state snapshot for World State integration."""
    is_active: bool = Field(default=False)
    current_url: str = Field(default="")
    current_title: str = Field(default="")
    tab_count: int = Field(default=0)
    active_tab_index: int = Field(default=0)
    last_observation: Optional[PageObservation] = Field(default=None)
    pending_dialogs: List[DialogEvent] = Field(default_factory=list)
    active_downloads: List[DownloadRecord] = Field(default_factory=list)
    timestamp: float = Field(default_factory=time.time)
