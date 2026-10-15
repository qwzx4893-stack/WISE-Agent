"""Durable outbound channel threads. No invented inbound messages or AI replies."""
import hashlib
import re
import threading

_lock = threading.RLock()


def ensure_conversation(name):
    from .unified import get_registry, _scheme_of
    from .service_catalog import services
    from core.session_service import get_session_service
    normalized = str(name).strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", normalized):
        raise ValueError("Invalid messaging connection")
    url = get_registry().get_url(normalized)
    if not url: raise ValueError("Messaging connection was removed")
    scheme = _scheme_of(url)
    app = next((item for item in services() if scheme in item["protocols"]), None)
    if not app: raise ValueError("This connector is not a messaging app")
    sid = "channel_" + hashlib.sha256(normalized.encode()).hexdigest()[:24]
    metadata = {"origin": "messaging", "title": normalized, "channel": normalized,
        "platform": {"id": app["id"], "name": app["name"], "logo": app["icon_url"], "supports_incoming": False}}
    ss = get_session_service()
    existing = ss.get_session(sid)
    if existing is not None:
        metadata.pop("title")  # Preserve the owner's conversation rename.
        if all(existing.metadata.get(key) == value for key, value in metadata.items()):
            return existing
    return ss.get_or_create_session(sid, metadata=metadata)


def sync_conversations():
    from .unified import list_channels
    from .service_catalog import services
    schemes = {scheme for app in services() for scheme in app["protocols"]}
    sessions = [ensure_conversation(row["name"]).session_id for row in list_channels()["configured"] if row["scheme"] in schemes]
    return {"session_ids": sessions}


def deliver(name, text, request_id):
    from core.session_service import get_session_service
    from .unified import send_message
    if not isinstance(text, str) or not text.strip() or len(text) > 16_000:
        raise ValueError("Enter a message of at most 16000 characters")
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,128}", request_id):
        raise ValueError("A message request identity is required")
    with _lock:
        session = ensure_conversation(name)
        ss = get_session_service()
        previous = next((entry for entry in session.history if entry.get("message_id") == request_id), None)
        receipt = next((entry for entry in session.history if entry.get("message_id") == request_id + "_receipt"), None)
        if previous:
            if previous.get("content") != text.strip():
                raise ValueError("Message request identity conflicts with another message")
            return {"session_id": session.session_id, "ok": bool(receipt and receipt.get("metadata", {}).get("delivered")),
                "duplicate": True, "uncertain": receipt is None}
        # A durable pending record precedes the external effect. If interrupted,
        # the same identity never sends again blindly.
        ss.append_messages(session.session_id, [{"role": "user", "content": text.strip(), "message_id": request_id,
            "modality": "messaging", "metadata": {"delivery": "pending", "channel": name}}])
        result = send_message(name, text.strip())
        ss.append_messages(session.session_id, [{"role": "tool", "content": "Message sent" if result.ok else result.detail,
            "message_id": request_id + "_receipt", "model_name": session.metadata["platform"]["name"],
            "modality": "messaging", "metadata": {"delivered": result.ok, "channel": name}}])
        return {"session_id": session.session_id, "ok": result.ok, "detail": result.detail, "duplicate": False}
