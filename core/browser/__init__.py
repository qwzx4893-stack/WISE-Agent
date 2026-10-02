# ==============================================================================
# WISE Browser Subsystem Package Interface
# ==============================================================================

from .browser_models import (
    BrowserSessionConfig,
    BrowserLaunchMode,
    BrowserProfileMode,
    ElementRef,
    PageObservation,
    BrowserActionResult,
    DialogEventType,
    DialogEvent,
    DownloadState,
    DownloadRecord,
    BrowserSessionState,
)
from .browser_session import BrowserSession
from .browser_page_agent import BrowserPageAgent, is_sensitive_field, redact_sensitive_text
from .browser_action_dispatcher import BrowserActionDispatcher
from .browser_dialog_handler import BrowserDialogHandler
from typing import Optional

_GLOBAL_BROWSER_SESSION: Optional[BrowserSession] = None


def get_browser_session(config: Optional[BrowserSessionConfig] = None) -> BrowserSession:
    """Get or initialize the canonical process-wide BrowserSession."""
    global _GLOBAL_BROWSER_SESSION
    if _GLOBAL_BROWSER_SESSION is None or not _GLOBAL_BROWSER_SESSION.is_active:
        _GLOBAL_BROWSER_SESSION = BrowserSession(config=config)
    return _GLOBAL_BROWSER_SESSION


def shutdown_browser_session() -> None:
    """Release the process-owned browser and its Playwright event loop.

    This is intentionally explicit instead of relying on ``__del__``: Python
    does not guarantee finalizer timing during ASGI shutdown or test teardown.
    """
    global _GLOBAL_BROWSER_SESSION
    session, _GLOBAL_BROWSER_SESSION = _GLOBAL_BROWSER_SESSION, None
    if session is not None:
        session.close()


__all__ = [
    "BrowserSessionConfig",
    "BrowserLaunchMode",
    "BrowserProfileMode",
    "ElementRef",
    "PageObservation",
    "BrowserActionResult",
    "DialogEventType",
    "DialogEvent",
    "DownloadState",
    "DownloadRecord",
    "BrowserSessionState",
    "BrowserSession",
    "get_browser_session",
    "shutdown_browser_session",
    "BrowserPageAgent",
    "BrowserActionDispatcher",
    "BrowserDialogHandler",
    "is_sensitive_field",
    "redact_sensitive_text",
]
