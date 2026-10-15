"""Storage security regressions using isolated files and synthetic DPAPI only."""
import base64
import importlib.util
import json
import os
import subprocess
from pathlib import Path
import sys
import types

import pytest


class FakeDPAPI:
    def CryptProtectData(self, data, *args):
        return b"fixture-protected:" + data

    def CryptUnprotectData(self, data, *args):
        if not data.startswith(b"fixture-protected:"):
            raise ValueError("fixture decryption failure")
        return "fixture", data[len(b"fixture-protected:"):]


@pytest.fixture
def stores(tmp_path, monkeypatch):
    # Import just the owned modules: core.__init__ starts unrelated subsystems.
    root = Path(__file__).resolve().parents[1]
    package = types.ModuleType("_storage_security")
    package.__path__ = [str(root / "core")]
    llm = types.ModuleType("_storage_security.llm")
    llm.__path__ = [str(root / "core" / "llm")]
    paths = types.ModuleType("_storage_security.paths")
    paths.MEMORY_DIR = tmp_path
    for name, module in [(package.__name__, package), (llm.__name__, llm),
                         (paths.__name__, paths)]:
        monkeypatch.setitem(sys.modules, name, module)
    modules = []
    for name, relative in [("_storage_security.llm.keystore", "llm/keystore.py"),
                           ("_storage_security.secrets_store", "secrets_store.py")]:
        spec = importlib.util.spec_from_file_location(name, root / "core" / relative)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules.append(module)
    keys, secrets = modules
    monkeypatch.setitem(sys.modules, "core.llm.keystore", keys)
    monkeypatch.setattr(keys, "win32crypt", FakeDPAPI())
    # Platform simulation is local to each module, never to pathlib/pytest.
    for module in modules:
        monkeypatch.setattr(module, "os", types.SimpleNamespace(**{**vars(os), "name": "nt"}))
    return keys, secrets


def make_store(stores, kind, tmp_path):
    keys, secrets = stores
    if kind == "keys":
        return keys.KeyStore(tmp_path / "keys.json", tmp_path / "keys.secret")
    return secrets.SecretStore()


def save(store, kind, value="fixture-api-key"):
    if kind == "keys":
        store.add(name="fixture", provider="openrouter", api_key=value)
    else:
        store.set("fixture", value)


@pytest.mark.parametrize("kind", ["keys", "secrets"])
def test_windows_missing_dpapi_refuses_new_secret(stores, tmp_path, monkeypatch, kind):
    store = make_store(stores, kind, tmp_path)
    monkeypatch.setattr(stores[0], "win32crypt", None)
    with pytest.raises(RuntimeError, match="DPAPI"):
        save(store, kind)
    assert not store.path.exists()


@pytest.mark.parametrize("kind", ["keys", "secrets"])
def test_failed_protection_preserves_existing_store(stores, tmp_path, monkeypatch, kind):
    store = make_store(stores, kind, tmp_path)
    save(store, kind)
    before = store.path.read_bytes()
    def fail(*args):
        raise ValueError("fixture protection failure")
    monkeypatch.setattr(stores[0].win32crypt, "CryptProtectData", fail)
    with pytest.raises(RuntimeError, match="DPAPI"):
        save(store, kind, "replacement-fixture")
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["keys", "secrets"])
@pytest.mark.parametrize("corrupt", ["{broken", "[]", '{"version":1}',
                                   '{"version":1,"keys":{},"items":[]}'])
def test_corrupt_store_is_not_overwritten(stores, tmp_path, kind, corrupt):
    store = make_store(stores, kind, tmp_path)
    store.path.write_text(corrupt, encoding="utf-8")
    with pytest.raises(RuntimeError):
        save(store, kind)
    assert store.path.read_text(encoding="utf-8") == corrupt


def test_missing_legacy_sidecar_does_not_migrate_or_create_replacement(stores, tmp_path):
    keys = stores[0]
    path = tmp_path / "keys.json"
    sidecar = tmp_path / "keys.secret"
    original = json.dumps({"version": 1, "keys": [{"name": "fixture", "api_key": "YWJj"}]})
    path.write_text(original, encoding="utf-8")
    store = keys.KeyStore(path, sidecar)
    assert path.read_text(encoding="utf-8") == original
    assert not sidecar.exists()
    assert store.get("fixture")["api_key"] == ""


def test_invalid_legacy_utf8_is_preserved_during_migration(stores, tmp_path):
    keys = stores[0]
    path, sidecar = tmp_path / "keys.json", tmp_path / "keys.secret"
    sidecar.write_bytes(b"a" * 32)
    original = json.dumps({"version": 1, "keys": [{"name": "fixture", "api_key":
                         base64.urlsafe_b64encode(keys._xor(b"\xff\xfe", b"a" * 32)).decode()}]})
    path.write_text(original, encoding="utf-8")
    keys.KeyStore(path, sidecar)
    assert path.read_text(encoding="utf-8") == original


def test_legacy_migration_roundtrip_and_missing_dpapi_read(stores, tmp_path, monkeypatch):
    keys = stores[0]
    path, sidecar = tmp_path / "keys.json", tmp_path / "keys.secret"
    sidecar.write_bytes(b"a" * 32)
    encoded = base64.urlsafe_b64encode(keys._xor(b"fixture-legacy-key", b"a" * 32)).decode()
    path.write_text(json.dumps({"version": 1, "keys": [{"name": "fixture", "api_key": encoded}]}))
    store = keys.KeyStore(path, sidecar)
    assert store.get("fixture")["api_key"] == "fixture-legacy-key"
    original = path.read_bytes()
    monkeypatch.setattr(keys, "win32crypt", None)
    store = keys.KeyStore(path, sidecar)
    assert store.get("fixture")["api_key"] == ""
    assert path.read_bytes() == original


@pytest.mark.parametrize("kind", ["keys", "secrets"])
def test_store_hardlink_rejected_without_changing_victim(stores, tmp_path, kind):
    store = make_store(stores, kind, tmp_path)
    victim = tmp_path / "victim.json"
    original = '{"version":1,"keys":[],"items":{}}'
    victim.write_text(original)
    os.link(victim, store.path)
    with pytest.raises(RuntimeError):
        save(store, kind)
    assert victim.read_text() == original


@pytest.mark.parametrize("kind", ["keys", "secrets"])
def test_fixed_temporary_alias_is_never_opened(stores, tmp_path, kind):
    store = make_store(stores, kind, tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("fixture-victim")
    os.link(victim, store.path.with_suffix(".json.tmp"))
    save(store, kind)
    assert victim.read_text() == "fixture-victim"


def test_store_and_sidecar_cannot_alias(stores, tmp_path):
    path = tmp_path / "same.json"
    with pytest.raises(RuntimeError):
        stores[0].KeyStore(path, path)
    assert not path.exists()


@pytest.mark.parametrize("kind", ["keys", "secrets"])
def test_nonwindows_legacy_roundtrip_is_compatible(stores, tmp_path, monkeypatch, kind):
    for module in stores:
        monkeypatch.setattr(module.os, "name", "posix")
    store = make_store(stores, kind, tmp_path)
    save(store, kind)
    assert store._dec(store._enc("fixture-api-key")) == "fixture-api-key"


@pytest.mark.parametrize("kind", ["keys", "secrets"])
def test_unreadable_dpapi_is_not_changed_by_unrelated_write(stores, tmp_path, kind):
    store = make_store(stores, kind, tmp_path)
    unreadable = "dpapi:" + base64.urlsafe_b64encode(b"other-user-fixture").decode()
    if kind == "keys":
        data = {"version": 2, "keys": [{"name": "inaccessible", "api_key": unreadable}]}
    else:
        data = {"version": 1, "items": {"inaccessible": unreadable}}
    store.path.write_text(json.dumps(data))
    save(store, kind)
    after = json.loads(store.path.read_text())
    assert (after["keys"][0]["api_key"] if kind == "keys" else after["items"]["inaccessible"]) == unreadable


@pytest.mark.parametrize("kind", ["keys", "secrets"])
def test_sidecar_hardlink_rejected_before_read(stores, tmp_path, kind):
    victim = tmp_path / "victim.secret"
    victim.write_bytes(b"x" * 32)
    sidecar = tmp_path / ("keys.secret" if kind == "keys" else ".secrets_secret")
    os.link(victim, sidecar)
    with pytest.raises(RuntimeError):
        make_store(stores, kind, tmp_path)
    assert victim.read_bytes() == b"x" * 32


def test_migration_protection_failure_preserves_entire_file(stores, tmp_path, monkeypatch):
    keys = stores[0]
    path, sidecar = tmp_path / "keys.json", tmp_path / "keys.secret"
    sidecar.write_bytes(b"a" * 32)
    encoded = base64.urlsafe_b64encode(keys._xor(b"fixture-legacy-key", b"a" * 32)).decode()
    original = json.dumps({"version": 1, "keys": [{"name": "fixture", "api_key": encoded}]})
    path.write_text(original)
    monkeypatch.setattr(keys.win32crypt, "CryptProtectData", lambda *args: b"")
    with pytest.raises(RuntimeError, match="DPAPI"):
        keys.KeyStore(path, sidecar)
    assert path.read_text() == original
    assert sidecar.read_bytes() == b"a" * 32


@pytest.mark.parametrize("kind", ["keys", "secrets"])
def test_windows_junction_parent_rejected(stores, tmp_path, monkeypatch, kind):
    if os.name != "nt":
        pytest.skip("Actual NTFS junction fixture requires Windows")
    victim = tmp_path / "victim-directory"
    victim.mkdir()
    junction = tmp_path / "redirected"
    created = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(victim)],
                             capture_output=True, text=True)
    if created.returncode:
        pytest.fail("Could not create isolated junction fixture: " + created.stderr)
    try:
        monkeypatch.setattr(stores[1], "MEMORY_DIR", junction)
        with pytest.raises(RuntimeError, match="links or reparse"):
            make_store(stores, kind, junction)
        assert list(victim.iterdir()) == []
    finally:
        # Remove only the validated fixture junction, never its target.
        os.rmdir(junction)


def test_failed_singleton_initialization_can_be_retried(stores, tmp_path):
    sidecar = tmp_path / ".secrets_secret"
    sidecar.write_bytes(b"broken-fixture")
    with pytest.raises(RuntimeError):
        stores[1].SecretStore()
    assert stores[1].SecretStore._instance is None
    sidecar.write_bytes(b"x" * 32)
    assert stores[1].SecretStore()._secret == b"x" * 32
