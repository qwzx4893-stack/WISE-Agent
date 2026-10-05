"""Safe YAML contracts and actual optional pySigma worker, without live log data."""
import json
from pathlib import Path
from contextlib import contextmanager
import pytest

from core.intelligence.sigma_worker import load_documents, checked_path, MAX_INPUT

RULE = """title: Synthetic WISE Sigma QA
id: 88f4d8f3-39c0-4862-97f4-198c4c0c7a12
status: test
logsource:
  category: process_creation
  product: windows
detection:
  selection:
    Image|endswith: '\\synthetic.exe'
  condition: selection
level: low
"""


@pytest.mark.parametrize("text", ["title: a\ntitle: b", "x: !!python/object/apply:os.system ['echo bad']",
    "x: &x [a]\ny: *x", "42", "", "x: " + "[" * 21 + "0" + "]" * 21,
    "---\ntitle: a\n" * 33, "x: [" + ",".join(["1"] * 10001) + "]"])
def test_yaml_budgets_and_unsafe_forms_rejected(text):
    import yaml
    with pytest.raises((ValueError, yaml.YAMLError)):
        load_documents(text)


def test_yaml_plain_rule_loaded_without_executing():
    assert load_documents(RULE)[0]["title"] == "Synthetic WISE Sigma QA"


@pytest.mark.parametrize("path", ["outside.yaml", "sessions/private.yaml", "memory/a.yaml", ".tooling/private.yaml", "rule.txt"])
def test_worker_path_boundaries(tmp_path, path):
    root = tmp_path / "workspace"; root.mkdir()
    target = (tmp_path / path) if path == "outside.yaml" else (root / path)
    target.parent.mkdir(parents=True, exist_ok=True); target.write_text(RULE)
    with pytest.raises(ValueError): checked_path(root.resolve(), str(target))


def test_worker_oversize_rule_rejected(tmp_path):
    target = tmp_path / "large.yaml"; target.write_bytes(b"x" * (MAX_INPUT + 1))
    with pytest.raises(ValueError): checked_path(tmp_path.resolve(), str(target))


@pytest.fixture
def upstream_worker(monkeypatch, tmp_path):
    from core.paths import REPO_ROOT
    import core.security.optional_tools as optional
    import core.intelligence.sigma_adapter as adapter
    import core.tools_bridge as bridge
    cache = Path(REPO_ROOT) / ".tooling" / "sigma-qa-tools"
    manager = optional.OptionalToolManager(cache, ttl_seconds=3600)
    python = manager._path("sigma") / "Scripts" / "python.exe"
    if not python.is_file():
        python = manager._path("sigma") / "bin" / "python"
    if not python.is_file(): pytest.skip("Reviewed optional pySigma QA environment is not installed; tests never download implicitly")
    @contextmanager
    def lease(identifier):
        assert identifier == "sigma"
        yield python
    monkeypatch.setattr(optional, "OptionalToolManager", lambda: type("Lease", (), {"lease": staticmethod(lease)})())
    monkeypatch.setattr(bridge, "WORKSPACE_DIR", tmp_path)
    return tmp_path, adapter.inspect_sigma


def test_actual_upstream_rule_validation_and_no_rule_body(upstream_worker):
    root, inspect = upstream_worker
    (root / "rule.yaml").write_text(RULE, encoding="utf-8")
    result = inspect("rule.yaml")
    assert result["success"] and result["valid"] and result["total"] == 1
    assert result["parser_version"] == "1.5.1"
    assert result["records"][0]["detection_count"] == 1
    assert len(result["content_sha256"]) == 64
    assert "synthetic.exe" not in json.dumps(result)


def test_actual_yaml_date_and_canonical_router_adapter(upstream_worker):
    root, inspect = upstream_worker
    from core.intelligence.inventory import profile
    from core.intelligence.bindings import register_intelligence
    assert profile("sigma")["status"] == "ON_DEMAND"
    (root / "dated.yaml").write_text(RULE + "date: 2026-10-04\n", encoding="utf-8")
    descriptors = []
    class Router:
        def register(self, descriptor): descriptors.append(descriptor)
    register_intelligence(Router())
    descriptor = next(item for item in descriptors if item.id == "intelligence.sigma")
    result = descriptor.execution_adapter({"action": "inspect", "path": "dated.yaml"})
    assert result["valid"] and result["resource_id"] == "sigma"


@pytest.mark.parametrize("text", [RULE.replace("condition: selection", "condition: unknown_selector"),
    RULE.replace("condition: selection", "condition: selection and"), RULE.replace("Image|endswith", "Image|jq"),
    RULE.replace("status: test", "status: invalid_status"), RULE.replace("logsource:", "invalid_logsource:")])
def test_actual_upstream_bad_rules_are_not_valid(upstream_worker, text):
    root, inspect = upstream_worker
    (root / "bad.yaml").write_text(text, encoding="utf-8")
    result = inspect("bad.yaml")
    assert result["success"] and not result["valid"]
    assert result["records"][0]["diagnostics"]
    assert "synthetic.exe" not in json.dumps(result)


def test_actual_worker_rejects_unsafe_yaml(upstream_worker):
    root, inspect = upstream_worker
    (root / "unsafe.yaml").write_text("!!python/object/apply:os.system ['echo bad']")
    with pytest.raises(ValueError, match="rejected"):
        inspect("unsafe.yaml")


def test_all_documents_validated_even_when_output_limited(upstream_worker):
    root, inspect = upstream_worker
    (root / "multi.yaml").write_text(RULE + "\n---\n" + RULE.replace("condition: selection", "condition: missing"))
    result = inspect("multi.yaml", limit=1)
    assert result["total"] == 2 and result["limited"] and not result["valid"]
    assert len(result["records"]) == 1


def test_missing_optional_dependency_never_becomes_success(monkeypatch, tmp_path):
    import core.security.optional_tools as optional
    import core.tools_bridge as bridge
    from core.intelligence.sigma_adapter import inspect_sigma
    monkeypatch.setattr(bridge, "WORKSPACE_DIR", tmp_path)
    (tmp_path / "rule.yaml").write_text(RULE)
    @contextmanager
    def unavailable(identifier):
        raise RuntimeError("Required optional package unavailable")
        yield
    monkeypatch.setattr(optional, "OptionalToolManager", lambda: type("Lease", (), {"lease": staticmethod(unavailable)})())
    with pytest.raises(RuntimeError, match="unavailable"):
        inspect_sigma("rule.yaml")


@pytest.mark.parametrize("fault", ["valid", "records", "total", "limited", "version", "digest", "diagnostics", "conflict"])
def test_invalid_worker_output_is_rejected(fault):
    from core.intelligence.sigma_adapter import validated_result
    result = {"success": True, "resource_id": "sigma", "valid": True,
        "records": [{"document": 1, "valid": True, "diagnostics": []}], "total": 1, "limited": False,
        "parser": "pysigma", "parser_version": "1.5.1", "content_sha256": "a" * 64,
        "coverage": "OFFLINE_SIGMA_PARSE_AND_CONDITION_VALIDATION_ONLY"}
    if fault == "valid": result["valid"] = "true"
    if fault == "records": result["records"] = []
    if fault == "total": result["total"] = True
    if fault == "limited": result["limited"] = True
    if fault == "version": result["parser_version"] = "unknown"
    if fault == "digest": result["content_sha256"] = "unknown"
    if fault == "diagnostics": result["records"][0]["diagnostics"] = ["private input: secret"]
    if fault == "conflict": result["valid"] = False
    with pytest.raises(ValueError): validated_result(result)
