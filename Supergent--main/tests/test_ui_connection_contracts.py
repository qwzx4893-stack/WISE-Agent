"""UI connection contracts. External credential/send boundaries are stubbed."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
def test_messaging_logos_are_local_and_do_not_require_connection_broker(isolated):
    from core.channels.service_catalog import services
    root = Path(__file__).resolve().parents[1]/"ui/wise_web"
    for row in services():
        assert row["icon_url"].startswith("/app/assets/")
        assert (root/row["icon_url"].removeprefix("/app/")).is_file()
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    import core.paths as paths
    import core.secrets_store as secrets
    import core.session_service as sessions
    from core.channels import unified
    monkeypatch.setattr(paths, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(secrets, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(secrets.SecretStore, "_instance", None)
    ss = sessions.SessionService(tmp_path / "sessions")
    monkeypatch.setattr(sessions, "get_session_service", lambda: ss)
    monkeypatch.setattr(unified.ChannelRegistry, "_instance", None)
    monkeypatch.delenv("NANGO_SECRET_KEY", raising=False)
    monkeypatch.delenv("NANGO_BASE_URL", raising=False)
    return ss


def connect_telegram():
    from core.channels.service_catalog import configure
    return configure("tgram", "telegram_test", {"bot_token": "123456:fixture_only_no_live_token_123456", "targets": "12345"})


def test_messaging_discovery_is_curated_but_legacy_registry_is_not_deleted(isolated):
    from core.channels.service_catalog import services, _all_services, MESSAGING_APPS
    actual = services()
    assert {app["id"] for app in actual} == set(MESSAGING_APPS)
    assert all(not row["supports_incoming"] for row in actual)
    assert {"jira", "windows", "bsky", "mailtos", "chime", "guilded"}.isdisjoint(row["id"] for row in actual)
    assert len(_all_services()) > len(actual)


def test_saving_connection_creates_durable_platform_thread_without_sending(isolated, monkeypatch):
    from core.channels import unified
    monkeypatch.setattr(unified, "send_message", Mock(side_effect=AssertionError("Must not send during setup")))
    result = connect_telegram()
    assert result["message_sent"] is False
    session = isolated.list_sessions()[0]
    assert session.metadata["platform"]["name"] == "Telegram" and not session.history
    from core.session_service import SessionService
    reread = SessionService(isolated.storage_dir).get_session(session.session_id)
    assert reread.metadata["channel"] == "telegram_test"


def test_conversation_rename_survives_connection_access(isolated):
    from core.channels.conversations import ensure_conversation
    connect_telegram()
    session = ensure_conversation("telegram_test")
    isolated.update_session(session.session_id, title="Owner title")
    assert ensure_conversation("telegram_test").metadata["title"] == "Owner title"


def test_sync_existing_connection_creates_one_thread_without_refreshing_recency(isolated):
    from core.channels.unified import register_channel
    from core.channels.conversations import sync_conversations
    register_channel("old_telegram", "tgram://123456:fixture_only_no_live_token_123456/12345")
    first = sync_conversations()["session_ids"]
    session = isolated.get_session(first[0]); timestamp = session.updated_at
    assert sync_conversations()["session_ids"] == first
    assert len(isolated.list_sessions()) == 1 and session.updated_at == timestamp


def test_delivery_identity_records_actual_result_and_never_sends_twice(isolated, monkeypatch):
    from core.channels import unified
    from core.channels.conversations import deliver
    connect_telegram()
    sender = Mock(return_value=unified.ChannelResult("telegram_test", True, "sent"))
    monkeypatch.setattr(unified, "send_message", sender)
    first = deliver("telegram_test", "Fixture message", "fixture_request_0001")
    replay = deliver("telegram_test", "Fixture message", "fixture_request_0001")
    assert first["ok"] and replay["ok"] and replay["duplicate"] and sender.call_count == 1
    history = isolated.get_session(first["session_id"]).history
    assert len(history) == 2 and history[1]["metadata"]["delivered"] is True
    with pytest.raises(ValueError, match="conflicts"):
        deliver("telegram_test", "Different body", "fixture_request_0001")


def test_pending_receipt_after_interruption_is_not_resent(isolated, monkeypatch):
    from core.channels import unified
    from core.channels.conversations import ensure_conversation, deliver
    connect_telegram()
    session = ensure_conversation("telegram_test")
    isolated.append_messages(session.session_id, [{"role": "user", "content": "Fixture message", "message_id": "fixture_request_0002"}])
    sender = Mock(side_effect=AssertionError("No automatic resend"))
    monkeypatch.setattr(unified, "send_message", sender)
    assert deliver("telegram_test", "Fixture message", "fixture_request_0002")["uncertain"] is True
    assert not sender.called


def test_failed_delivery_is_durable_and_removed_connection_cannot_send(isolated, monkeypatch):
    from core.channels import unified
    from core.channels.conversations import deliver
    connect_telegram()
    sender = Mock(return_value=unified.ChannelResult("telegram_test", False, "Fixture failure"))
    monkeypatch.setattr(unified, "send_message", sender)
    result = deliver("telegram_test", "Fixture message", "fixture_request_0003")
    assert not result["ok"] and isolated.get_session(result["session_id"]).history[-1]["metadata"]["delivered"] is False
    unified.unregister_channel("telegram_test")
    with pytest.raises(ValueError, match="removed"):
        deliver("telegram_test", "Fixture message", "fixture_request_0004")
    assert isolated.get_session(result["session_id"]) is not None and sender.call_count == 1


def test_messaging_logos_are_local_and_do_not_require_connection_broker(isolated):
    from core.channels.service_catalog import services
    root = Path(__file__).resolve().parents[1]/"ui/wise_web"
    for row in services():
        assert row["icon_url"].startswith("/app/assets/")
        assert (root/row["icon_url"].removeprefix("/app/")).is_file()
