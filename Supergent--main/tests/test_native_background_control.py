"""WebView2 clear-color binding without launching or activating a window."""
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def dotnet_stub(monkeypatch):
    system = ModuleType("System")
    drawing = ModuleType("System.Drawing")
    system.Action = lambda callback: callback
    drawing.Color = SimpleNamespace(FromArgb=lambda *args: args)
    monkeypatch.setitem(sys.modules, "System", system)
    monkeypatch.setitem(sys.modules, "System.Drawing", drawing)


@pytest.mark.parametrize("direct", [True, False])
def test_clear_color_changes_actual_control_on_invoke(dotnet_stub, direct):
    from wise_desktop import _keep_windows_renderer_opaque
    control = SimpleNamespace(DefaultBackgroundColor=None)
    wrapper = SimpleNamespace(webview=control)
    invoked = []

    def invoke(callback):
        assert control.DefaultBackgroundColor is None
        invoked.append(True)
        callback()

    native = SimpleNamespace(browser=wrapper, Invoke=invoke)
    if direct:
        native.webview = control
    _keep_windows_renderer_opaque(native)
    assert invoked == [True]
    assert control.DefaultBackgroundColor == (255, 16, 16, 20)
    assert not hasattr(wrapper, "DefaultBackgroundColor")


def test_missing_control_fails_without_assigning_to_python_wrapper(dotnet_stub):
    from wise_desktop import _keep_windows_renderer_opaque
    wrapper = SimpleNamespace()
    native = SimpleNamespace(browser=wrapper, Invoke=lambda action: action())
    with pytest.raises(RuntimeError, match="WebView2 control"):
        _keep_windows_renderer_opaque(native)
    assert not hasattr(wrapper, "DefaultBackgroundColor")


def test_invoke_failure_propagates(dotnet_stub):
    from wise_desktop import _keep_windows_renderer_opaque
    control = SimpleNamespace(DefaultBackgroundColor=None)

    def disposed(_):
        raise RuntimeError("disposed host")

    native = SimpleNamespace(webview=control, Invoke=disposed)
    with pytest.raises(RuntimeError, match="disposed host"):
        _keep_windows_renderer_opaque(native)
    assert control.DefaultBackgroundColor is None
