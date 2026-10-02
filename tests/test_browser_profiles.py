"""Safety contracts for isolated versus explicit host-browser sessions."""

from __future__ import annotations

import sys
import types

import pytest

from core.brain.intent_parser import CognitiveIntentParser
from core.browser.browser_models import BrowserProfileMode, BrowserSessionConfig
from core.browser.browser_session import BrowserSession


class _FakePage:
    def __init__(self) -> None:
        self.closed = False

    def on(self, *_args) -> None:
        pass

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, pages=None) -> None:
        self.pages = pages or []
        self.closed = False
        self.new_context_called = False

    def set_default_timeout(self, _timeout) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, context: _FakeContext) -> None:
        self.contexts = [context]
        self.closed = False

    def is_connected(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


def _install_fake_playwright(monkeypatch, browser: _FakeBrowser):
    class Chromium:
        def connect_over_cdp(self, url):
            assert url == "http://127.0.0.1:9222"
            return browser

    class Playwright:
        chromium = Chromium()

        def stop(self):
            pass

    class Manager:
        def start(self):
            return Playwright()

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: Manager()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)


def test_host_cdp_borrows_existing_tabs_and_never_closes_them(monkeypatch):
    existing_page = _FakePage()
    context = _FakeContext([existing_page])
    browser = _FakeBrowser(context)
    _install_fake_playwright(monkeypatch, browser)

    session = BrowserSession(BrowserSessionConfig(profile_mode=BrowserProfileMode.HOST_CDP))
    session.start()

    assert session.page_count == 1
    assert session.get_page() is existing_page
    assert session.save_storage_state() is False

    session.close()
    assert not existing_page.closed
    assert not context.closed
    assert not browser.closed


def test_host_cdp_rejects_network_exposed_debugger():
    session = BrowserSession(BrowserSessionConfig(
        profile_mode=BrowserProfileMode.HOST_CDP,
        cdp_url="http://192.168.1.10:9222",
    ))
    with pytest.raises(RuntimeError, match="loopback"):
        session._host_cdp_url()


def test_default_session_is_isolated_and_non_persistent():
    config = BrowserSessionConfig()
    assert config.profile_mode is BrowserProfileMode.ISOLATED
    assert config.persist_storage is False


def test_explicit_device_browser_request_selects_host_cdp_only_for_browser_steps():
    result = CognitiveIntentParser().parse(
        "افتح المتصفح وابحث عن وثائق بايثون باستخدام متصفح جهازي"
    )
    assert result.steps
    browser_steps = [step for step in result.steps if step.action_type.value.startswith("browser_")]
    assert browser_steps
    assert {step.params.get("browser_mode") for step in browser_steps} == {"host_cdp"}

