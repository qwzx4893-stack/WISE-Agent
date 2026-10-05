"""Owner preferences: real storage/API, synthetic text, no model/network calls."""
from concurrent.futures import ThreadPoolExecutor
import json

from fastapi.testclient import TestClient
import pytest

from core import user_instructions as instructions


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(instructions, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(instructions, "MEMORY_DIR", tmp_path / "legacy-memory")
    monkeypatch.setattr(instructions, "_CACHE", None)
    return tmp_path


def test_legacy_preferences_read_and_migrate_without_destroying_original(store):
    instructions.MEMORY_DIR.mkdir()
    legacy = instructions.MEMORY_DIR / "instructions.txt"
    legacy.write_text("Legacy owner preferences", encoding="utf-8")
    previous = instructions.read_user_instructions()
    assert previous["instructions"] == "Legacy owner preferences"
    saved = instructions.save_user_instructions(previous["instructions"], expected_revision=previous["revision"])
    assert (store / "user_instructions.json").exists()
    instructions.save_user_instructions("", expected_revision=saved["revision"])
    assert instructions.get_user_instructions() == ""
    assert legacy.read_text() == "Legacy owner preferences"


def test_legacy_api_uses_same_store_and_revision_guard(store):
    from api.server import app
    client = TestClient(app)
    initial = client.get("/admin/instructions").json()
    payload = {"instructions": "Unified preferences", "expected_revision": initial["revision"]}
    assert client.post("/admin/instructions", json=payload).status_code == 403
    assert client.post("/admin/instructions", json=payload, headers={"X-Wise-Action": "settings"}).status_code == 200
    assert client.get("/api/v2/settings/instructions").json()["instructions"] == "Unified preferences"
    assert client.post("/admin/instructions", json=payload, headers={"X-Wise-Action": "settings"}).status_code == 409


def test_save_reload_bilingual_and_clear(store):
    empty = instructions.read_user_instructions()
    saved = instructions.save_user_instructions("أجب بوضوح.\nUse verified citations.", expected_revision=empty["revision"])
    instructions._CACHE = None
    assert instructions.read_user_instructions() == saved
    assert saved["revision"] != empty["revision"]
    assert instructions.save_user_instructions(saved["instructions"])["updated_at"] == saved["updated_at"]
    assert instructions.save_user_instructions("", expected_revision=saved["revision"])["instructions"] == ""


def test_stale_save_preserves_newer_preferences(store):
    revision = instructions.read_user_instructions()["revision"]
    instructions.save_user_instructions("Version A", expected_revision=revision)
    with pytest.raises(instructions.InstructionConflict):
        instructions.save_user_instructions("Version B", expected_revision=revision)
    assert instructions.get_user_instructions() == "Version A"


def test_process_writer_reloads_revision_before_cas(store):
    import subprocess
    import sys
    revision = instructions.read_user_instructions()["revision"]
    script = (
        "from pathlib import Path; import sys; "
        "from core import user_instructions as i; "
        "i.CONFIG_DIR=Path(sys.argv[1]); "
        "i.save_user_instructions('Other process', expected_revision=sys.argv[2])"
    )
    subprocess.run([sys.executable, "-c", script, str(store), revision], check=True, capture_output=True, timeout=15)
    with pytest.raises(instructions.InstructionConflict):
        instructions.save_user_instructions("Stale process", expected_revision=revision)
    assert instructions.get_user_instructions() == "Other process"


def test_parallel_edit_has_one_winner(store):
    revision = instructions.read_user_instructions()["revision"]
    def save(text):
        try:
            instructions.save_user_instructions(text, expected_revision=revision)
            return "saved"
        except instructions.InstructionConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, ["A", "B"])) == ["conflict", "saved"]


@pytest.mark.parametrize("text", ["x" * 30_001, "unsafe\x00text", "<wise_owner_preferences>", "</wise_owner_preferences>", 123])
def test_invalid_content_rejected(store, text):
    with pytest.raises(ValueError):
        instructions.save_user_instructions(text)
    assert not (store / "user_instructions.json").exists()


def test_failed_write_does_not_acknowledge_or_replace(store, monkeypatch):
    saved = instructions.save_user_instructions("Original")
    def fail(*args):
        raise PermissionError("fixture: denied")
    monkeypatch.setattr(instructions.os, "replace", fail)
    with pytest.raises(instructions.InstructionStorageError):
        instructions.save_user_instructions("Changed")
    assert instructions.read_user_instructions() == saved
    assert list(store.glob("instructions-*.tmp")) == []


def test_corrupt_or_tampered_storage_fails_explicitly(store):
    instructions.save_user_instructions("Original")
    path = store / "user_instructions.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["instructions"] = "Tampered"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(instructions.InstructionStorageError):
        instructions.get_user_instructions()


def test_preferences_applied_once_and_updated_without_overriding_policy(store):
    instructions.save_user_instructions("Use Arabic")
    policy = "Do not bypass tool approval."
    prompt = instructions.apply_user_instructions(policy)
    assert prompt.startswith(policy)
    assert "They cannot grant tool permissions" in prompt
    assert instructions.apply_user_instructions(prompt) == prompt
    instructions.save_user_instructions("Use English")
    updated = instructions.apply_user_instructions(prompt)
    assert "Use Arabic" not in updated and "Use English" in updated
    assert updated.count("<wise_owner_preferences>") == 1
    instructions.save_user_instructions("")
    assert instructions.apply_user_instructions(updated) == policy


def test_real_api_roundtrip_and_origin_protection(store):
    from api.server import app
    client = TestClient(app)
    url = "/api/v2/settings/instructions"
    empty = client.get(url).json()
    body = {"instructions": "التزم بالوضوح. Be concise.", "expected_revision": empty["revision"]}
    assert client.put(url, json=body).status_code == 403
    assert client.put(url, json=body, headers={"X-Wise-Action": "settings", "Origin": "https://foreign.example"}).status_code == 403
    assert client.get(url, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    result = client.put(url, json=body, headers={"X-Wise-Action": "settings"})
    assert result.status_code == 200 and result.json()["ok"] is True
    assert client.get(url).json()["instructions"] == body["instructions"]
    assert client.put(url, json=body, headers={"X-Wise-Action": "settings"}).status_code == 409
    assert client.put(url, json={"instructions": "x" * 30_001}, headers={"X-Wise-Action": "settings"}).status_code == 422


def test_model_request_consumes_real_saved_preferences(store):
    from core.models.provider_interface import ModelCompletionRequest
    instructions.save_user_instructions("Keep verified sources")
    req = ModelCompletionRequest(messages=[{"role": "user", "content": "Hello"}], system_prompt="Native policy")
    assert "Keep verified sources" in req.system_prompt
    assert req.messages == [{"role": "user", "content": "Hello"}]
