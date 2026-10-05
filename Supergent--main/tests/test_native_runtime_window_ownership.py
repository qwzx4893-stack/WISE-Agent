"""Native launcher ownership contracts with no OS calls or runtime writes.

Only the two functions under test are compiled from the application AST. This
avoids importing the launcher's file-backed logger or starting a native host.
These mocks are safety regressions, not native acceptance.
"""
import ast
import ctypes
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def launcher(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "wise_desktop.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    names = {"_acquire_native_window_mutex", "_reveal_existing_native_window",
             "_native_window_property_name", "_native_window_user32", "_native_window_runtime_pid",
             "_register_native_window_owner", "_remove_native_window_owner", "_launch_pywebview"}
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in selected} == names
    module = ModuleType("isolated_native_window_contract")
    module.sys = SimpleNamespace(platform="win32")
    module.os = SimpleNamespace(getpid=lambda: 2020, environ={})
    module._native_transparency_enabled = lambda: False
    module._apply_windows_backdrop = lambda window: True
    module.DesktopAPI = lambda: object()
    module.WINDOW_WIDTH, module.WINDOW_HEIGHT = 1440, 900
    module.MIN_WIDTH, module.MIN_HEIGHT = 1024, 640
    module.LOG = logging.getLogger("test.native.window.ownership")
    module.WINDOW_TITLE = "WISE — General Conversational Computer Agent"
    module._native_mutex_handle = None
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), module.__dict__)

    calls = []
    windows = []
    properties = {}
    after_enum = []
    after_show = []
    handle = (1 << 40) + 701
    kernel = SimpleNamespace(
        last_error=0,
        CreateMutexW=lambda security, owner, name: calls.append(("mutex", name)) or handle,
        GetLastError=lambda: kernel.last_error,
        CloseHandle=lambda handle: calls.append(("close_mutex", handle)),
    )
    def get_pid(hwnd, out):
        out._obj.value = {101: 1010, 202: 2020}.get(hwnd, 0)
        return 1 if out._obj.value else 0

    def set_property(hwnd, name, pid):
        calls.append(("set_property", hwnd, name, pid))
        properties[(hwnd, name)] = pid
        return True

    def remove_property(hwnd, name):
        calls.append(("remove_property", hwnd, name))
        return properties.pop((hwnd, name), 0)

    user32 = SimpleNamespace(
        IsWindow=lambda hwnd: any(item[0] == hwnd for item in windows),
        GetWindowThreadProcessId=get_pid,
        SetPropW=set_property,
        GetPropW=lambda hwnd, name: properties.get((hwnd, name), 0),
        RemovePropW=remove_property,
    )
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: user32, raising=False)
    win32gui = ModuleType("win32gui")
    def enum_windows(callback, extra):
        for hwnd, _runtime in windows:
            callback(hwnd, extra)
        for action in after_enum:
            action()

    def show_window(hwnd, command):
        calls.append(("show", hwnd, command))
        if after_show:
            after_show.pop(0)()

    win32gui.EnumWindows = enum_windows
    win32gui.GetWindowText = lambda hwnd: module.WINDOW_TITLE
    win32gui.ShowWindow = show_window
    win32gui.SetForegroundWindow = lambda hwnd: calls.append(("foreground", hwnd))
    win32con = ModuleType("win32con")
    win32con.SW_RESTORE = 9
    win32con.SW_SHOW = 5
    paths = ModuleType("core.paths")
    paths.RUNTIME_ROOT = Path("synthetic-runtime-a")
    for name, stub in {"win32gui": win32gui, "win32con": win32con, "core.paths": paths}.items():
        monkeypatch.setitem(sys.modules, name, stub)
    return SimpleNamespace(module=module, calls=calls, windows=windows, kernel=kernel, paths=paths,
                           properties=properties, user32=user32, gui=win32gui, handle=handle,
                           after_enum=after_enum, after_show=after_show)


def test_distinct_runtimes_have_distinct_mutex_names_without_native_actions(launcher):
    assert launcher.module._acquire_native_window_mutex() is True
    launcher.paths.RUNTIME_ROOT = Path("synthetic-runtime-b")
    assert launcher.module._acquire_native_window_mutex() is True
    names = [call[1] for call in launcher.calls if call[0] == "mutex"]
    assert len(names) == 2 and names[0] != names[1]
    assert all(name.startswith("Local\\WISEDesktopNativeWindow-") for name in names)
    assert not any(call[0] in {"show", "foreground"} for call in launcher.calls)


def test_existing_mutex_restores_only_the_matching_runtime_window(launcher):
    launcher.kernel.last_error = 183
    launcher.windows[:] = [(101, "synthetic-runtime-b"), (202, "synthetic-runtime-a")]
    property_name = launcher.module._native_window_property_name()
    launcher.properties[(101, property_name + "-other-runtime")] = 1010
    launcher.properties[(202, property_name)] = 2020
    assert launcher.module._acquire_native_window_mutex() is False
    assert ("close_mutex", launcher.handle) in launcher.calls
    assert ("show", 202, 9) in launcher.calls
    assert ("foreground", 202) in launcher.calls
    assert not any(call[0] in {"show", "foreground"} and call[1] == 101 for call in launcher.calls)


def test_existing_mutex_must_not_activate_foreign_runtime_window(launcher):
    launcher.kernel.last_error = 183
    launcher.windows[:] = [(101, "synthetic-runtime-b"), (202, "synthetic-runtime-a")]
    launcher.module._acquire_native_window_mutex()
    assert not any(call[0] in {"show", "foreground"} and call[1] == 101 for call in launcher.calls)


def test_mutex_handle_has_pointer_sized_contract_and_is_not_truncated(launcher):
    from ctypes import wintypes
    assert launcher.module._acquire_native_window_mutex() is True
    assert launcher.module._native_mutex_handle == launcher.handle > 2**32
    assert launcher.kernel.CreateMutexW.restype is wintypes.HANDLE
    assert launcher.kernel.CloseHandle.argtypes == [wintypes.HANDLE]


def test_title_match_with_wrong_pid_tag_cannot_activate(launcher):
    launcher.windows[:] = [(101, "synthetic-runtime-b")]
    launcher.properties[(101, launcher.module._native_window_property_name())] = 2020
    assert launcher.module._reveal_existing_native_window() is False
    assert launcher.calls == []


def test_ownership_changed_after_enumeration_prevents_all_actions(launcher):
    launcher.windows[:] = [(202, "synthetic-runtime-a")]
    launcher.properties[(202, launcher.module._native_window_property_name())] = 2020
    launcher.after_enum.append(launcher.properties.clear)
    assert launcher.module._reveal_existing_native_window() is False
    assert launcher.calls == []


@pytest.mark.parametrize("after_action", [1, 2])
def test_ownership_rechecked_before_each_subsequent_action(launcher, after_action):
    launcher.windows[:] = [(202, "synthetic-runtime-a")]
    launcher.properties[(202, launcher.module._native_window_property_name())] = 2020
    launcher.after_show[:] = [lambda: None] * (after_action - 1) + [launcher.properties.clear]
    assert launcher.module._reveal_existing_native_window() is False
    assert len([call for call in launcher.calls if call[0] == "show"]) == after_action
    assert not any(call[0] == "foreground" for call in launcher.calls)


def test_native_handle_registration_and_owned_cleanup(launcher):
    launcher.windows[:] = [(202, "synthetic-runtime-a")]
    window = SimpleNamespace(native=SimpleNamespace(Handle=SimpleNamespace(ToInt64=lambda: 202)))
    assert launcher.module._register_native_window_owner(window) == 202
    assert launcher.properties[(202, launcher.module._native_window_property_name())] == 2020
    assert launcher.module._remove_native_window_owner(202) is True
    assert launcher.properties == {}


def test_foreign_native_handle_registration_never_sets_a_property(launcher):
    launcher.windows[:] = [(101, "synthetic-runtime-b")]
    window = SimpleNamespace(native=SimpleNamespace(Handle=SimpleNamespace(ToInt64=lambda: 101)))
    assert launcher.module._register_native_window_owner(window) == 0
    assert launcher.calls == []


@pytest.mark.parametrize("destroyed", [False, True])
def test_cleanup_does_not_remove_foreign_or_destroyed_window_tag(launcher, destroyed):
    launcher.windows[:] = [] if destroyed else [(101, "synthetic-runtime-b")]
    launcher.properties[(101, launcher.module._native_window_property_name())] = 1010
    assert launcher.module._remove_native_window_owner(101) is False
    assert launcher.calls == []


def test_restore_log_does_not_claim_success_when_no_owned_window_exists(launcher, caplog):
    launcher.kernel.last_error = 183
    launcher.windows[:] = [(101, "synthetic-runtime-b")]
    with caplog.at_level(logging.INFO, logger=launcher.module.LOG.name):
        assert launcher.module._acquire_native_window_mutex() is False
    assert "duplicate launch skipped" in caplog.text
    assert "restored it" not in caplog.text


@pytest.mark.parametrize("fails_after_shown", [False, True])
def test_shown_registration_and_closed_or_exception_cleanup_are_connected(launcher, monkeypatch, fails_after_shown):
    class Event:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, callback):
            self.handlers.append(callback)
            return self

        def fire(self):
            for callback in self.handlers:
                callback()

    launcher.windows[:] = [(202, "synthetic-runtime-a")]
    events = SimpleNamespace(shown=Event(), closing=Event(), closed=Event())
    window = SimpleNamespace(native=SimpleNamespace(Handle=SimpleNamespace(ToInt64=lambda: 202)), events=events)
    webview = ModuleType("webview")
    webview.create_window = lambda **kwargs: window

    def start(**kwargs):
        assert launcher.properties == {}
        events.shown.fire()
        assert launcher.properties[(202, launcher.module._native_window_property_name())] == 2020
        if fails_after_shown:
            raise RuntimeError("mock host error")
        events.closed.fire()
        assert launcher.properties == {}

    webview.start = start
    monkeypatch.setitem(sys.modules, "webview", webview)
    assert launcher.module._launch_pywebview("http://127.0.0.1:1/app/index.html") is not fails_after_shown
    assert launcher.properties == {}
    assert len([call for call in launcher.calls if call[0] == "remove_property"]) == 1


def test_native_property_api_handles_are_pointer_sized(launcher):
    from ctypes import wintypes
    api = launcher.module._native_window_user32()
    assert api.SetPropW.argtypes == [wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE]
    assert api.GetPropW.restype is api.RemovePropW.restype is wintypes.HANDLE


def test_missing_title_match_does_not_act_on_any_window(launcher):
    assert launcher.module._reveal_existing_native_window() is False
    assert launcher.calls == []


def test_non_windows_reveal_does_not_import_native_apis_or_activate(launcher):
    launcher.module.sys.platform = "linux"
    launcher.windows[:] = [(101, "synthetic-runtime-b")]
    assert launcher.module._reveal_existing_native_window() is False
    assert launcher.calls == []
