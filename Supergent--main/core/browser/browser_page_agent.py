# ==============================================================================
# WISE Browser Subsystem — Browser Page Agent
# Encapsulates page-level interaction using Playwright.
# Primary observation: page.aria_snapshot(mode="ai", boxes=True)
# Stateless beyond the page reference. Receives page via DI.
# ==============================================================================

from __future__ import annotations

import re
import time
import logging
from typing import Any, Dict, List, Optional

from core.browser.browser_models import (
    BrowserActionResult,
    ElementRef,
    PageObservation,
)

LOG = logging.getLogger("WISE.Browser.PageAgent")

# Sensitive field keywords — used for security tier escalation
_SENSITIVE_FIELD_KEYWORDS = [
    "password", "passwd", "pass",
    "credit", "card", "payment", "cvv", "cvc",
    "token", "secret", "api_key", "apikey",
    "ssn", "social security",
    "كلمة مرور", "كلمة السر",
    "بطاقة ائتمان", "رقم البطاقة", "دفع",
    "رمز الأمان",
]

# Patterns to redact in extracted text
_SENSITIVE_REDACT_PATTERNS = [
    (r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b", "[REDACTED:card_number]"),
    (r"\b\d{3}[\s\-]?\d{2}[\s\-]?\d{4}\b", "[REDACTED:ssn]"),
    (r"(?i)(?:password|token|secret|api[_\s]?key)\s*[:=]\s*\S+", "[REDACTED:credential]"),
]


def is_sensitive_field(element_role: str, element_name: str) -> bool:
    """Detect if an element is a sensitive input field (password, payment, credentials)."""
    combined = f"{element_role} {element_name}".lower()
    # Check explicit password type
    if "password" in combined:
        return True
    return any(kw in combined for kw in _SENSITIVE_FIELD_KEYWORDS)


def redact_sensitive_text(text: str) -> str:
    """Redact sensitive patterns (card numbers, SSNs, credentials) from extracted text."""
    result = text
    for pattern, replacement in _SENSITIVE_REDACT_PATTERNS:
        result = re.sub(pattern, replacement, result)
    return result


def _parse_aria_snapshot(raw_snapshot: str) -> List[ElementRef]:
    """
    Parse the ARIA snapshot text into structured ElementRef objects.
    Extracts [ref=XX] markers and associated role/name/value information.
    """
    elements: List[ElementRef] = []
    if not raw_snapshot:
        return elements

    # Pattern: - role "name" [ref=XX]
    # Also handles variations in the snapshot format
    ref_pattern = re.compile(
        r'- (\w+)\s+"([^"]*)"(?:\s+\[ref=([^\]]+)\])?'
    )

    for match in ref_pattern.finditer(raw_snapshot):
        role = match.group(1) or ""
        name = match.group(2) or ""
        ref_id = match.group(3) or ""

        if ref_id:
            elements.append(ElementRef(
                ref_id=ref_id,
                role=role,
                name=name,
            ))

    # Also capture refs without the standard format (e.g., [ref=XX] standalone)
    standalone_refs = re.compile(r'\[ref=([^\]]+)\]')
    existing_refs = {e.ref_id for e in elements}
    for m in standalone_refs.finditer(raw_snapshot):
        rid = m.group(1)
        if rid not in existing_refs:
            elements.append(ElementRef(ref_id=rid, role="unknown", name=""))
            existing_refs.add(rid)

    return elements


class BrowserPageAgent:
    """
    Page-level browser interaction agent.
    All actions operate on a Playwright Page object received via DI.
    Primary observation: aria_snapshot(mode="ai", boxes=True).
    """

    def __init__(self, page) -> None:
        self._page = page
        self._last_observation: Optional[PageObservation] = None

    @property
    def page(self):
        return self._page

    @property
    def last_observation(self) -> Optional[PageObservation]:
        return self._last_observation

    # ==========================================================================
    # Core Observation
    # ==========================================================================

    def observe(self, depth: Optional[int] = None) -> PageObservation:
        """
        Observe the current page via aria_snapshot(mode="ai", boxes=True).
        Returns a structured PageObservation with element references.
        """
        t0 = time.perf_counter()
        try:
            kwargs: Dict[str, Any] = {"mode": "ai", "boxes": True}
            if depth is not None:
                kwargs["depth"] = depth

            raw_snapshot = self._page.aria_snapshot(**kwargs)
            elements = _parse_aria_snapshot(raw_snapshot)

            obs = PageObservation(
                url=self._page.url,
                title=self._page.title(),
                aria_snapshot=raw_snapshot,
                elements=elements,
                observation_method="aria_snapshot(mode=ai,boxes=True)",
            )
            self._last_observation = obs
            latency = (time.perf_counter() - t0) * 1000
            LOG.info(
                "Page observed: url=%s, elements=%d, latency=%.1fms",
                obs.url[:80], len(elements), latency,
            )
            return obs

        except Exception as e:
            LOG.error("Failed to observe page: %s", e)
            return PageObservation(
                url=self._page.url if not self._page.is_closed() else "",
                title="",
                aria_snapshot="",
                elements=[],
            )

    def detect_challenge(self) -> Any:
        """
        Inspects page observation for active CAPTCHA, MFA, Cloudflare, or OTP challenges.
        Returns ChallengeDetectionResult.
        """
        obs = self._last_observation or self.observe()
        from core.security.challenge_detector import ChallengeDetector
        cd = ChallengeDetector()
        return cd.detect_from_browser_elements(
            elements=obs.elements,
            aria_text=obs.aria_snapshot,
            page_url=obs.url,
        )

    def _resolve_target(self, ref_or_locator: str):
        """
        Resolve an element target.
        If ref_or_locator looks like a ref ID (from boxes=True), use aria ref locator.
        Otherwise, treat as a text/role-based locator.
        """
        page = self._page
        # Try as a get_by_role, get_by_text, or CSS selector depending on format
        # For ARIA refs from boxes=True, Playwright uses page.locator with aria ref
        # Try get_by_text first as it's the most reliable for search fields
        try:
            locator = page.get_by_text(ref_or_locator, exact=False)
            if locator.count() > 0:
                return locator.first
        except Exception:
            pass

        try:
            locator = page.get_by_role("textbox", name=ref_or_locator)
            if locator.count() > 0:
                return locator.first
        except Exception:
            pass

        try:
            locator = page.get_by_role("button", name=ref_or_locator)
            if locator.count() > 0:
                return locator.first
        except Exception:
            pass

        try:
            locator = page.get_by_label(ref_or_locator)
            if locator.count() > 0:
                return locator.first
        except Exception:
            pass

        try:
            locator = page.get_by_placeholder(ref_or_locator)
            if locator.count() > 0:
                return locator.first
        except Exception:
            pass

        # Fallback: ID selector
        if not ref_or_locator.startswith("#") and not ref_or_locator.startswith("."):
            try:
                locator = page.locator(f"#{ref_or_locator}")
                if locator.count() > 0:
                    return locator.first
            except Exception:
                pass

        # Fallback: CSS selector
        try:
            locator = page.locator(ref_or_locator)
            if locator.count() > 0:
                return locator.first
        except Exception:
            pass

        return None

    def _find_search_input(self) -> Optional[Any]:
        """
        Find the main search input on the page using multiple strategies.
        Returns a Playwright locator for the search input, or None.
        """
        page = self._page

        # Strategy 1: Role-based search for textbox with search-related names
        search_names = ["search", "Search", "بحث", "q", "query", "search_query"]
        for name in search_names:
            try:
                loc = page.get_by_role("searchbox", name=name)
                if loc.count() > 0:
                    return loc.first
            except Exception:
                pass
            try:
                loc = page.get_by_role("textbox", name=name)
                if loc.count() > 0:
                    return loc.first
            except Exception:
                pass

        # Strategy 2: get_by_role searchbox without name
        try:
            loc = page.get_by_role("searchbox")
            if loc.count() > 0:
                return loc.first
        except Exception:
            pass

        # Strategy 3: Common search input selectors
        common_selectors = [
            '#sb_form_q',
            'input[name="q"]',
            'textarea[name="q"]',
            'input[name="search"]',
            'textarea[name="search"]',
            'input[type="search"]',
            'input[aria-label*="search" i]',
            'textarea[aria-label*="search" i]',
            'input[aria-label*="Search" i]',
            'textarea[aria-label*="Search" i]',
            'input[aria-label*="بحث"]',
            'textarea[aria-label*="بحث"]',
            'input[title*="search" i]',
            'textarea[title*="search" i]',
            'input[title*="Search" i]',
            'textarea[title*="Search" i]',
            'input[placeholder*="search" i]',
            'textarea[placeholder*="search" i]',
            'input[placeholder*="بحث"]',
            'textarea[placeholder*="بحث"]',
        ]
        for sel in common_selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    return loc.first
            except Exception:
                pass

        # Strategy 4: Parse ARIA snapshot for textbox/searchbox
        try:
            obs = self.observe()
            for el in obs.elements:
                if el.role in ("searchbox", "textbox") and any(
                    kw in el.name.lower() for kw in ["search", "بحث", "query"]
                ):
                    return self._resolve_target(el.name)
        except Exception:
            pass

        return None

    # ==========================================================================
    # Browser Actions (14 total)
    # ==========================================================================

    def navigate(self, url: str, wait_until: str = "domcontentloaded") -> BrowserActionResult:
        """NAVIGATE — Go to a URL and wait for page load."""
        t0 = time.perf_counter()
        try:
            self._page.goto(url, wait_until=wait_until)
            latency = (time.perf_counter() - t0) * 1000
            obs = self.observe()
            return BrowserActionResult(
                success=True,
                action="navigate",
                url=self._page.url,
                observation=obs,
                latency_ms=latency,
            )
        except Exception as e:
            return BrowserActionResult(
                success=False,
                action="navigate",
                error=str(e),
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

    def click(self, ref_or_locator: str) -> BrowserActionResult:
        """CLICK — Click an element by ARIA ref, text, or locator."""
        t0 = time.perf_counter()
        try:
            target = self._resolve_target(ref_or_locator)
            if target is None:
                # Re-observe and retry
                self.observe()
                target = self._resolve_target(ref_or_locator)

            if target is None:
                return BrowserActionResult(
                    success=False,
                    action="click",
                    error=f"Element not found: '{ref_or_locator}'",
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )

            target.click()
            latency = (time.perf_counter() - t0) * 1000
            return BrowserActionResult(
                success=True,
                action="click",
                url=self._page.url,
                latency_ms=latency,
                metadata={"target": ref_or_locator},
            )
        except Exception as e:
            return BrowserActionResult(
                success=False,
                action="click",
                error=str(e),
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

    def type_text(self, ref_or_locator: str, text: str, is_search: bool = False) -> BrowserActionResult:
        """TYPE — Type text into an element. Uses fill() for reliability."""
        t0 = time.perf_counter()
        try:
            target = self._resolve_target(ref_or_locator)
            if target is None and is_search:
                target = self._find_search_input()
            if target is None:
                self.observe()
                target = self._resolve_target(ref_or_locator)
                if target is None and is_search:
                    target = self._find_search_input()

            if target is None:
                return BrowserActionResult(
                    success=False,
                    action="type",
                    error=f"Target element not found: '{ref_or_locator}'",
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )

            target.fill(text)
            latency = (time.perf_counter() - t0) * 1000
            return BrowserActionResult(
                success=True,
                action="type",
                url=self._page.url,
                latency_ms=latency,
                metadata={"target": ref_or_locator, "text_length": len(text)},
            )
        except Exception as e:
            return BrowserActionResult(
                success=False,
                action="type",
                error=str(e),
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

    def clear(self, ref_or_locator: str) -> BrowserActionResult:
        """CLEAR — Clear element content."""
        t0 = time.perf_counter()
        try:
            target = self._resolve_target(ref_or_locator)
            if target is None:
                self.observe()
                target = self._resolve_target(ref_or_locator)

            if target is None:
                return BrowserActionResult(
                    success=False, action="clear",
                    error=f"Element not found: '{ref_or_locator}'",
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )

            target.fill("")
            return BrowserActionResult(
                success=True, action="clear",
                url=self._page.url,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            return BrowserActionResult(
                success=False, action="clear", error=str(e),
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

    def press_key(self, key: str) -> BrowserActionResult:
        """PRESS_KEY — Press a keyboard key (Enter, Tab, Escape, etc.)."""
        t0 = time.perf_counter()
        try:
            self._page.keyboard.press(key)
            return BrowserActionResult(
                success=True, action="press_key",
                url=self._page.url,
                latency_ms=(time.perf_counter() - t0) * 1000,
                metadata={"key": key},
            )
        except Exception as e:
            return BrowserActionResult(
                success=False, action="press_key", error=str(e),
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

    def scroll(self, direction: str = "down", amount: int = 3) -> BrowserActionResult:
        """SCROLL — Scroll the page. direction: up/down/left/right."""
        t0 = time.perf_counter()
        try:
            delta_map = {
                "down": (0, amount * 100),
                "up": (0, -amount * 100),
                "right": (amount * 100, 0),
                "left": (-amount * 100, 0),
            }
            dx, dy = delta_map.get(direction.lower(), (0, amount * 100))
            self._page.mouse.wheel(dx, dy)
            self._page.wait_for_timeout(200)
            return BrowserActionResult(
                success=True, action="scroll",
                url=self._page.url,
                latency_ms=(time.perf_counter() - t0) * 1000,
                metadata={"direction": direction, "amount": amount},
            )
        except Exception as e:
            return BrowserActionResult(
                success=False, action="scroll", error=str(e),
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

    def go_back(self) -> BrowserActionResult:
        """BACK — Navigate back."""
        t0 = time.perf_counter()
        try:
            self._page.go_back(wait_until="domcontentloaded")
            return BrowserActionResult(
                success=True, action="back",
                url=self._page.url,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            return BrowserActionResult(
                success=False, action="back", error=str(e),
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

    def go_forward(self) -> BrowserActionResult:
        """FORWARD — Navigate forward."""
        t0 = time.perf_counter()
        try:
            self._page.go_forward(wait_until="domcontentloaded")
            return BrowserActionResult(
                success=True, action="forward",
                url=self._page.url,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            return BrowserActionResult(
                success=False, action="forward", error=str(e),
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

    def reload(self) -> BrowserActionResult:
        """RELOAD — Reload the current page."""
        t0 = time.perf_counter()
        try:
            self._page.reload(wait_until="domcontentloaded")
            return BrowserActionResult(
                success=True, action="reload",
                url=self._page.url,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            return BrowserActionResult(
                success=False, action="reload", error=str(e),
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

    def wait(self, condition: str = "load", timeout_ms: float = 5000, selector: Optional[str] = None) -> BrowserActionResult:
        """WAIT — Wait for a condition: load, networkidle, timeout, or selector."""
        t0 = time.perf_counter()
        try:
            if condition == "selector" and selector:
                self._page.wait_for_selector(selector, timeout=timeout_ms)
            elif condition in ("load", "domcontentloaded", "networkidle"):
                self._page.wait_for_load_state(condition, timeout=timeout_ms)
            else:
                self._page.wait_for_timeout(timeout_ms)

            return BrowserActionResult(
                success=True, action="wait",
                url=self._page.url,
                latency_ms=(time.perf_counter() - t0) * 1000,
                metadata={"condition": condition},
            )
        except Exception as e:
            return BrowserActionResult(
                success=False, action="wait", error=str(e),
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

    def extract(self, scope: str = "text") -> BrowserActionResult:
        """
        EXTRACT — Extract page content.
        scope: "text" (inner text), "title", "url", "full" (observation + text).
        Sensitive content is automatically redacted.
        """
        t0 = time.perf_counter()
        try:
            if scope == "title":
                extracted = self._page.title()
            elif scope == "url":
                extracted = self._page.url
            elif scope == "full":
                obs = self.observe()
                inner_text = self._page.inner_text("body")
                # Truncate to reasonable size
                if len(inner_text) > 5000:
                    inner_text = inner_text[:5000] + "\n... [truncated]"
                extracted = redact_sensitive_text(inner_text)
                return BrowserActionResult(
                    success=True, action="extract",
                    url=self._page.url,
                    extracted_text=extracted,
                    observation=obs,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )
            else:
                inner_text = self._page.inner_text("body")
                if len(inner_text) > 5000:
                    inner_text = inner_text[:5000] + "\n... [truncated]"
                extracted = redact_sensitive_text(inner_text)

            return BrowserActionResult(
                success=True, action="extract",
                url=self._page.url,
                extracted_text=extracted,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            return BrowserActionResult(
                success=False, action="extract", error=str(e),
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
