from contextlib import contextmanager
import json
import time

import pytest

from core.security.optional_tools import OptionalToolManager, RECIPES


def test_unknown_recipe_never_creates_files(tmp_path):
    manager = OptionalToolManager(tmp_path / "cache")
    with pytest.raises(ValueError):
        with manager.lease("../../anything"): pass
    assert not manager.root.exists()


def test_idle_removal_only_affects_owned_cache(tmp_path):
    manager = OptionalToolManager(tmp_path / "cache", ttl_seconds=1)
    path = manager._path("bandit"); path.mkdir(parents=True)
    (path / "wise-install.json").write_text(json.dumps({"last_used": time.time() - 10}))
    unrelated = tmp_path / "user-files"; unrelated.mkdir(); (unrelated / "keep.txt").write_text("keep")
    manager.cleanup_idle()
    assert not path.exists() and (unrelated / "keep.txt").read_text() == "keep"


def test_explicit_remove_rejects_symlink(tmp_path):
    manager = OptionalToolManager(tmp_path / "cache")
    assert manager.remove("bandit")["scope"] == "WISE_OWNED_CACHE_ONLY"
    with pytest.raises(ValueError): manager.remove("pip")


def test_recipe_is_visible_as_on_demand_not_globally_installed():
    from core.intelligence.inventory import profile
    for identifier in RECIPES:
        row = profile(identifier)
        assert row["status"] == "ON_DEMAND" and row["available"] and row["actions"] == ["inspect"]


def test_zero_retention_removes_after_lease(tmp_path, monkeypatch):
    manager = OptionalToolManager(tmp_path / "cache", ttl_seconds=0)
    path = manager._path("yara")
    def install(_):
        path.mkdir(); (path / "wise-install.json").write_text(json.dumps({"last_used": time.time()}))
        return path / "python"
    monkeypatch.setattr(manager, "_install", install)
    with manager.lease("yara") as python: assert python == path / "python"
    assert not path.exists()


def test_zero_retention_removes_after_failed_tool(tmp_path, monkeypatch):
    manager = OptionalToolManager(tmp_path / "cache", ttl_seconds=0)
    path = manager._path("yara")
    def install(_):
        path.mkdir(); (path / "wise-install.json").write_text(json.dumps({"last_used": time.time()}))
        return path / "python"
    monkeypatch.setattr(manager, "_install", install)
    with pytest.raises(RuntimeError):
        with manager.lease("yara"): raise RuntimeError("tool failed")
    assert not path.exists()
