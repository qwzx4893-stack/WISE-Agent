"""WISE's launcher and web shell must be standalone and product-owned."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_wise_web_shell_is_mounted():
    from fastapi.testclient import TestClient
    from api.server import app

    response = TestClient(app).get("/app/")

    assert response.status_code == 200
    assert "Super Computer" in response.text
    assert "متصفح الجهاز" in response.text
    assert "WISE" in response.text
    assert "Idempotency-Key" in response.text


def test_launcher_opens_only_the_standalone_wise_app():
    launcher = (ROOT / "Start-WISE.ps1").read_text(encoding="utf-8")
    assert "$Port = 8765" in launcher
    assert "$desktopLauncher" in launcher
    assert "'--native-only'" in launcher
    assert "brave" not in launcher.lower()
    assert "--app=" not in launcher
    assert "uvicorn" not in launcher.lower()
    assert "opencode" not in launcher.lower()
    assert "4096" not in launcher
    assert "--load-extension" not in launcher


def test_native_launcher_leaves_initial_window_lifecycle_to_pywebview():
    desktop = (ROOT / "wise_desktop.py").read_text(encoding="utf-8")
    assert "WISE native window shown." in desktop
    assert "WISE native window closed." in desktop
    tree = ast.parse(desktop)
    launch = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_launch_pywebview")
    calls = [node for node in ast.walk(launch) if isinstance(node, ast.Call)]
    creates = [node for node in calls if ast.unparse(node.func) == "webview.create_window"]
    starts = [node for node in calls if ast.unparse(node.func) == "webview.start"]
    assert len(creates) == len(starts) == 1
    assert creates[0].lineno < starts[0].lineno
    # Start the normal pywebview event loop without a startup callback that
    # manually shows/restores the form. Cleanup may legitimately wrap start
    # in try/finally; its indentation is not part of this lifecycle contract.
    assert starts[0].args == []
    assert [keyword.arg for keyword in starts[0].keywords] == ["debug"]
    assert not any(ast.unparse(call.func) in {"window.show", "window.hide", "window.restore"} for call in calls)
    event_hooks = {
        ast.unparse(node.target)
        for node in ast.walk(launch)
        if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add)
    }
    assert {"window.events.shown", "window.events.closed"} <= event_hooks
    assert "_show_native_window" not in desktop


def test_native_launcher_defaults_to_reliable_opaque_host():
    desktop = (ROOT / "wise_desktop.py").read_text(encoding="utf-8")
    assert 'NATIVE_TRANSPARENCY_ENV = "WISE_NATIVE_TRANSPARENCY"' in desktop
    assert "transparent=transparent_host" in desktop
    assert 'background_color="#101014"' in desktop
    assert 'os.environ.get(NATIVE_TRANSPARENCY_ENV, "")' in desktop
    assert "def _apply_windows_backdrop(window: object) -> bool:" in desktop
    assert "_DWMWA_SYSTEMBACKDROP_TYPE = 38" in desktop
    assert "_DWMSBT_TRANSIENTWINDOW = 3" in desktop
    assert "_keep_windows_renderer_opaque(native)" in desktop
    assert 'control = getattr(native, "webview", None)' in desktop
    assert "control.DefaultBackgroundColor = Color.FromArgb(255, 16, 16, 20)" in desktop
    assert "native.browser.DefaultBackgroundColor =" not in desktop
    assert "margins = _Margins(7, 7, 7, 7)" in desktop
    assert "margins = _Margins(-1, -1, -1, -1)" not in desktop


def test_wise_assets_are_self_contained():
    styles = (ROOT / "ui" / "wise_web" / "styles.css").read_text(encoding="utf-8")
    script = (ROOT / "ui" / "wise_web" / "app.js").read_text(encoding="utf-8")
    shell = (ROOT / "ui" / "wise_web" / "index.html").read_text(encoding="utf-8")
    assert '"Segoe UI Variable Text"' in styles
    assert "/wise-shell-assets/" not in styles
    assert "backdrop-filter: blur(34px)" in styles
    assert 'id="coreChip"' not in shell
    assert 'id="taskPlanDock"' in shell
    assert 'id="inspector"' not in shell
    assert 'id="integrationsView"' not in shell
    assert 'data-view="integrations"' not in shell
    assert "composer-note" not in shell
    assert 'data-test-provider' in script
    assert 'id="settingsProviderForm"' in script
    assert "captureSettingsProviderDraft" in script
    assert 'id="settingsProviderSetActive"' in script
    assert "provider-directory-row" in script
    assert "PROVIDER_LOGOS" in script
    assert "openModelProvider" in script
    assert "fetchSettingsProviderModels" in script
    assert "saveLocalModelSettings" in script
    assert 'id="settingsLocalModelForm"' in script
    assert "disconnectProvider" in script
    assert 'id="settingsDescription"' in shell
    assert 'data-calibrate-mic' in script
    assert 'commandPalette' in script
    assert "function renderTaskPlan()" in script
    assert "function syncComposerState()" in script
    assert ".composer.has-content .send-button" in styles
    assert ".settings-nav-group" in styles
    assert ".settings-savebar" in styles
    assert ".model-provider-group.expanded" in styles
    assert "html[data-wise-window=\"restored\"] .app-shell" in styles
