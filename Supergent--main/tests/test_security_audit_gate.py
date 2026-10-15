"""The audit harness cannot turn absence, stale evidence or observations into PASS."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qa.acceptance.security_audit import evaluate, scrub, sbom, wsl_path, semgrep_command
from qa.acceptance import security_audit
from qa.acceptance import security_baseline


@pytest.mark.parametrize("user_paths", ["C:/fixture/user-site", ["C:/fixture/user-site", "C:/fixture/second-site"]])
def test_dependency_inventory_keeps_user_site_separate_from_qa(user_paths, monkeypatch):
    monkeypatch.setattr(security_audit.site, "getusersitepackages", lambda: user_paths)
    paths = security_audit.dependency_paths()
    expected = [user_paths] if isinstance(user_paths, str) else user_paths
    assert paths["runtime-user"] == [Path(path) for path in expected]
    assert paths["qa"] != paths["runtime-user"]
    assert "voice" in paths and "tts" in paths


def test_omitted_user_site_audit_blocks_release():
    result = evaluate([], "same", "same")
    assert "missing-stage:pip-audit-runtime-user" in result["blockers"]


def test_history_scope_stamp_has_no_ref_names_or_git_contents(monkeypatch):
    monkeypatch.setattr(security_audit.shutil, "which", lambda name: "fixture-git")
    replies = iter(["a" * 40, "refs/heads/private-fixture-name " + "a" * 40, "12"])
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=next(replies))
    monkeypatch.setattr(security_audit.subprocess, "run", run)
    result = security_audit.history_identity()
    assert result["status"] == "VERIFIED" and result["reachable_commit_count"] == 12
    assert "private-fixture-name" not in json.dumps(result)
    assert all(command[1] == "--no-pager" for command in commands)


@pytest.mark.parametrize("key", ["head", "refs_sha256", "reachable_commit_count", "ref_count"])
def test_changed_history_identity_blocks_acceptance(key):
    initial = {"status": "VERIFIED", "head": "a" * 40, "refs_sha256": "b" * 64,
               "reachable_commit_count": 12, "ref_count": 2}
    final = {**initial, key: "changed"}
    assert security_audit.history_blockers(initial, final)


def test_unverified_git_history_is_not_equivalent_to_same_snapshot():
    assert security_audit.history_blockers({"status": "UNVERIFIED"}, {"status": "UNVERIFIED"})


def test_empty_audit_is_not_ready():
    result = evaluate([], "same", "same")
    assert result["release_verdict"] == "NOT_READY"
    assert "No scans executed" in result["blockers"]
    assert "missing-stage:semgrep" in result["blockers"]


def test_skips_and_missing_windows_evidence_never_pass():
    result = evaluate([{"id": "bandit", "status": "SKIP"}], "same", "same")
    assert "stage:bandit" in result["blockers"]
    assert "unverified:official-uac" in result["blockers"]


def test_changed_source_or_open_finding_blocks_release():
    result = evaluate([{"id": "bandit", "status": "PASS"}], "before", "after",
                      findings=[{"id": "synthetic", "status": "UNRESOLVED"}])
    assert "Source changed during audit" in result["blockers"]
    assert "finding:synthetic" in result["blockers"]


def test_native_gate_needs_actual_owned_hashed_evidence():
    result = evaluate([], "same", "same", external_gates=[{
        "id": "official-uac", "status": "PASS", "source_sha256": "same",
        "evidence_sha256": "madeup", "evidence_path": "not-present.json"}])
    assert "unverified:official-uac" in result["blockers"]


def test_scanner_source_and_secret_snippets_are_removed():
    result = scrub({"results": [{"rule": "fixture", "Secret": "SYNTHETIC_KEY",
                    "Match": "PRIVATE_SYNTHETIC_TEXT", "code": "PRIVATE_SOURCE",
                    "extra": {"lines": "PRIVATE_SOURCE", "metavars": {"x": "PRIVATE_SOURCE"}},
                    "line": 20}]})
    assert result["results"][0] == {"rule": "fixture", "extra": {}, "line": 20}


def test_source_inventory_includes_model_runtime_not_weights(tmp_path, monkeypatch):
    app = tmp_path / "app"
    code = app / "core/models/runtime.py"
    code.parent.mkdir(parents=True)
    code.write_text("# fixture model runtime\n")
    (app / "config").mkdir()
    weights = app / "models/weights.bin"
    weights.parent.mkdir()
    weights.write_bytes(b"synthetic weights, not source")
    monkeypatch.setattr(security_baseline, "APP", app)
    monkeypatch.setattr(security_baseline, "WORKSPACE", tmp_path)
    assert code in security_baseline.source_files()
    assert weights not in security_baseline.source_files()


@pytest.mark.parametrize("name,version", [("package\n--index-url=https://external.invalid", "1.0"),
                                         ("package", "1.0\n--extra-index-url=https://external.invalid"),
                                         ("package", "1.0; extra == 'private'"),
                                         ("package", "https://external.invalid/package.whl")])
def test_sbom_metadata_cannot_inject_pip_requests(tmp_path, monkeypatch, name, version):
    fixture = SimpleNamespace(metadata={"Name": name}, version=version)
    monkeypatch.setattr(security_audit.importlib.metadata, "distributions", lambda **kwargs: [fixture])
    with pytest.raises(ValueError):
        sbom(tmp_path, "fixture", [tmp_path])
    assert not list(tmp_path.iterdir())


def test_sbom_preserves_normal_names_and_versions(tmp_path, monkeypatch):
    fixture = SimpleNamespace(metadata={"Name": "fixture_package"}, version="1.2.3+local.1")
    monkeypatch.setattr(security_audit.importlib.metadata, "distributions", lambda **kwargs: [fixture])
    pins, count = sbom(tmp_path, "fixture", [tmp_path])
    assert count == 1
    assert pins.read_text().strip() == "fixture_package==1.2.3+local.1"
    assert json.loads((tmp_path / "sbom-fixture.json").read_text())["components"][0]["purl"].startswith("pkg:pypi/")


def test_security_demo_has_no_default_target_or_embedded_cookie():
    script = Path(__file__).resolve().parents[1] / "skills/security/pentest-vuln-verify/1.0.0/scan_demo.sh"
    contents = script.read_text(encoding="utf-8")
    assert "WISE_SCAN_DEMO_TARGET:?" in contents
    assert 'COOKIE="${WISE_SCAN_DEMO_COOKIE:-}"' in contents
    assert "https://" not in contents and "http://" not in contents


def test_remediation_example_can_be_analyzed_without_running_it():
    script = (Path(__file__).resolve().parents[1] /
              "skills/general/public-skills/1.0.0/plugins/vulnerability-remediation/scripts/scan_and_remediate.py")
    tree = ast.parse(script.read_text(encoding="utf-8"))
    assert any(isinstance(node, ast.FunctionDef) and node.name == "create_remediation_prompt"
               for node in tree.body)


@pytest.mark.parametrize("path", [r"\\server\share\code", r"C:relative", "relative/file",
                                  r"C:\workspace\..\private", "C:\\nul\x00"])
def test_wsl_scanner_rejects_ambiguous_or_network_paths(path):
    with pytest.raises(ValueError):
        wsl_path(path)


def test_wsl_path_preserves_spaces_without_shell_interpolation():
    assert wsl_path(r"C:\workspace\folder with spaces\example.py") == (
        "/mnt/c/workspace/folder with spaces/example.py")


def test_native_scanner_uses_only_local_rules_and_disables_telemetry(tmp_path):
    command = semgrep_command("trusted-scanner", tmp_path, tmp_path)
    assert command[0] == "trusted-scanner"
    assert "--metrics=off" in command and "--disable-version-check" in command
    assert "--no-git-ignore" in command and command[-1] == str(tmp_path)
    assert command[command.index("--config") + 1].endswith("security_rules.yml")


def test_wsl_backend_is_not_implicitly_selected_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(security_audit, "os", SimpleNamespace(name="posix"))
    with pytest.raises(ValueError):
        semgrep_command(None, tmp_path, tmp_path, use_wsl=True)
