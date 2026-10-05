# ==============================================================================
# WISE Browser Subsystem — Browser Dialog Handler
# Registers Playwright dialog event listeners. Applies conservative safety:
# - unexpected alerts → safe dismiss
# - ambiguous confirm/prompt → cancel/dismiss (never auto-accept)
# - high-impact dialogs → require SecurityGate / user confirmation
# - cookie/consent banners → deterministic dismissal when clearly identified
# ==============================================================================

from __future__ import annotations

import re
import logging
import threading
from typing import List, Optional, Callable

from core.browser.browser_models import DialogEvent, DialogEventType

LOG = logging.getLogger("WISE.Browser.DialogHandler")

# Keywords indicating high-impact dialogs that must not be auto-accepted
_HIGH_IMPACT_KEYWORDS = [
    "delete", "remove", "حذف", "إزالة",
    "payment", "pay", "purchase", "دفع", "شراء",
    "account", "حساب",
    "password", "كلمة مرور", "كلمة السر",
    "security", "أمان", "أمن",
    "confirm order", "تأكيد الطلب",
    "unsubscribe", "إلغاء الاشتراك",
    "format", "wipe",
]

# Keywords identifying clearly safe cookie/consent banners in aria snapshots
_COOKIE_BANNER_PATTERNS = [
    r"accept\s*(all\s*)?cookies?",
    r"cookie\s*(consent|policy|notice|banner)",
    r"we\s+use\s+cookies",
    r"الموافقة على ملفات تعريف الارتباط",
    r"قبول الكوكيز",
]


def _is_high_impact_dialog(message: str) -> bool:
    """Check if dialog text suggests a high-impact action."""
    lower = message.lower()
    return any(kw in lower for kw in _HIGH_IMPACT_KEYWORDS)


class BrowserDialogHandler:
    """
    Conservative browser dialog handler.
    Registers on a Playwright page and handles JS dialogs (alert, confirm, prompt, beforeunload).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._dialog_history: List[DialogEvent] = []
        self._pending_dialogs: List[DialogEvent] = []

    @property
    def dialog_history(self) -> List[DialogEvent]:
        with self._lock:
            return list(self._dialog_history)

    @property
    def pending_dialogs(self) -> List[DialogEvent]:
        with self._lock:
            return list(self._pending_dialogs)

    def clear_pending(self) -> None:
        with self._lock:
            self._pending_dialogs.clear()

    def handle_dialog(self, dialog) -> None:
        """
        Playwright dialog event handler. Called by page.on("dialog", ...).
        Conservative behavior:
        - alert → dismiss (informational only)
        - beforeunload → dismiss (cancel navigation loss by default)
        - confirm with high-impact keywords → dismiss (do NOT accept)
        - confirm without high-impact → dismiss (safe default)
        - prompt → dismiss (never auto-fill)
        """
        try:
            dialog_type_str = dialog.type
            message = dialog.message or ""
            default_val = dialog.default_value or ""

            try:
                dtype = DialogEventType(dialog_type_str)
            except ValueError:
                dtype = DialogEventType.ALERT

            is_high_impact = _is_high_impact_dialog(message)

            event = DialogEvent(
                dialog_type=dtype,
                message=message,
                default_value=default_val,
                is_high_impact=is_high_impact,
            )

            # Conservative safety: always dismiss by default
            # Never auto-accept confirm/prompt dialogs
            action = "dismissed"

            if dtype == DialogEventType.ALERT:
                # Alerts are informational — safe to dismiss
                dialog.dismiss()
                action = "dismissed"
                LOG.info("Browser dialog [alert] dismissed: '%s'", message[:100])

            elif dtype == DialogEventType.BEFOREUNLOAD:
                # Cancel navigation loss by default
                dialog.dismiss()
                action = "dismissed"
                LOG.info("Browser dialog [beforeunload] dismissed (navigation preserved)")

            elif dtype == DialogEventType.CONFIRM:
                # NEVER auto-accept confirms — conservative safety
                dialog.dismiss()
                action = "dismissed"
                if is_high_impact:
                    LOG.warning("High-impact confirm dialog DISMISSED (not accepted): '%s'", message[:100])
                else:
                    LOG.info("Browser dialog [confirm] dismissed: '%s'", message[:100])

            elif dtype == DialogEventType.PROMPT:
                # Never auto-fill prompts
                dialog.dismiss()
                action = "dismissed"
                LOG.info("Browser dialog [prompt] dismissed: '%s'", message[:100])

            else:
                dialog.dismiss()
                action = "dismissed"

            event.action_taken = action

            with self._lock:
                self._dialog_history.append(event)
                if is_high_impact:
                    self._pending_dialogs.append(event)

        except Exception as e:
            LOG.error("Error handling browser dialog: %s", e)

    def is_cookie_banner_element(self, element_name: str, element_role: str) -> bool:
        """Check if an element appears to be a cookie consent banner button."""
        combined = f"{element_name} {element_role}".lower()
        return any(re.search(pat, combined) for pat in _COOKIE_BANNER_PATTERNS)

    def suggest_cookie_dismiss_action(self, aria_snapshot: str) -> Optional[str]:
        """
        Scan an ARIA snapshot for clearly identifiable cookie/consent banners.
        Returns the element ref_id to click for dismissal, or None if not clearly identified.
        Only matches very clear, deterministic patterns.
        """
        lower = aria_snapshot.lower()
        for pat in _COOKIE_BANNER_PATTERNS:
            if re.search(pat, lower):
                # Found a cookie banner — look for accept/dismiss/close button ref
                # Format 1: button text followed by [ref=XX] (e.g. - button 'Accept All Cookies' [ref=c2])
                ref_match = re.search(
                    r'(?:accept|reject|dismiss|close|got it|agree|أوافق|إغلاق)[^\]\n]*?\[ref=([^\]]+)\]',
                    lower,
                )
                if ref_match:
                    return ref_match.group(1)
                # Format 2: [ref=XX] followed by text
                ref_match = re.search(
                    r'\[ref=([^\]]+)\].*?(?:accept|reject|dismiss|close|got it|agree|أوافق|إغلاق)',
                    lower,
                )
                if ref_match:
                    return ref_match.group(1)
        return None
