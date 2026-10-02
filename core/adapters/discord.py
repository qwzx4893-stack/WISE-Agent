"""Discord adapter — webhook + bot REST API.

Capabilities exposed as tools:

- ``discord_send_webhook``   post a message to a webhook URL (no auth).
- ``discord_send_message``   bot post to a channel (Bot token required).
- ``discord_list_messages``  fetch recent messages of a channel.
- ``discord_status``         identity probe.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .base import AdapterError, BaseAdapter

_API = "https://discord.com/api/v10"


class DiscordAdapter(BaseAdapter):
    NAME = "discord"
    KEYSTORE_NAMES = ["discord", "DISCORD_BOT_TOKEN"]
    ENV_KEYS = ["DISCORD_BOT_TOKEN", "DISCORD_WEBHOOK_URL"]
    REQUIRED: List[str] = []
    CLI_FALLBACK = False
    RATE_PER_SEC = 5.0
    RATE_BURST = 5

    # ------------------------------------------------------------------
    def _token(self) -> Optional[str]:
        return self.cred("DISCORD_BOT_TOKEN", "discord")

    def _webhook(self) -> Optional[str]:
        return self.cred("DISCORD_WEBHOOK_URL")

    def _headers(self) -> Dict[str, str]:
        token = self._token()
        if not token:
            raise AdapterError("discord: لا يوجد bot token")
        return {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
        }

    def is_ready(self) -> bool:
        return bool(self._token() or self._webhook())

    # ------------------------------------------------------------------
    def send_webhook(self, content: str = "", *, webhook: str = "",
                     username: str = "") -> Dict[str, Any]:
        url = webhook or self._webhook()
        if not url:
            raise AdapterError("discord: لا يوجد webhook URL")
        payload: Dict[str, Any] = {"content": content}
        if username:
            payload["username"] = username
        self._call(
            "send_webhook", self._http,
            "POST", url, json=payload,
        )
        return {"sent": True}

    def send_message(self, channel_id: str, content: str) -> Dict[str, Any]:
        return self._call(
            "send_message", self._http,
            "POST", f"{_API}/channels/{channel_id}/messages",
            headers=self._headers(), json={"content": content},
        )

    def list_messages(self, channel_id: str,
                      *, limit: int = 20) -> List[Dict[str, Any]]:
        return self._call(
            "list_messages", self._http,
            "GET", f"{_API}/channels/{channel_id}/messages",
            headers=self._headers(),
            params={"limit": min(limit, 100)},
        )

    def status_call(self) -> Dict[str, Any]:
        if self._token():
            try:
                me = self._call(
                    "me", self._http,
                    "GET", f"{_API}/users/@me",
                    headers=self._headers(),
                )
                return {
                    "authenticated": True,
                    "id": me.get("id"),
                    "username": me.get("username"),
                    "transport": "bot",
                }
            except AdapterError as exc:
                return {"authenticated": False, "error": str(exc)}
        if self._webhook():
            return {"authenticated": False, "transport": "webhook_only"}
        return {"authenticated": False}

    # ------------------------------------------------------------------
    def tools_for(self) -> Dict[str, Callable[..., Any]]:
        return {
            "discord_send_webhook": self.send_webhook,
            "discord_send_message": self.send_message,
            "discord_list_messages": self.list_messages,
            "discord_status": self.status_call,
        }


__all__ = ["DiscordAdapter"]
