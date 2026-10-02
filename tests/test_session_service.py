from __future__ import annotations

import asyncio

from core.session_service import SessionService


def test_messages_are_durable_titled_and_archivable(tmp_path):
    service = SessionService(tmp_path)
    service.get_or_create_session("wise_alpha")

    updated = service.append_messages(
        "wise_alpha",
        [
            {"role": "user", "content": "   Build a durable session timeline   ", "modality": "chat"},
            {"role": "assistant", "content": "I will keep it durable.", "modality": "chat"},
        ],
    )

    assert updated.metadata["title"] == "Build a durable session timeline"
    assert updated.last_query.strip() == "Build a durable session timeline"
    assert updated.last_response == "I will keep it durable."
    assert len(updated.history) == 2
    assert updated.history[0]["modality"] == "chat"

    reopened = SessionService(tmp_path).get_session("wise_alpha")
    assert reopened is not None
    assert reopened.to_dict()["title"] == "Build a durable session timeline"
    assert len(reopened.history) == 2

    service.update_session("wise_alpha", title="Project WISE", archived=True)
    assert service.list_sessions() == []
    archived = service.list_sessions(include_archived=True)
    assert [item.session_id for item in archived] == ["wise_alpha"]
    assert archived[0].to_dict()["title"] == "Project WISE"


def test_invalid_messages_do_not_pollute_the_timeline(tmp_path):
    service = SessionService(tmp_path)
    service.get_or_create_session("wise_clean")

    session = service.append_messages(
        "wise_clean",
        [
            {"role": "unknown", "content": "ignore"},
            {"role": "user", "content": ""},
            {"role": "tool", "content": "valid tool result"},
        ],
    )

    assert session.history == [{"role": "tool", "content": "valid tool result", "timestamp": session.history[0]["timestamp"]}]


def test_v2_session_index_supports_search_archive_and_pagination(tmp_path, monkeypatch):
    import core.session_service as session_module
    from api import server

    service = SessionService(tmp_path)
    service.append_messages("wise_one", [{"role": "user", "content": "First useful session"}])
    service.append_messages("wise_two", [{"role": "user", "content": "Second useful session"}])
    monkeypatch.setattr(session_module, "get_session_service", lambda: service)

    sessions = asyncio.run(server.v2_list_sessions(False, 10, 0, "second"))
    assert [item["session_id"] for item in sessions] == ["wise_two"]

    updated = asyncio.run(
        server.v2_update_session("wise_two", server.V2SessionUpdateRequest(title="Archived WISE", archived=True))
    )
    assert updated["title"] == "Archived WISE"
    index = asyncio.run(server.v2_list_sessions(False, 10, 0, None))
    assert index == [{
        "session_id": "wise_one",
        "id": "wise_one",
        "title": "First useful session",
        "created_at": service.get_session("wise_one").created_at,
        "updated_at": service.get_session("wise_one").updated_at,
        "last_query": "First useful session",
        "last_response": "",
        "message_count": 1,
        "metadata": {"archived": False, "archived_at": None},
    }]
    history = asyncio.run(server.v2_session_history("wise_one", 1, 0))
    assert history == [{"role": "user", "content": "First useful session", "timestamp": history[0]["timestamp"]}]
