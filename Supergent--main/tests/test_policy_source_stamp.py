"""An evidence fingerprint must cover the actual policy source, too."""

def test_policy_edit_invalidates_source_evidence(tmp_path, monkeypatch):
    from qa.acceptance import source_stamp as module
    monkeypatch.setattr(module, "ROOT", tmp_path)
    (tmp_path / "policies").mkdir()
    (tmp_path / "config").mkdir()
    for path in (tmp_path / "config/intelligence_resources.json", tmp_path / "requirements.txt",
                 tmp_path / "requirements-intelligence.txt"):
        path.write_text("fixture", encoding="utf-8")
    policy = tmp_path / "policies/policies.rego"
    policy.write_text("package fixture\ndefault allow := false", encoding="utf-8")
    before = module.source_stamp()
    policy.write_text("package fixture\ndefault allow := true", encoding="utf-8")
    assert module.source_stamp() != before
