# ==============================================================================
# WISE Browser Subsystem — Browser Session (Playwright Lifecycle Manager)
# Manages a single Playwright browser context lifecycle.
# Default: Microsoft Edge, headed. Headless only for tests/CI.
# NOT a singleton — instantiated per-session via dependency injection.
# ==============================================================================

from __future__ import annotations

import os
import hashlib
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from core.browser.browser_models import (
    BrowserSessionConfig,
    BrowserProfileMode,
    BrowserSessionState,
    DownloadRecord,
    DownloadState,
)
from core.browser.browser_dialog_handler import BrowserDialogHandler

LOG = logging.getLogger("WISE.Browser.Session")

# Default download directory under WISE workspace
_WISE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEFAULT_DOWNLOAD_DIR = _WISE_ROOT / "downloads"
_BRAVE_PATHS = (
    Path(os.environ.get("WISE_BRAVE_PATH", "")),
    Path(r"C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"),
    Path(r"C:\\Program Files (x86)\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
)


class BrowserSession:
    """
    Playwright browser lifecycle manager for WISE.
    Owns the browser instance, context, and pages.
    NOT a singleton — created per orchestration session and passed via DI.
    """

    def __init__(self, config: Optional[BrowserSessionConfig] = None) -> None:
        self.config = config or BrowserSessionConfig()

        # Override headless from environment for tests/CI
        env_headless = os.environ.get("WISE_BROWSER_HEADLESS", "").strip()
        if env_headless == "1":
            self.config.headless = True

        # Controlled download directory
        self._download_dir = Path(self.config.download_dir or str(_DEFAULT_DOWNLOAD_DIR))
        self._download_dir.mkdir(parents=True, exist_ok=True)

        # Playwright objects (initialized in start())
        self._playwright = None
        self._browser = None
        self._context = None
        self._pages: List = []
        # A host CDP session borrows the user's browser.  It must never close
        # the browser, its context, or tabs that existed before WISE attached.
        self._owns_browser = True
        self._owns_context = True

        # State tracking
        self._lock = threading.RLock()
        self._active = False
        self._downloads: List[DownloadRecord] = []

        # Dialog handler
        self.dialog_handler = BrowserDialogHandler()

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def download_dir(self) -> Path:
        return self._download_dir

    @property
    def downloads(self) -> List[DownloadRecord]:
        with self._lock:
            return list(self._downloads)

    def _launch_kwargs(self) -> Dict:
        """Build safe Playwright options, resolving Brave explicitly.

        Playwright supports Chrome/Edge as channels, but Brave is a Chromium
        derivative rather than a recognised channel.  It must therefore be
        started via its installed executable, never silently replaced by Edge.
        """
        requested = (self.config.channel or "brave").strip().lower()
        kwargs: Dict = {"headless": self.config.headless}
        if self.config.executable_path:
            executable = Path(self.config.executable_path).expanduser()
            if not executable.is_file():
                raise RuntimeError(f"Configured browser executable does not exist: {executable}")
            kwargs["executable_path"] = str(executable)
            return kwargs
        if requested == "brave":
            for candidate in _BRAVE_PATHS:
                if str(candidate) and candidate.is_file():
                    kwargs["executable_path"] = str(candidate)
                    return kwargs
            resolved = shutil.which("brave") or shutil.which("brave.exe")
            if resolved:
                kwargs["executable_path"] = resolved
                return kwargs
            raise RuntimeError(
                "Brave was requested but was not found. Set WISE_BRAVE_PATH or "
                "BrowserSessionConfig(executable_path=...)."
            )
        if requested in {"chrome", "msedge", "chromium"}:
            kwargs["channel"] = requested
            return kwargs
        raise RuntimeError(f"Unsupported browser channel '{self.config.channel}'")

    def _host_cdp_url(self) -> str:
        endpoint = (
            self.config.cdp_url
            or os.environ.get("WISE_BRAVE_CDP_URL")
            or "http://127.0.0.1:9222"
        ).rstrip("/")
        parsed = urlparse(endpoint)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError(
                "Host-browser access only accepts a loopback CDP endpoint. "
                "Use http://127.0.0.1:9222 (never a network-exposed debugger)."
            )
        return endpoint

    def _attach_host_browser(self) -> None:
        """Attach to the user's existing local Brave context without owning it."""
        if self.config.headless:
            raise RuntimeError("host_cdp mode requires a visible local browser; headless is not supported")
        endpoint = self._host_cdp_url()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(endpoint)
        except Exception as exc:
            raise RuntimeError(
                "Could not attach to your Brave session. Start Brave with "
                "--remote-debugging-port=9222, then request host-browser mode again."
            ) from exc
        if not self._browser.contexts:
            raise RuntimeError("The local Brave CDP endpoint has no browser context to attach to.")
        self._context = self._browser.contexts[0]
        self._pages = list(self._context.pages)
        for page in self._pages:
            try:
                page.on("dialog", self.dialog_handler.handle_dialog)
                page.on("download", self._handle_download)
            except Exception:
                LOG.debug("Could not register handlers on an existing host-browser page", exc_info=True)
        self._owns_browser = False
        self._owns_context = False
        LOG.info("Attached to local host browser through loopback CDP: %s", endpoint)

    def start(self) -> None:
        """Launch the configured Playwright browser."""
        with self._lock:
            if self._active:
                LOG.warning("BrowserSession already active.")
                return

            try:
                from playwright.sync_api import sync_playwright

                self._playwright = sync_playwright().start()

                if self.config.profile_mode == BrowserProfileMode.HOST_CDP:
                    self._attach_host_browser()
                else:
                    launch_kwargs = self._launch_kwargs()
                    self._browser = self._playwright.chromium.launch(**launch_kwargs)

                    # Create a disposable WISE-owned context.  Cookie storage is
                    # opt-in; old workspace state is never silently reloaded.
                    context_kwargs: Dict = {
                        "accept_downloads": True,
                        "viewport": {
                            "width": self.config.viewport_width,
                            "height": self.config.viewport_height,
                        },
                    }
                    if self.config.user_agent:
                        context_kwargs["user_agent"] = self.config.user_agent
                    if self.config.persist_storage:
                        storage_path = _WISE_ROOT / "browser_storage_state.json"
                        if storage_path.exists() and storage_path.stat().st_size > 0:
                            context_kwargs["storage_state"] = str(storage_path)
                            LOG.info("Restoring explicitly enabled WISE storage state")
                    self._context = self._browser.new_context(**context_kwargs)
                    self._context.set_default_timeout(self.config.default_timeout_ms)

                self._active = True
                LOG.info(
                    "BrowserSession started: channel=%s, headless=%s, download_dir=%s",
                    self.config.channel,
                    self.config.headless,
                    self._download_dir,
                )

            except Exception as e:
                LOG.error("Failed to start BrowserSession: %s", e)
                self._cleanup_playwright()
                raise

    @property
    def page(self):
        """Active page (convenience property)."""
        return self.get_page()

    def get_page(self, index: int = -1):
        """Get a page by index. Returns the last-opened page by default. Auto-creates initial page if empty."""
        with self._lock:
            if not self._active or not self._context:
                return None
            if not self._pages:
                try:
                    return self.new_page()
                except Exception as ex:
                    LOG.error("Failed to auto-create initial browser page: %s", ex)
                    return None
            try:
                return self._pages[index]
            except IndexError:
                return None

    def new_page(self, url: Optional[str] = None):
        """Create a new page (tab) in the browser context."""
        with self._lock:
            if not self._active or not self._context:
                raise RuntimeError("BrowserSession is not active. Call start() first.")

            page = self._context.new_page()

            # Register dialog handler
            page.on("dialog", self.dialog_handler.handle_dialog)

            # Register download handler
            page.on("download", self._handle_download)

            self._pages.append(page)

            if url:
                page.goto(url, wait_until="domcontentloaded")

            LOG.info("New browser page created. Total pages: %d", len(self._pages))
            return page

    def close_page(self, index: int = -1) -> bool:
        """Close a specific page/tab."""
        with self._lock:
            if not self._pages:
                return False
            try:
                page = self._pages.pop(index)
                if not page.is_closed():
                    page.close()
                LOG.info("Browser page closed. Remaining pages: %d", len(self._pages))
                return True
            except (IndexError, Exception) as e:
                LOG.error("Error closing page: %s", e)
                return False

    def switch_page(self, index: int) -> bool:
        """Switch active page to the given tab index."""
        with self._lock:
            if not self._pages or index < 0 or index >= len(self._pages):
                return False
            page = self._pages[index]
            if not page.is_closed():
                page.bring_to_front()
                return True
            return False

    @property
    def page_count(self) -> int:
        with self._lock:
            return len(self._pages)

    def get_session_state(self) -> BrowserSessionState:
        """Get current browser session state for World State integration."""
        with self._lock:
            if not self._active or not self._pages:
                return BrowserSessionState(is_active=self._active)

            current_page = self._pages[-1] if self._pages else None
            return BrowserSessionState(
                is_active=self._active,
                current_url=current_page.url if current_page and not current_page.is_closed() else "",
                current_title=current_page.title() if current_page and not current_page.is_closed() else "",
                tab_count=len(self._pages),
                active_tab_index=len(self._pages) - 1,
                pending_dialogs=self.dialog_handler.pending_dialogs,
                active_downloads=[d for d in self._downloads if d.state == DownloadState.STARTED],
            )

    def navigate(self, url: str, wait_until: str = "domcontentloaded") -> Dict[str, Any]:
        """Navigate active page to url, auto-starting session if inactive."""
        self.ensure_alive()
        with self._lock:
            page = self.get_page()
            if page is None or page.is_closed():
                page = self.new_page()
            try:
                resp = page.goto(url, wait_until=wait_until)
                return {
                    "success": True,
                    "url": page.url,
                    "status": resp.status if resp else 200,
                }
            except Exception as exc:
                LOG.error("Browser navigation to %s failed: %s", url, exc)
                return {"success": False, "error": str(exc), "url": url}

    def extract_dom(self) -> Dict[str, Any]:
        """Extract DOM / accessibility structure from active page."""
        self.ensure_alive()
        with self._lock:
            page = self.get_page()
            if page is None or page.is_closed():
                return {"elements": [], "url": "", "title": ""}
            try:
                from core.browser.browser_page_agent import BrowserPageAgent
                agent = BrowserPageAgent(page)
                obs = agent.observe()
                elements_data = [
                    {
                        "ref_id": el.ref_id,
                        "role": el.role,
                        "name": el.name,
                        "text": f"{el.role} {el.name}".strip(),
                    }
                    for el in obs.elements
                ]
                if not elements_data:
                    raw_nodes = page.evaluate("() => Array.from(document.querySelectorAll('button, a, input, h1, h2, p')).map(e => ({tag: e.tagName, text: e.innerText || e.value || ''}))")
                    elements_data = [
                        {
                            "ref_id": f"dom_{i}",
                            "role": item.get("tag", "").lower(),
                            "name": item.get("text", ""),
                            "text": item.get("text", ""),
                        }
                        for i, item in enumerate(raw_nodes)
                    ]
                return {
                    "elements": elements_data,
                    "url": obs.url,
                    "title": obs.title,
                    "aria_snapshot": obs.aria_snapshot,
                }
            except Exception as exc:
                LOG.error("extract_dom failed: %s", exc)
                return {"elements": [], "error": str(exc)}

    def _handle_download(self, download) -> None:
        """Playwright download event handler. Downloads go to controlled directory. NEVER auto-executed."""
        try:
            suggested = download.suggested_filename or f"download_{int(time.time())}"
            source_url = download.url or ""

            record = DownloadRecord(
                filename=suggested,
                url=source_url,
                download_dir=str(self._download_dir),
                state=DownloadState.STARTED,
            )

            with self._lock:
                self._downloads.append(record)

            LOG.info("Download started: '%s' from %s", suggested, source_url[:100])

            # Save to controlled download directory
            save_path = self._download_dir / suggested
            try:
                download.save_as(str(save_path))
                record.state = DownloadState.COMPLETED
                record.completed_at = time.time()
                record.file_path = str(save_path)

                # Compute SHA-256 hash for provenance
                if save_path.exists():
                    record.size_bytes = save_path.stat().st_size
                    sha256 = hashlib.sha256()
                    with open(save_path, "rb") as f:
                        for chunk in iter(lambda: f.read(8192), b""):
                            sha256.update(chunk)
                    record.sha256_hash = sha256.hexdigest()

                LOG.info(
                    "Download completed: '%s' (%d bytes, SHA256: %s)",
                    suggested,
                    record.size_bytes or 0,
                    record.sha256_hash or "N/A",
                )

            except Exception as save_err:
                record.state = DownloadState.FAILED
                record.error = str(save_err)
                LOG.error("Download failed for '%s': %s", suggested, save_err)

        except Exception as e:
            LOG.error("Error in download handler: %s", e)

    def save_storage_state(self, path: Optional[str] = None) -> bool:
        """Persists browser storage state (cookies, local/session storage) for session continuity."""
        with self._lock:
            if not self._context or not self._owns_context:
                return False
            if not path and not self.config.persist_storage:
                return False
            try:
                target_path = Path(path) if path else (_WISE_ROOT / "browser_storage_state.json")
                target_path.parent.mkdir(parents=True, exist_ok=True)
                self._context.storage_state(path=str(target_path))
                LOG.info("Browser storage state persisted to '%s'", target_path)
                return True
            except Exception as e:
                LOG.warning("Failed to persist browser storage state: %s", e)
                return False

    def close(self) -> None:
        """Close the browser session and release all resources."""
        with self._lock:
            if not self._active:
                return

            LOG.info("Closing BrowserSession...")
            if self._owns_context and self.config.persist_storage:
                try:
                    self.save_storage_state()
                except Exception as e:
                    LOG.warning("Could not auto-save storage state during close: %s", e)

            self._active = False

            # Existing user tabs belong to the host browser and must remain open.
            if self._owns_context:
                for page in self._pages:
                    try:
                        if not page.is_closed():
                            page.close()
                    except Exception as e:
                        LOG.warning("Error closing page during shutdown: %s", e)
            self._pages.clear()

            self._cleanup_playwright()
            LOG.info("BrowserSession closed cleanly.")

    def stop(self) -> None:
        """Exact alias to close() to adhere to ManagedWorker/Service lifecycle contracts."""
        self.close()

    def ensure_alive(self) -> bool:
        """Checks if browser is alive and auto-recovers if crashed or disconnected."""
        with self._lock:
            if not self._active or self._browser is None:
                LOG.info("BrowserSession inactive. Starting fresh instance...")
                self.start()
                return True
            try:
                if not self._browser.is_connected():
                    LOG.warning("Playwright browser disconnected! Performing crash recovery...")
                    self._cleanup_playwright()
                    self._active = False
                    self.start()
                    return True
                return True
            except Exception as e:
                LOG.warning("Browser connectivity check failed: %s. Re-launching...", e)
                self._cleanup_playwright()
                self._active = False
                self.start()
                return True

    def _cleanup_playwright(self) -> None:
        """Clean up Playwright resources in proper order."""
        try:
            if self._context and self._owns_context:
                self._context.close()
            self._context = None
        except Exception as e:
            LOG.warning("Error closing browser context: %s", e)

        try:
            if self._browser and self._owns_browser:
                self._browser.close()
            self._browser = None
        except Exception as e:
            LOG.warning("Error closing browser: %s", e)

        try:
            if self._playwright:
                self._playwright.stop()
                self._playwright = None
        except Exception as e:
            LOG.warning("Error stopping playwright: %s", e)

    def __del__(self):
        """Ensure cleanup on garbage collection."""
        try:
            self.close()
        except Exception:
            pass
