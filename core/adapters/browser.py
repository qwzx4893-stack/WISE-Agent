"""Browser adapter — Playwright over local CDP, or HTTP fallback.

Capabilities exposed as tools:

- ``browser_fetch``      simple HTML/text fetch (HTTP, no JS).
- ``browser_open``       launch Playwright, navigate, return rendered HTML.
- ``browser_screenshot`` take a PNG screenshot (returns base64).
- ``browser_extract``    return readable text + visible links.
- ``browser_status``     reports which transport is available.

Transport selection:

1. If Playwright is installed AND ``localhost:29229`` is reachable,
   we attach over CDP (Devin's persistent profile) — JS executes,
   cookies persist.
2. Else if Playwright is installed, we launch a headless Chromium.
3. Else we fall back to plain ``httpx.get`` + minimal HTML parsing.

The adapter never *prompts* — it picks the best transport silently
and reports it via ``browser_status``.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from .base import AdapterError, BaseAdapter

try:
    import httpx as _httpx  # type: ignore
except Exception:  # noqa: BLE001
    _httpx = None  # type: ignore

try:
    from playwright.sync_api import sync_playwright  # type: ignore
    _PLAYWRIGHT_OK = True
except Exception:  # noqa: BLE001
    sync_playwright = None  # type: ignore
    _PLAYWRIGHT_OK = False


_CDP_URL = "http://localhost:29229"


class BrowserAdapter(BaseAdapter):
    NAME = "browser"
    KEYSTORE_NAMES: List[str] = []
    ENV_KEYS: List[str] = ["BROWSER_CDP_URL"]
    REQUIRED: List[str] = []
    CLI_FALLBACK = False
    RATE_PER_SEC = 4.0
    RATE_BURST = 8

    # ------------------------------------------------------------------
    def is_ready(self) -> bool:
        # We're always at least HTTP-capable (httpx is in our deps).
        return True

    @property
    def transport(self) -> str:
        if _PLAYWRIGHT_OK and self._cdp_reachable():
            return "playwright_cdp"
        if _PLAYWRIGHT_OK:
            return "playwright_headless"
        return "http"

    def _cdp_url(self) -> str:
        return self.cred("BROWSER_CDP_URL") or _CDP_URL

    def _cdp_reachable(self) -> bool:
        if _httpx is None:
            return False
        try:
            r = _httpx.get(f"{self._cdp_url()}/json/version", timeout=1.0)
            return r.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Pure-HTTP fallback
    # ------------------------------------------------------------------
    def fetch(self, url: str, *, timeout: float = 15.0) -> Dict[str, Any]:
        if _httpx is None:
            raise AdapterError("browser: httpx غير متاح")

        def _do() -> Dict[str, Any]:
            try:
                with _httpx.Client(
                    timeout=timeout, follow_redirects=True,
                    headers={"User-Agent": "AgentOS-Browser/1.0"},
                ) as cli:
                    resp = cli.get(url)
            except Exception as exc:  # noqa: BLE001
                raise AdapterError(f"browser fetch: {exc}") from exc
            return {
                "status": resp.status_code,
                "url": str(resp.url),
                "html": resp.text[:500_000],
                "transport": "http",
            }
        return self._call("fetch", _do)

    # ------------------------------------------------------------------
    # Playwright
    # ------------------------------------------------------------------
    def _with_pw(self, fn: Callable[[Any], Any]) -> Any:
        if not _PLAYWRIGHT_OK:
            raise AdapterError("browser: playwright غير مثبّت")
        with sync_playwright() as pw:  # type: ignore
            try:
                if self._cdp_reachable():
                    browser = pw.chromium.connect_over_cdp(self._cdp_url())
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                else:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()
                page = context.new_page()
                try:
                    return fn(page)
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                    if not self._cdp_reachable():
                        try:
                            browser.close()
                        except Exception:
                            pass
            except Exception as exc:  # noqa: BLE001
                raise AdapterError(f"browser playwright: {exc}") from exc

    def open(self, url: str, *, wait: str = "load",
             timeout_ms: int = 15000) -> Dict[str, Any]:
        if not _PLAYWRIGHT_OK:
            return self.fetch(url)

        def _do() -> Dict[str, Any]:
            def _action(page: Any) -> Dict[str, Any]:
                page.goto(url, wait_until=wait, timeout=timeout_ms)
                title = page.title()
                html = page.content()
                return {
                    "url": page.url, "title": title,
                    "html": html[:500_000],
                    "transport": self.transport,
                }
            return self._with_pw(_action)
        return self._call("open", _do)

    def screenshot(self, url: str, *, full_page: bool = True,
                   timeout_ms: int = 15000) -> Dict[str, Any]:
        if not _PLAYWRIGHT_OK:
            raise AdapterError("browser: screenshot يحتاج playwright")

        def _do() -> Dict[str, Any]:
            def _action(page: Any) -> Dict[str, Any]:
                page.goto(url, wait_until="load", timeout=timeout_ms)
                png = page.screenshot(full_page=full_page)
                return {
                    "url": page.url,
                    "png_base64": base64.b64encode(png).decode("ascii"),
                    "bytes": len(png),
                    "transport": self.transport,
                }
            return self._with_pw(_action)
        return self._call("screenshot", _do)

    # ------------------------------------------------------------------
    # Extract readable text + visible links from a URL.
    # ------------------------------------------------------------------
    def extract(self, url: str, *, max_links: int = 50,
                timeout_ms: int = 15000) -> Dict[str, Any]:
        if _PLAYWRIGHT_OK:
            def _do_pw() -> Dict[str, Any]:
                def _action(page: Any) -> Dict[str, Any]:
                    page.goto(url, wait_until="load", timeout=timeout_ms)
                    text = page.evaluate("() => document.body && document.body.innerText || ''")
                    raw_links = page.evaluate(
                        "() => Array.from(document.links || [])"
                        ".map(a => ({href: a.href, text: (a.innerText||'').trim()}))",
                    ) or []
                    links = []
                    for ln in raw_links[: max_links * 2]:
                        href = (ln or {}).get("href", "")
                        if not href or href.startswith("javascript:"):
                            continue
                        links.append({
                            "href": href,
                            "text": (ln or {}).get("text", "")[:200],
                        })
                        if len(links) >= max_links:
                            break
                    return {
                        "url": page.url, "title": page.title(),
                        "text": (text or "")[:200_000],
                        "links": links,
                        "transport": self.transport,
                    }
                return self._with_pw(_action)
            return self._call("extract_pw", _do_pw)

        # HTTP fallback
        page = self.fetch(url)
        html = page.get("html", "")
        text = _strip_html(html)[:200_000]
        links = _extract_links(html, base_url=url, limit=max_links)
        return {
            "url": page.get("url", url), "title": _extract_title(html),
            "text": text, "links": links, "transport": "http",
        }

    # ------------------------------------------------------------------
    def status_call(self) -> Dict[str, Any]:
        return {
            "transport": self.transport,
            "playwright_installed": _PLAYWRIGHT_OK,
            "cdp_reachable": self._cdp_reachable(),
            "cdp_url": self._cdp_url(),
        }

    # ------------------------------------------------------------------
    def tools_for(self) -> Dict[str, Callable[..., Any]]:
        return {
            "browser_fetch": self.fetch,
            "browser_open": self.open,
            "browser_screenshot": self.screenshot,
            "browser_extract": self.extract,
            "browser_status": self.status_call,
        }


# --------------------------------------------------------------------------
# Tiny HTML helpers (no BeautifulSoup dependency).
# --------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                      re.IGNORECASE | re.DOTALL)


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_title(html: str) -> str:
    m = _TITLE_RE.search(html or "")
    if not m:
        return ""
    return _strip_html(m.group(1))[:300]


def _extract_links(html: str, *, base_url: str, limit: int) -> List[Dict[str, str]]:
    links: List[Dict[str, str]] = []
    seen: set = set()
    for m in _HREF_RE.finditer(html or ""):
        href = m.group(1).strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        try:
            full = urljoin(base_url, href)
        except Exception:
            continue
        if not urlparse(full).scheme.startswith("http"):
            continue
        if full in seen:
            continue
        seen.add(full)
        text = _strip_html(m.group(2))[:200]
        links.append({"href": full, "text": text})
        if len(links) >= limit:
            break
    return links


__all__ = ["BrowserAdapter"]
