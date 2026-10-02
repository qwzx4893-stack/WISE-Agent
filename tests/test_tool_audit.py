"""Tests for the tool manifest auditor."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.tool_audit import ToolAuditor, run_audit


def _write_pack(path: Path, tools):
    path.write_text(json.dumps(tools, indent=2), encoding="utf-8")


@pytest.fixture
def packs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "packs"
    d.mkdir()
    return d


def test_audit_flags_missing_required_fields(packs_dir: Path):
    _write_pack(packs_dir / "p1.json", [
        {"name": "foo", "version": "1.0.0", "implementation_type": "cli",
         "category": "Execution", "description": "Foo tool",
         "cli_command": "foo {arg}"},
    ])
    r = ToolAuditor(packs_dir).run(apply=False)
    codes = [i["code"] for i in r["issues"]]
    # capabilities + use_cases + dependencies missing
    assert codes.count("missing_field") == 3
    assert r["fixed_count"] == 0


def test_audit_auto_fills_missing_fields(packs_dir: Path):
    _write_pack(packs_dir / "p1.json", [
        {"name": "foo", "version": "1.0.0", "implementation_type": "cli",
         "category": "Execution", "description": "Scan a target",
         "cli_command": "foo {arg}"},
    ])
    r = ToolAuditor(packs_dir).run(apply=True)
    raw = json.loads((packs_dir / "p1.json").read_text())[0]
    assert raw["dependencies"] == []
    assert len(raw["capabilities"]) >= 3
    assert len(raw["use_cases"]) >= 2
    assert "needs_review" in raw  # fixes are flagged for human review
    assert r["fixed_count"] >= 3


def test_audit_dedup_marks_older_with_replaces(packs_dir: Path):
    base = {"version": "1.0.0", "implementation_type": "cli",
            "category": "Execution", "description": "x",
            "capabilities": ["a", "b", "c"],
            "use_cases": ["u1", "u2"],
            "dependencies": [], "cli_command": "x"}
    old = {**base, "name": "dup", "version": "1.0.0"}
    new = {**base, "name": "dup", "version": "2.1.0"}
    _write_pack(packs_dir / "p_old.json", [old])
    _write_pack(packs_dir / "p_new.json", [new])
    ToolAuditor(packs_dir).run(apply=True)
    old_loaded = json.loads((packs_dir / "p_old.json").read_text())[0]
    new_loaded = json.loads((packs_dir / "p_new.json").read_text())[0]
    assert old_loaded.get("replaces") == "2.1.0"
    assert "replaces" not in new_loaded


def test_audit_capabilities_too_many_trimmed(packs_dir: Path):
    _write_pack(packs_dir / "p1.json", [{
        "name": "many", "version": "1.0.0",
        "implementation_type": "cli", "category": "Execution",
        "description": "x", "cli_command": "x",
        "capabilities": [f"c{i}" for i in range(12)],
        "use_cases": ["u1", "u2"],
        "dependencies": [],
    }])
    ToolAuditor(packs_dir).run(apply=True)
    raw = json.loads((packs_dir / "p1.json").read_text())[0]
    assert len(raw["capabilities"]) == 8


def test_audit_use_cases_too_many_trimmed(packs_dir: Path):
    _write_pack(packs_dir / "p1.json", [{
        "name": "many", "version": "1.0.0",
        "implementation_type": "cli", "category": "Execution",
        "description": "x", "cli_command": "x",
        "capabilities": ["a", "b", "c"],
        "use_cases": [f"u{i}" for i in range(9)],
        "dependencies": [],
    }])
    ToolAuditor(packs_dir).run(apply=True)
    raw = json.loads((packs_dir / "p1.json").read_text())[0]
    assert len(raw["use_cases"]) == 5


def test_audit_invalid_json_reported(packs_dir: Path):
    (packs_dir / "broken.json").write_text("{not json", encoding="utf-8")
    r = ToolAuditor(packs_dir).run(apply=False)
    assert any(i["code"] == "json_parse_error" for i in r["issues"])


def test_audit_cli_command_placeholder_first_token_is_ok(packs_dir: Path):
    _write_pack(packs_dir / "p1.json", [{
        "name": "shell", "version": "1.0.0",
        "implementation_type": "cli", "category": "Execution",
        "description": "Generic shell runner.",
        "capabilities": ["a", "b", "c"],
        "use_cases": ["u1", "u2"],
        "dependencies": [],
        "cli_command": "{command}",
    }])
    r = ToolAuditor(packs_dir).run(apply=False)
    assert all(i["code"] != "cli_command_unparseable"
               for i in r["issues"])


def test_audit_real_packs_end_to_end():
    """The bundled tools/packs/ should be issue-free except duplicates."""
    r = run_audit(apply=False)
    by_code = r["by_code"]
    # ``duplicate`` are the auditor-flagged superseded entries — they
    # carry ``replaces`` after the apply step. Anything else must be 0.
    bad = {k: v for k, v in by_code.items()
           if k not in ("duplicate",)}
    assert bad == {}, f"unexpected residual issues: {bad}"
