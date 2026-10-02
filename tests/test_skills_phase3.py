"""Tests for Phase 3 skills overhaul + file-management toolkit."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# File-management pack manifests
# ---------------------------------------------------------------------------
def test_file_mgmt_pack_present_and_valid() -> None:
    pack = json.loads(
        (REPO_ROOT / "tools/packs/pack_19_file_mgmt.json").read_text(encoding="utf-8")
    )
    names = {t["name"] for t in pack}
    expected = {
        "busybox", "toybox", "libarchive", "7z", "ripgrep", "sed", "awk",
        "file", "binwalk", "radare2", "apktool", "jadx", "apksigner",
        "zipalign", "frida", "dex2jar",
    }
    assert expected.issubset(names), f"missing: {expected - names}"
    for tool in pack:
        assert 3 <= len(tool["capabilities"]) <= 8
        assert 2 <= len(tool["use_cases"]) <= 5
        assert tool["category"] in {"Execution", "Security"}
        assert tool["dependencies"]


def test_file_mgmt_curated_map_entries() -> None:
    curated = json.loads(
        (REPO_ROOT / "core/tool_packages.json").read_text(encoding="utf-8")
    )
    cases: Dict[str, Dict[str, str]] = {
        "busybox":    {"type": "apt"},
        "ripgrep":    {"type": "apt"},
        "binwalk":    {"type": "apt"},
        "radare2":    {"type": "github_release"},
        "jadx":       {"type": "github_release"},
        "dex2jar":    {"type": "github_release"},
        "frida":      {"type": "pip"},
    }
    for name, expect in cases.items():
        entry = curated.get(name)
        assert entry, f"missing curated entry for {name}"
        assert entry["type"] == expect["type"], (name, entry)


def test_tool_installer_recognises_new_tools() -> None:
    from core.tool_installer import ToolInstaller
    inst = ToolInstaller(packs_dir=REPO_ROOT / "tools/packs")
    statuses = inst.scan(force=True)
    for name in ("busybox", "binwalk", "radare2", "jadx", "frida"):
        assert name in statuses, f"{name} not in scan output"
        st = statuses[name]
        assert st.required, f"{name} has no required deps"


# ---------------------------------------------------------------------------
# Indexer backward-compat (legacy + nested)
# ---------------------------------------------------------------------------
def _seed_skill(root: Path, *parts: str, name: str, body: str = "") -> Path:
    folder = root.joinpath(*parts)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"# {name}\n\n{body or 'demo skill'}\n",
                                     encoding="utf-8")
    (folder / "metadata.json").write_text(
        json.dumps({"name": name, "version": "1.0.0",
                    "description": body or "demo skill"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return folder


def test_indexer_supports_legacy_and_nested(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    legacy = _seed_skill(skills, "legacy-tool", "1.0.0", name="legacy-tool",
                         body="legacy skill")
    nested = _seed_skill(skills, "design", "huashu-design", "1.0.0",
                         name="huashu-design", body="huashu skill")
    archived = _seed_skill(skills, ".archive", "old", "1.0.0",
                           name="old", body="archived")

    # Reset the singleton so it picks up our temp dir.
    from core.skills import indexer as idx_mod
    idx_mod.SkillIndexer._instance = None
    import core.paths as paths_mod
    orig = paths_mod.SKILLS_DIR
    paths_mod.SKILLS_DIR = skills
    try:
        si = idx_mod.SkillIndexer()
        si.skills_dir = skills
        si._index = si._scan_skills()
        names = set(si._index.keys())
    finally:
        paths_mod.SKILLS_DIR = orig
        idx_mod.SkillIndexer._instance = None

    assert "legacy-tool" in names
    assert "huashu-design" in names
    assert si._index["huashu-design"]["metadata"].get("category") == "design"
    assert "old" not in names  # archived is skipped
    assert all(not n.startswith(".") for n in names)
    _ = legacy, nested, archived  # silence linters


# ---------------------------------------------------------------------------
# Dedup: dry-run vs apply
# ---------------------------------------------------------------------------
def test_dedup_dry_run_groups_similar(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    body = "Run trivy scan against docker images for CVE detection and report"
    _seed_skill(skills, "trivy-a", "1.0.0", name="trivy-a", body=body)
    _seed_skill(skills, "trivy-b", "1.0.0", name="trivy-b", body=body)
    _seed_skill(skills, "polars-tutor", "1.0.0", name="polars-tutor",
                body="Polars dataframe tutorials and SQL bridge")

    from core.skills.dedup import dedup_skills
    report = dedup_skills(apply=False, similarity_threshold=0.5,
                          skills_dir=skills)
    assert report["applied"] is False
    assert report["clusters"] >= 1
    keep_skills = [g["keep"] for g in report["groups"]]
    archive_skills = [s for g in report["groups"] for s in g["archive"]]
    assert any("trivy" in n for n in keep_skills)
    assert any("trivy" in n for n in archive_skills)
    assert "polars-tutor" not in archive_skills
    # Dry-run must NOT touch files.
    for child in ("trivy-a", "trivy-b", "polars-tutor"):
        assert (skills / child / "1.0.0" / "SKILL.md").exists()


def test_dedup_apply_moves_to_archive(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    body = "Run trivy scan against docker images for CVE detection and report"
    _seed_skill(skills, "trivy-a", "1.0.0", name="trivy-a", body=body)
    _seed_skill(skills, "trivy-b", "1.0.0", name="trivy-b", body=body)

    from core.skills.dedup import dedup_skills
    report = dedup_skills(apply=True, similarity_threshold=0.5,
                          skills_dir=skills)
    assert report["applied"] is True
    archive = skills / ".archive"
    assert archive.exists()
    archived_names = {p.name for p in archive.iterdir()}
    assert archived_names, "expected at least one archived skill"


# ---------------------------------------------------------------------------
# Lifecycle manager
# ---------------------------------------------------------------------------
def test_lifecycle_transitions_and_failure_threshold(tmp_path: Path) -> None:
    from core.skills.lifecycle import LifecycleManager
    lc = LifecycleManager(lifecycle_file=tmp_path / "lifecycle.json")

    rec = lc.activate("foo")
    assert rec.state == "active"
    lc.deactivate("foo")
    assert lc.get("foo").state == "inactive"
    lc.activate("foo", reason="re-enable")
    lc.record_failure("foo", error="boom")
    lc.record_failure("foo", error="boom")
    lc.record_failure("foo", error="boom")
    # Auto-flag after 3 failures.
    assert lc.get("foo").state == "failed"
    assert lc.get("foo").failure_count == 3
    lc.record_success("foo")
    assert lc.get("foo").success_count == 1


def test_lifecycle_health_check_marks_failed(tmp_path: Path) -> None:
    from core.skills.lifecycle import LifecycleManager
    lc = LifecycleManager(lifecycle_file=tmp_path / "lifecycle.json")
    skill_dir = tmp_path / "ghost"
    # Folder doesn't exist — health check should flip to failed.
    result = lc.health_check("ghost", skill_dir=skill_dir)
    assert result["ok"] is False
    assert lc.get("ghost").state == "failed"

    # Repair: create the files, re-check.
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# ghost\n", encoding="utf-8")
    repaired = lc.repair("ghost", skill_dir=skill_dir)
    assert repaired["ok"] is True
    assert repaired["state"] == "active"


def test_lifecycle_dep_hint_detection() -> None:
    from core.skills.lifecycle import _looks_like_missing_dep
    assert _looks_like_missing_dep("ModuleNotFoundError: No module named 'foo'")
    assert _looks_like_missing_dep("bash: bar: command not found")
    assert _looks_like_missing_dep("ImportError: cannot import name X")
    assert not _looks_like_missing_dep("Connection refused")
    assert not _looks_like_missing_dep("")


def test_lifecycle_auto_install_invoked_on_dep_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing-dependency error triggers ToolInstaller.install_missing()."""
    from core.skills import lifecycle as lf

    invoked: dict = {"deps": None}

    class _FakeIndexer:
        def get_skill_info(self, name: str):
            return {
                "name": name,
                "metadata": {"dependencies": ["nmap", "trivy"]},
            }

    class _FakeInstaller:
        def install_missing(self, names=None):
            invoked["deps"] = list(names or [])
            return {"installed": [{"tool": n} for n in (names or [])],
                    "failed": [], "skipped": []}

    # Patch the lazy imports inside the helper.
    import sys
    fake_indexer_mod = type(sys)("core.skills.indexer")
    fake_indexer_mod.SkillIndexer = lambda: _FakeIndexer()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.skills.indexer", fake_indexer_mod)

    fake_inst_mod = type(sys)("core.tool_installer")
    fake_inst_mod.ToolInstaller = _FakeInstaller  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.tool_installer", fake_inst_mod)

    lc = lf.LifecycleManager(lifecycle_file=tmp_path / "lifecycle.json")
    lc.record_failure("network-scan", error="bash: nmap: command not found")
    assert invoked["deps"] == ["nmap", "trivy"]


def test_first_boot_health_check_runs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first-boot health check populates lifecycle.json and is idempotent."""
    from core.skills import lifecycle as lf

    lifecycle_file = tmp_path / "lifecycle.json"
    monkeypatch.setattr(lf, "LIFECYCLE_FILE", lifecycle_file)
    monkeypatch.setattr(lf, "_DEFAULT", None)

    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# demo\n", encoding="utf-8")

    class _Indexer:
        def get_index(self):
            return {"demo": {"path": str(skill_dir), "metadata": {}}}

    import sys
    fake_idx = type(sys)("core.skills.indexer")
    fake_idx.SkillIndexer = lambda: _Indexer()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.skills.indexer", fake_idx)

    first = lf.first_boot_health_check()
    assert first["ok"] and first.get("checked") == 1
    # Re-running should short-circuit because lifecycle.json now exists.
    second = lf.first_boot_health_check()
    assert second.get("skipped") is True


# ---------------------------------------------------------------------------
# Skillnet
# ---------------------------------------------------------------------------
def test_skillnet_local_sdk_works_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK ships with a default endpoint — no env vars required."""
    monkeypatch.delenv("SKILLNET_BASE_URL", raising=False)
    monkeypatch.delenv("SKILLNET_API_KEY", raising=False)
    from core.skills import skillnet as sn

    cli = sn.SkillnetClient()
    if not sn._HAS_SDK:
        pytest.skip("skillnet-ai SDK not installed in this environment")
    assert cli.enabled is True
    assert cli.backend == "skillnet-ai"


def test_skillnet_urllib_fallback_when_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the SDK is unavailable, the urllib path needs SKILLNET_BASE_URL."""
    from core.skills import skillnet as sn

    monkeypatch.setattr(sn, "_HAS_SDK", False)
    monkeypatch.delenv("SKILLNET_BASE_URL", raising=False)
    cli = sn.SkillnetClient()
    cli._sdk_searcher = None
    cli._sdk_downloader = None
    assert cli.enabled is False
    assert cli.search("anything") == []
    res = sn.search_with_skillnet("foo", local_results=[], client=cli)
    assert res["used_skillnet"] is False


def test_skillnet_search_and_import(tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end search + import via the urllib fallback path."""
    from core.skills import skillnet as sn
    monkeypatch.setattr(sn, "_HAS_SDK", False)
    monkeypatch.setenv("SKILLNET_BASE_URL", "https://example.invalid")

    fake_search = {
        "results": [
            {"name": "demo-skill", "description": "a demo",
             "version": "1.2.3", "category": "design", "rating": 4.7}
        ]
    }
    fake_manifest = {
        "name": "demo-skill", "description": "a demo",
        "version": "1.2.3", "category": "design", "rating": 4.7,
        "skill_md": "# demo-skill\n\nbody from skillnet.\n",
    }

    calls = {"hit": []}

    def fake_get(url: str, **kw):
        calls["hit"].append(url)
        if "/skills/search" in url:
            return fake_search
        if "/skills/demo-skill" in url:
            return fake_manifest
        return None

    monkeypatch.setattr(sn, "_http_get_json", fake_get)
    cli = sn.SkillnetClient()
    cli._sdk_searcher = None
    cli._sdk_downloader = None
    assert cli.enabled  # via SKILLNET_BASE_URL fallback

    hits = cli.search("demo")
    assert [h.name for h in hits] == ["demo-skill"]

    result = cli.import_skill("demo-skill", skills_dir=tmp_path)
    assert result["ok"]
    target = tmp_path / "design" / "demo-skill" / "1.2.3"
    assert (target / "SKILL.md").exists()
    assert (target / "metadata.json").exists()


def test_skillnet_scaffold_fallback(tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from core.skills import skillnet as sn
    monkeypatch.delenv("SKILLNET_BASE_URL", raising=False)
    out = sn.scaffold_missing_skill(
        "brand-new",
        description="a hand-rolled scaffold",
        category="design",
        skills_dir=tmp_path,
        use_thinking=False,
    )
    assert out["ok"]
    folder = tmp_path / "design" / "brand-new" / "0.1.0"
    assert (folder / "SKILL.md").exists()
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert meta["draft"] is True


# ---------------------------------------------------------------------------
# Ranker + bundles
# ---------------------------------------------------------------------------
def test_ranker_prefers_relevant_skill_with_history() -> None:
    from core.skills.ranker import rank_skills
    index = {
        "trivy": {
            "name": "trivy",
            "description": "Scan docker images for CVEs and known vulns",
            "excerpt": "container security CVE pipeline",
            "category": "security",
        },
        "shadcn-ui": {
            "name": "shadcn-ui",
            "description": "React component recipes built on Tailwind",
            "excerpt": "frontend UI primitives",
            "category": "frontend",
        },
    }
    history = {
        "trivy": {"state": "active", "success_count": 5, "failure_count": 0},
        "shadcn-ui": {"state": "active", "success_count": 0, "failure_count": 0},
    }
    ranked = rank_skills(
        "scan docker container for CVE vulnerabilities",
        index=index, history=history, bundles={},
    )
    assert ranked[0].name == "trivy"
    assert ranked[0].score > ranked[1].score


def test_ranker_excludes_failed_state() -> None:
    from core.skills.ranker import rank_skills
    index = {
        "trivy": {
            "name": "trivy",
            "description": "Scan docker images for CVEs",
            "excerpt": "container security",
            "category": "security",
        },
    }
    history = {"trivy": {"state": "failed", "failure_count": 5}}
    ranked = rank_skills("scan docker for CVE", index=index, history=history)
    assert ranked == []


def test_ranker_bundle_boost_for_web_dev() -> None:
    from core.skills.ranker import rank_skills, detect_bundle, DEFAULT_BUNDLES
    index = {
        "shadcn-ui": {
            "name": "shadcn-ui",
            "description": "React shadcn primitives",
            "excerpt": "tailwind ui components",
            "category": "frontend",
        },
        "trivy": {
            "name": "trivy",
            "description": "container security scanner",
            "excerpt": "cve docker",
            "category": "security",
        },
    }
    bundle = detect_bundle("build a react frontend with tailwind")
    assert bundle == "web-development"
    ranked = rank_skills("build a react frontend with tailwind",
                         index=index, history={}, bundles=DEFAULT_BUNDLES)
    assert ranked[0].name == "shadcn-ui"
    assert ranked[0].bundle_boost > 0


def test_default_bundles_round_trip(tmp_path: Path) -> None:
    from core.skills.ranker import save_default_bundles, load_bundles
    p = save_default_bundles(path=tmp_path / "bundles.json")
    assert p.exists()
    loaded = load_bundles(path=p)
    assert "web-development" in loaded
    assert "security-audit" in loaded


# ---------------------------------------------------------------------------
# huashu-design discoverability
# ---------------------------------------------------------------------------
def test_huashu_design_skill_files_exist() -> None:
    folder = REPO_ROOT / "skills/design/huashu-design/1.0.0"
    assert (folder / "SKILL.md").exists()
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert meta["name"] == "huashu-design"
    assert meta["category"] == "design"
    assert 3 <= len(meta["capabilities"]) <= 8


# ---------------------------------------------------------------------------
# API endpoints sanity
# ---------------------------------------------------------------------------
def test_api_skills_endpoints_registered() -> None:
    from api.server import app
    paths = {r.path for r in app.routes}
    assert "/admin/skills/lifecycle" in paths
    assert "/admin/skills/{name}/activate" in paths
    assert "/admin/skills/import-from-skillnet" in paths
    assert "/admin/skills/recommend" in paths
    assert "/admin/skills/dedup" in paths
    assert "/admin/skills/optimize" in paths
    assert "/admin/skills/upload" in paths


def test_admin_skills_upload_writes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /admin/skills/upload persists SKILL.md under skills/uploaded/<slug>."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENT_OS_SKIP_FIRST_BOOT_CHECK", "1")
    skills = tmp_path / "skills"
    skills.mkdir()

    import core.paths as paths_mod
    monkeypatch.setattr(paths_mod, "SKILLS_DIR", skills)

    from api import server as srv
    monkeypatch.setattr(srv, "_require_auth", lambda *a, **k: None)

    # Avoid touching the real indexer singleton in tests.
    class _FakeIdx:
        def refresh(self):
            return None
    monkeypatch.setattr("core.skills.indexer.SkillsIndex", _FakeIdx,
                        raising=False)

    client = TestClient(srv.app)
    body = "# My Test Skill\n\nDoes a thing."
    resp = client.post("/admin/skills/upload",
                       json={"filename": "SKILL.md", "content": body})
    assert resp.status_code == 200
    out = resp.json()
    assert out["ok"] is True
    assert out["slug"] == "my-test-skill"
    target = skills / "uploaded" / "my-test-skill" / "SKILL.md"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == body


def test_admin_skills_upload_rejects_empty(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    from api import server as srv
    monkeypatch.setattr(srv, "_require_auth", lambda *a, **k: None)
    client = TestClient(srv.app)
    resp = client.post("/admin/skills/upload",
                       json={"content": "   "})
    assert resp.status_code == 400


def test_admin_optimize_endpoint_runs_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /admin/skills/optimize chains dedup + reorganize + repair."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENT_OS_SKIP_FIRST_BOOT_CHECK", "1")
    from api import server as srv

    # Disable auth header check.
    monkeypatch.setattr(srv, "_require_auth", lambda *a, **k: None)

    captured: dict = {}

    def fake_dedup(*, apply, similarity_threshold, min_quality, **_):
        captured["dedup"] = (apply, similarity_threshold, min_quality)
        return {"applied": apply, "total_skills": 0, "duplicate_skills": 0}

    def fake_plan():
        return []

    def fake_apply(moves):
        return moves

    def fake_health(force=False):
        captured["health_force"] = force
        return {"ok": True, "checked": 0}

    class _Mgr:
        def all(self):
            return {}
        def repair(self, name, **_):
            return {"ok": True}

    monkeypatch.setattr("core.skills.dedup.dedup_skills", fake_dedup)
    monkeypatch.setattr("core.skills.reorganize.plan", fake_plan)
    monkeypatch.setattr("core.skills.reorganize.apply", fake_apply)
    monkeypatch.setattr("core.skills.lifecycle.first_boot_health_check", fake_health)
    monkeypatch.setattr("core.skills.lifecycle.get_manager", lambda: _Mgr())

    client = TestClient(srv.app)
    resp = client.post("/admin/skills/optimize", json={"apply": True, "threshold": 0.9})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "dedup" in body["steps"]
    assert "reorganize" in body["steps"]
    assert "health_check" in body["steps"]
    assert captured["dedup"] == (True, 0.9, 0.0)
    assert captured["health_force"] is True
