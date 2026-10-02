"""File errors must not be marked successful in the agent execution trace."""
import pytest


def test_strict_bridge_propagates_missing_file(monkeypatch, tmp_path):
    import core.tools_bridge as bridge
    monkeypatch.setattr(bridge, "WORKSPACE_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        bridge.call_python_builtin_strict("read_file", {"path": "missing.txt"})
    # Legacy callers keep their existing presentation contract.
    assert bridge.call_python_builtin("read_file", {"path": "missing.txt"}).startswith("❌")


def test_real_file_content_is_not_mistaken_for_a_bridge_error(monkeypatch, tmp_path):
    import core.tools_bridge as bridge
    monkeypatch.setattr(bridge, "WORKSPACE_DIR", tmp_path)
    (tmp_path / "notes.txt").write_text("❌ This is ordinary file content", encoding="utf-8")
    assert bridge.call_python_builtin_strict("read_file", {"path": "notes.txt"}) == "❌ This is ordinary file content"
