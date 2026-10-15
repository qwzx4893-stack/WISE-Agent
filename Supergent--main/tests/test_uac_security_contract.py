"""UAC launch contract tests: every Windows API is replaced; no elevation occurs."""

import ntpath
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.windows import uac


@pytest.fixture
def windows_api(monkeypatch):
    captured = []
    loaded = []
    system_directory = {"path": r"C:\Windows\System32", "result": None}

    def get_directory(buffer, capacity):
        path = system_directory["path"]
        buffer.value = path
        return system_directory["result"] if system_directory["result"] is not None else len(path)

    def launch(pointer):
        info = pointer._obj
        captured.append({"file": info.lpFile, "arguments": info.lpParameters,
                         "directory": info.lpDirectory, "verb": info.lpVerb,
                         "show": info.nShow, "mask": info.fMask})
        info.hProcess = 33
        return True

    kernel = SimpleNamespace(GetSystemDirectoryW=Mock(side_effect=get_directory),
                             GetProcessId=Mock(return_value=4321))
    shell = SimpleNamespace(ShellExecuteExW=Mock(side_effect=launch))

    def load(name, **kwargs):
        loaded.append((name, dict(kwargs)))
        if name.lower() in ("kernel32", "kernel32.dll"):
            return kernel
        if name.lower() in ("shell32", "shell32.dll"):
            return shell
        raise AssertionError("Unexpected DLL load in synthetic test")

    monkeypatch.setattr(uac.ctypes, "WinDLL", load, raising=False)
    monkeypatch.setattr(uac.ctypes, "get_last_error", lambda: 5, raising=False)
    return SimpleNamespace(captured=captured, system_directory=system_directory,
                           kernel=kernel, shell=shell, loaded=loaded)


@pytest.mark.parametrize("operation_id, executable, arguments", [
    ("system_file_check", "sfc.exe", "/scannow"),
    ("windows_image_repair", "DISM.exe", "/Online /Cleanup-Image /RestoreHealth"),
    ("flush_dns", "ipconfig.exe", "/flushdns"),
])
def test_launch_uses_api_system_directory_despite_hostile_environment(
        windows_api, monkeypatch, tmp_path, operation_id, executable, arguments):
    monkeypatch.chdir(tmp_path)
    for name in ("PATH", "SystemRoot", "WINDIR", "SYSTEMROOT", "windir"):
        monkeypatch.setenv(name, str(tmp_path / "attacker"))
    result = uac.UacBroker()._launch_runas(uac._OPERATIONS[operation_id])
    assert result == uac.UacLaunch(process_handle=33, process_id=4321)
    assert windows_api.captured == [{
        "file": ntpath.join(r"C:\Windows\System32", executable),
        "arguments": arguments, "directory": r"C:\Windows\System32",
        "verb": "runas", "show": uac._SW_SHOWNORMAL,
        "mask": uac._SEE_MASK_NOCLOSEPROCESS | uac._SEE_MASK_NOASYNC,
    }]
    windows_api.kernel.GetSystemDirectoryW.assert_called_once()
    assert all(options.get("winmode") == 0x00000800 for _, options in windows_api.loaded)


@pytest.mark.parametrize("directory", [
    "", "System32", "C:System32", r"\Windows\System32",
    r"\\attacker\share\System32", r"C:\Windows\System32\..\attacker",
    r"C:\Windows\Temp", "%SystemRoot%\\System32",
])
def test_untrusted_or_nonabsolute_directory_fails_before_launch(windows_api, directory):
    windows_api.system_directory["path"] = directory
    with pytest.raises(OSError):
        uac.UacBroker()._launch_runas(uac._OPERATIONS["flush_dns"])
    windows_api.shell.ShellExecuteExW.assert_not_called()


@pytest.mark.parametrize("result", [0, 32768, 32769, 1])
def test_failed_truncated_or_inconsistent_api_result_has_no_fallback(windows_api, result):
    windows_api.system_directory["result"] = result
    with pytest.raises(OSError):
        uac.UacBroker()._launch_runas(uac._OPERATIONS["flush_dns"])
    windows_api.shell.ShellExecuteExW.assert_not_called()


def test_unavailable_windows_api_has_no_environment_fallback(windows_api, monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\attacker")
    del windows_api.kernel.GetSystemDirectoryW
    with pytest.raises(OSError):
        uac.UacBroker()._launch_runas(uac._OPERATIONS["flush_dns"])
    windows_api.shell.ShellExecuteExW.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"executable": r"C:\attacker\ipconfig.exe"},
    {"executable": "..\\ipconfig.exe"},
    {"arguments": "/flushdns & attacker.exe"},
    {"id": "arbitrary_operation"},
])
def test_only_canonical_fixed_catalogue_entry_can_launch(windows_api, changes):
    forged = replace(uac._OPERATIONS["flush_dns"], **changes)
    with pytest.raises(OSError):
        uac.UacBroker()._launch_runas(forged)
    windows_api.shell.ShellExecuteExW.assert_not_called()


def test_intent_expiry_and_wrong_operation_are_existing_negative_controls(monkeypatch):
    broker = uac.UacBroker()
    monkeypatch.setattr(broker, "supported", lambda: True)
    clock = {"now": 1000.0}
    monkeypatch.setattr(uac.time, "time", lambda: clock["now"])
    expired = broker.issue_intent("flush_dns", ttl_seconds=15)["intent_token"]
    clock["now"] += 15
    assert not broker._consume_intent("flush_dns", expired)
    token = broker.issue_intent("flush_dns")["intent_token"]
    assert not broker._consume_intent("system_file_check", token)
    assert not broker._consume_intent("flush_dns", token)


def test_original_windows_prompt_and_process_contract_is_existing_positive_control(windows_api):
    result = uac.UacBroker()._launch_runas(uac._OPERATIONS["flush_dns"])
    assert result == uac.UacLaunch(process_handle=33, process_id=4321)
    captured = windows_api.captured[0]
    assert captured["verb"] == "runas" and captured["show"] == uac._SW_SHOWNORMAL
    assert captured["arguments"] == "/flushdns"
    assert captured["mask"] == uac._SEE_MASK_NOCLOSEPROCESS | uac._SEE_MASK_NOASYNC


def test_system_directory_is_derived_from_api_for_alternate_installation(windows_api):
    windows_api.system_directory["path"] = r"D:\Windows Installation\System32"
    uac.UacBroker()._launch_runas(uac._OPERATIONS["system_file_check"])
    assert windows_api.captured[0]["file"] == r"D:\Windows Installation\System32\sfc.exe"
    assert windows_api.captured[0]["directory"] == r"D:\Windows Installation\System32"
