# ==============================================================================
# WISE Browser Subsystem — Browser Action Dispatcher
# Adapter bridging ComputerActionType.BROWSER_* to BrowserPageAgent methods.
# Manages BrowserSession lifecycle within an orchestration cycle.
# Marks web content as untrusted. Detects sensitive fields for tier escalation.
# ==============================================================================

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from core.browser.browser_models import BrowserSessionConfig, BrowserActionResult
from core.browser.browser_session import BrowserSession
from core.browser.browser_page_agent import BrowserPageAgent, is_sensitive_field

LOG = logging.getLogger("WISE.Browser.ActionDispatcher")


class BrowserActionDispatcher:
    """
    Adapter that bridges BROWSER_* ComputerActionType actions to BrowserPageAgent.
    Receives BrowserSession via DI (not a singleton/global).
    Manages session lifecycle lazily: starts on first browser action, reuses within cycle.
    """

    def __init__(self, session: Optional[BrowserSession] = None, config: Optional[BrowserSessionConfig] = None) -> None:
        self._session = session
        self._config = config
        self._page_agent: Optional[BrowserPageAgent] = None
        self._lock = threading.RLock()

    @property
    def session(self) -> Optional[BrowserSession]:
        return self._session

    @property
    def is_active(self) -> bool:
        return self._session is not None and self._session.is_active

    def _ensure_session(self) -> BrowserSession:
        """Lazily create and start a BrowserSession if not already active."""
        with self._lock:
            if self._session is None or not self._session.is_active:
                self._session = BrowserSession(config=self._config)
                self._session.start()
            return self._session

    def _ensure_page_agent(self) -> BrowserPageAgent:
        """Ensure a page exists and return the BrowserPageAgent for it."""
        with self._lock:
            session = self._ensure_session()
            page = session.get_page()
            if page is None or page.is_closed():
                page = session.new_page()
            if self._page_agent is None or self._page_agent.page != page or page.is_closed():
                self._page_agent = BrowserPageAgent(page)
            return self._page_agent

    def dispatch(self, action_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Dispatch a BROWSER_* action to the appropriate BrowserPageAgent method.
        Returns a dict compatible with WISEHands ActionRecord.
        """
        t0 = time.perf_counter()
        result: Dict[str, Any] = {"success": False}

        try:
            agent = self._ensure_page_agent()

            if action_name == "browser_navigate":
                url = params.get("url", "")
                wait_until = params.get("wait_until", "domcontentloaded")
                res = agent.navigate(url, wait_until=wait_until)
                result = self._action_result_to_dict(res)

            elif action_name == "browser_click":
                target = params.get("target", params.get("ref", params.get("locator", "")))
                res = agent.click(target)
                result = self._action_result_to_dict(res)

            elif action_name == "browser_type":
                target = params.get("target", params.get("ref", params.get("locator", "")))
                text = params.get("text", "")
                is_search = params.get("is_search", False) or any(k in target.lower() for k in ("search", "بحث", "query", "q"))
                res = agent.type_text(target, text, is_search=is_search)
                result = self._action_result_to_dict(res)

            elif action_name == "browser_type_sensitive":
                # Sensitive typing — same method but flagged for audit
                target = params.get("target", params.get("ref", ""))
                text = params.get("text", "")
                res = agent.type_text(target, text)
                result = self._action_result_to_dict(res)
                result["is_sensitive"] = True

            elif action_name == "browser_clear":
                target = params.get("target", params.get("ref", ""))
                res = agent.clear(target)
                result = self._action_result_to_dict(res)

            elif action_name == "browser_press_key":
                key = params.get("key", "Enter")
                res = agent.press_key(key)
                result = self._action_result_to_dict(res)

            elif action_name == "browser_scroll":
                direction = params.get("direction", "down")
                amount = params.get("amount", 3)
                res = agent.scroll(direction, amount)
                result = self._action_result_to_dict(res)

            elif action_name == "browser_back":
                res = agent.go_back()
                result = self._action_result_to_dict(res)

            elif action_name == "browser_forward":
                res = agent.go_forward()
                result = self._action_result_to_dict(res)

            elif action_name == "browser_reload":
                res = agent.reload()
                result = self._action_result_to_dict(res)

            elif action_name == "browser_switch_tab":
                index = params.get("index", 0)
                session = self._ensure_session()
                ok = session.switch_page(index)
                if ok:
                    page = session.get_page(index)
                    if page:
                        self._page_agent = BrowserPageAgent(page)
                result = {"success": ok, "action": "switch_tab", "tab_index": index}

            elif action_name == "browser_new_tab":
                url = params.get("url")
                session = self._ensure_session()
                page = session.new_page(url=url)
                self._page_agent = BrowserPageAgent(page)
                result = {
                    "success": True, "action": "new_tab",
                    "url": page.url, "tab_count": session.page_count,
                }

            elif action_name == "browser_close_tab":
                session = self._ensure_session()
                ok = session.close_page()
                # Update agent to the new active page
                page = session.get_page()
                if page and not page.is_closed():
                    self._page_agent = BrowserPageAgent(page)
                else:
                    self._page_agent = None
                result = {"success": ok, "action": "close_tab", "tab_count": session.page_count}

            elif action_name == "browser_wait":
                condition = params.get("condition", "load")
                timeout = params.get("timeout_ms", 5000)
                selector = params.get("selector")
                res = agent.wait(condition, timeout, selector)
                result = self._action_result_to_dict(res)

            elif action_name == "browser_extract":
                scope = params.get("scope", "text")
                res = agent.extract(scope)
                result = self._action_result_to_dict(res)
                # Mark extracted content as untrusted
                result["is_untrusted_web_content"] = True

            elif action_name == "browser_extract_sensitive":
                # This would only be reached if SecurityGate already confirmed
                scope = params.get("scope", "text")
                res = agent.extract(scope)
                result = self._action_result_to_dict(res)
                result["is_sensitive"] = True
                result["is_untrusted_web_content"] = True

            elif action_name == "browser_observe":
                obs = agent.observe()
                result = {
                    "success": True, "action": "observe",
                    "url": obs.url, "title": obs.title,
                    "element_count": len(obs.elements),
                    "aria_snapshot_length": len(obs.aria_snapshot),
                    "is_untrusted_web_content": True,
                }

            elif action_name == "browser_close":
                self.close()
                result = {"success": True, "action": "close"}

            else:
                result = {"success": False, "error": f"Unknown browser action: {action_name}"}

        except Exception as e:
            LOG.error("BrowserActionDispatcher error for '%s': %s", action_name, e)
            result = {"success": False, "error": str(e)}

        result["latency_ms"] = (time.perf_counter() - t0) * 1000
        return result

    def check_sensitive_escalation(self, action_name: str, params: Dict[str, Any]) -> str:
        """
        Check if a browser action should be escalated to a sensitive variant.
        Returns the (possibly escalated) action name.
        """
        if action_name == "browser_type":
            target = params.get("target", params.get("ref", params.get("locator", "")))
            # Check if we can observe the target element's role/name
            try:
                agent = self._ensure_page_agent()
                obs = agent.last_observation
                if obs:
                    for el in obs.elements:
                        if target and (target in el.name or target in el.ref_id):
                            if is_sensitive_field(el.role, el.name):
                                LOG.warning("Sensitive field detected: role=%s, name=%s", el.role, el.name)
                                return "browser_type_sensitive"
            except Exception:
                pass

        elif action_name == "browser_extract":
            # Check if extraction target contains sensitive fields
            try:
                agent = self._ensure_page_agent()
                obs = agent.last_observation
                if obs:
                    for el in obs.elements:
                        if is_sensitive_field(el.role, el.name):
                            return "browser_extract_sensitive"
            except Exception:
                pass

        return action_name

    def get_session_state(self):
        """Get current browser session state for World State integration."""
        if self._session and self._session.is_active:
            return self._session.get_session_state()
        from core.browser.browser_models import BrowserSessionState
        return BrowserSessionState()

    def close(self) -> None:
        """Close the browser session and release resources."""
        with self._lock:
            if self._session:
                try:
                    self._session.close()
                except Exception as e:
                    LOG.error("Error closing browser session: %s", e)
                self._session = None
            self._page_agent = None

    @staticmethod
    def _action_result_to_dict(res: BrowserActionResult) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "success": res.success,
            "action": res.action,
        }
        if res.url:
            d["url"] = res.url
        if res.error:
            d["error"] = res.error
        if res.extracted_text is not None:
            d["extracted_text"] = res.extracted_text
        if res.observation:
            d["observation_url"] = res.observation.url
            d["observation_title"] = res.observation.title
            d["observation_elements"] = len(res.observation.elements)
        d["latency_ms"] = res.latency_ms
        if res.metadata:
            d.update(res.metadata)
        return d
