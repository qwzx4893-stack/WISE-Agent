"""Native bridge fixtures: no desktop, browser, network, or external processes."""
import ast
import logging
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self

    def fire(self):
        return [callback() for callback in self.handlers]


class Window:
    def __init__(self, url):
        self.url = url
        self.events = SimpleNamespace(**{name: Event() for name in
            ("shown", "closing", "closed", "before_load", "loaded")})
        self._js_api = None
        self._functions = {}
        self.results = []
        self.done = threading.Event()
        self.destroyed = False
        self.url_error = False
        self.url_reads = 0

    def expose(self, *functions):
        self._functions.update({func.__name__: func for func in functions})

    def get_current_url(self):
        self.url_reads += 1
        if self.url_error:
            raise RuntimeError("fixture renderer unavailable")
        return self.url

    def evaluate_js(self, result):
        self.results.append(result)
        self.done.set()

    def destroy(self):
        self.destroyed = True


@pytest.fixture
def launcher(monkeypatch):
    from webview.util import js_bridge_call
    # Compile only the bridge and launcher functions; top-level launcher code
    # owns file logging/environment setup and is deliberately never imported.
    source = Path(__file__).resolve().parents[1] / "wise_desktop.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    selected = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
                and node.name in {"DesktopAPI", "_launch_pywebview"}]
    module = ModuleType("isolated_native_bridge_launcher")
    module.os = SimpleNamespace(environ={})
    module.sys = SimpleNamespace(platform="fixture")
    module.Path = Path
    module.LOG = logging.getLogger("test.native.bridge")
    module.WINDOW_TITLE = "fixture"
    module.WINDOW_WIDTH, module.WINDOW_HEIGHT = 1000, 800
    module.MIN_WIDTH, module.MIN_HEIGHT = 100, 100
    module._native_transparency_enabled = lambda: False
    module._register_native_window_owner = lambda window: 0
    module._remove_native_window_owner = lambda hwnd: None
    module._apply_windows_backdrop = lambda window: None
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), module.__dict__)
    calls = []
    monkeypatch.setattr(module.DesktopAPI, "open_external_url", lambda self, url: calls.append(url) or True)
    state = SimpleNamespace(module=module, calls=calls, url="http://127.0.0.1:49123/app/index.html",
                            dispatch=js_bridge_call)
    window = Window(state.url)
    webview = ModuleType("webview")
    def create(**kwargs):
        state.created = kwargs
        window._js_api = kwargs.get("js_api")
        return window
    def start(**kwargs):
        window.events.before_load.fire()
        window.events.loaded.fire()
    webview.create_window, webview.start = create, start
    monkeypatch.setitem(sys.modules, "webview", webview)
    state.window = window
    return state


def launch(state):
    assert state.module._launch_pywebview(state.url)
    return state.window


def test_launcher_has_no_reflective_api_and_only_explicit_capability(launcher):
    window = launch(launcher)
    assert launcher.created.get("js_api") is None
    assert window._js_api is None
    assert set(window._functions) == {"open_external_url"}


def test_dynamic_loopback_backend_and_https_authorization_are_allowed(launcher):
    window = launch(launcher)
    assert window._functions["open_external_url"]("https://auth.example.invalid/authorize?fixture=1") is True
    assert launcher.calls == ["https://auth.example.invalid/authorize?fixture=1"]


@pytest.mark.parametrize("url", ["https://evil.example.invalid/", "http://127.0.0.1:49124/app/",
    "https://127.0.0.1:49123/app/", "http://localhost:49123/app/", "file:///fixture.html",
    "data:text/html,fixture", "http://127.0.0.1:49123.evil.invalid/", "http://user@127.0.0.1:49123/app/",
    "http://127.0.0.1:49123\n/app/", None])
def test_off_origin_or_unavailable_current_document_denies_external_open(launcher, url):
    window = launch(launcher)
    window.url = url
    with pytest.raises(PermissionError):
        window._functions["open_external_url"]("https://auth.example.invalid/")
    assert launcher.calls == []


def test_current_url_getter_failure_denies_before_capability_call(launcher):
    window = launch(launcher)
    window.url_error = True
    with pytest.raises(PermissionError):
        window._functions["open_external_url"]("https://auth.example.invalid/")
    assert launcher.calls == []


def test_before_load_revokes_already_bound_callback_until_verified_load(launcher):
    window = launch(launcher)
    bound = window._functions["open_external_url"]
    reads_before = window.url_reads
    window.events.before_load.fire()
    assert window.url_reads == reads_before
    with pytest.raises(PermissionError):
        bound("https://auth.example.invalid/")
    assert launcher.calls == []
    window.events.loaded.fire()
    assert bound("https://auth.example.invalid/") is True


def test_closed_window_revokes_previously_bound_callback(launcher):
    window = launch(launcher)
    bound = window._functions["open_external_url"]
    window.events.closed.fire()
    with pytest.raises(PermissionError):
        bound("https://auth.example.invalid/")
    assert launcher.calls == []


def test_load_revocation_during_url_check_denies_before_external_open(launcher):
    window = launch(launcher)
    def changing_document():
        window.events.before_load.fire()
        return launcher.url
    window.get_current_url = changing_document
    with pytest.raises(PermissionError):
        window._functions["open_external_url"]("https://auth.example.invalid/")
    assert launcher.calls == []


@pytest.mark.parametrize("failure", [False, True])
def test_untrusted_load_or_renderer_failure_closes_and_permanently_revokes(launcher, failure):
    window = launch(launcher)
    bound = window._functions["open_external_url"]
    window.events.before_load.fire()
    window.url = "https://evil.example.invalid/"
    window.url_error = failure
    window.events.loaded.fire()
    assert window.destroyed
    window.url, window.url_error = launcher.url, False
    window.events.loaded.fire()
    with pytest.raises(PermissionError):
        bound("https://auth.example.invalid/")
    assert launcher.calls == []


def test_installed_dispatcher_cannot_follow_exposed_function_globals(launcher, monkeypatch):
    # Import only the library utility; never create_window or start a renderer.
    monkeypatch.setitem(globals(), "BRIDGE_FIXTURE_MARKER", "benign-private-globals-fixture")
    window = launch(launcher)
    launcher.dispatch(window, "open_external_url.__globals__.get", ["BRIDGE_FIXTURE_MARKER"], "fixture")
    assert not window.done.wait(0.1)
    assert window.results == []
    assert launcher.calls == []


def test_installed_dispatcher_runs_only_explicit_guarded_function(launcher):
    window = launch(launcher)
    launcher.dispatch(window, "open_external_url", ["https://auth.example.invalid/"], "fixture")
    assert window.done.wait(2)
    assert launcher.calls == ["https://auth.example.invalid/"]
    assert "isError" not in window.results[-1]


def test_installed_dispatcher_off_origin_call_fails_without_opening_browser(launcher):
    window = launch(launcher)
    window.url = "https://evil.example.invalid/"
    launcher.dispatch(window, "open_external_url", ["https://auth.example.invalid/"], "fixture")
    assert window.done.wait(2)
    assert launcher.calls == []
    assert "isError: true" in window.results[-1]
    assert "PermissionError" in window.results[-1]


@pytest.mark.parametrize("url", ["https://evil.example.invalid/", "http://0.0.0.0:8765/app/",
                                 "file:///fixture", "http://user@127.0.0.1:8765/app/"])
def test_untrusted_initial_backend_does_not_receive_capability(launcher, url):
    launcher.url, launcher.window.url = url, url
    assert launcher.module._launch_pywebview(url) is False
    assert launcher.window._js_api is None
    assert launcher.window._functions == {}
    assert launcher.calls == []


def test_installed_object_dispatch_can_reach_benign_nested_global(launcher, monkeypatch):
    # Reproduce only harmless string retrieval, independent of the app fix.
    # No browser or runtime bridge exploit is attempted.
    monkeypatch.setitem(globals(), "BRIDGE_FIXTURE_MARKER", "benign-private-globals-fixture")
    window = Window(launcher.url)
    window._js_api = SimpleNamespace(open_external_url=lambda url: True)
    launcher.dispatch(window, "open_external_url.__globals__.get", ["BRIDGE_FIXTURE_MARKER"], "fixture")
    assert window.done.wait(2)
    assert "benign-private-globals-fixture" in window.results[-1]


def test_external_browser_primitive_keeps_https_validation_and_argv(monkeypatch, tmp_path):
    source = Path(__file__).resolve().parents[1] / "wise_desktop.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    desktop_api = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DesktopAPI")
    module = ModuleType("isolated_brave_primitive")
    module.Path, module.os = Path, SimpleNamespace(environ={})
    exec(compile(ast.Module(body=[desktop_api], type_ignores=[]), str(source), "exec"), module.__dict__)
    brave = tmp_path / "fixture brave.exe"
    brave.write_bytes(b"non-executable-fixture")
    import shutil
    import subprocess
    monkeypatch.setattr(shutil, "which", lambda name: str(brave))
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv: spawned.append(argv))
    assert module.DesktopAPI().open_external_url("https://auth.example.invalid/?fixture=1") is True
    assert spawned == [[str(brave), "https://auth.example.invalid/?fixture=1"]]
    for url in ["http://auth.example.invalid/", "file:///fixture", "https://user@auth.example.invalid/"]:
        with pytest.raises(ValueError):
            module.DesktopAPI().open_external_url(url)
    assert len(spawned) == 1
