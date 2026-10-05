"""Slack adapter — Web API.

Capabilities exposed as tools:

- ``slack_post_message``    post a message to a channel.
- ``slack_list_channels``   public/private channels visible to the bot.
- ``slack_list_users``      workspace users.
- ``slack_upload_file``     upload a snippet/text file to a channel.
- ``slack_status``          identity + scopes.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .base import AdapterError, BaseAdapter

_API = "https://slack.com/api"


class SlackAdapter(BaseAdapter):
    NAME = "slack"
    KEYSTORE_NAMES = ["slack", "SLACK_BOT_TOKEN"]
    ENV_KEYS = ["SLACK_BOT_TOKEN", "SLACK_TOKEN"]
    REQUIRED: List[str] = ["SLACK_BOT_TOKEN"]
    CLI_FALLBACK = False
    RATE_PER_SEC = 2.0
    RATE_BURST = 5

    # ------------------------------------------------------------------
    def _token(self) -> Optional[str]:
        return self.cred(*self.KEYSTORE_NAMES, *self.ENV_KEYS)

    def _headers(self) -> Dict[str, str]:
        token = self._token()
        if not token:
            raise AdapterError("slack: لا يوجد token")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def is_ready(self) -> bool:
        return bool(self._token())

    # ------------------------------------------------------------------
    def _check(self, resp: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(resp, dict):
            raise AdapterError(f"slack: unexpected response: {resp}")
        if not resp.get("ok"):
            raise AdapterError(
                f"slack: {resp.get('error') or 'unknown error'}",
                details=resp,
            )
        return resp

    # ------------------------------------------------------------------
    def post_message(self, channel: str, text: str, *,
                     blocks: Optional[List[Dict[str, Any]]] = None,
                     thread_ts: str = "") -> Dict[str, Any]:
        payload: Dict[str, Any] = {"channel": channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts
        resp = self._call(
            "post_message", self._http,
            "POST", f"{_API}/chat.postMessage",
            headers=self._headers(), json=payload,
        )
        return self._check(resp)

    def list_channels(self, *, types: str = "public_channel,private_channel",
                      limit: int = 100) -> List[Dict[str, Any]]:
        resp = self._call(
            "list_channels", self._http,
            "GET", f"{_API}/conversations.list",
            headers=self._headers(),
            params={"types": types, "limit": min(limit, 1000)},
        )
        self._check(resp)
        return resp.get("channels", [])

    def list_users(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        resp = self._call(
            "list_users", self._http,
            "GET", f"{_API}/users.list",
            headers=self._headers(),
            params={"limit": min(limit, 1000)},
        )
        self._check(resp)
        return resp.get("members", [])

    def upload_file(self, channel: str, *, content: str,
                    filename: str = "snippet.txt",
                    title: str = "",
                    initial_comment: str = "") -> Dict[str, Any]:
        # files.upload accepts form-encoded data, not JSON.
        token = self._token()
        if not token:
            raise AdapterError("slack: لا يوجد token")
        try:
            import httpx  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise AdapterError("slack: httpx required") from exc
        headers = {"Authorization": f"Bearer {token}"}
        data = {
            "channels": channel,
            "content": content,
            "filename": filename,
            "title": title or filename,
            "initial_comment": initial_comment,
        }
        try:
            with httpx.Client(timeout=self.DEFAULT_TIMEOUT) as cli:
                resp = cli.post(f"{_API}/files.upload",
                                headers=headers, data=data)
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"slack files.upload network: {exc}") from exc
        return self._check(payload)

    def status_call(self) -> Dict[str, Any]:
        if not self._token():
            return {"authenticated": False}
        resp = self._call(
            "auth_test", self._http,
            "POST", f"{_API}/auth.test",
            headers=self._headers(), json={},
        )
        self._check(resp)
        return {
            "authenticated": True,
            "team": resp.get("team"),
            "user": resp.get("user"),
            "url": resp.get("url"),
        }

    # ------------------------------------------------------------------
    def tools_for(self) -> Dict[str, Callable[..., Any]]:
        return {
            "slack_post_message": self.post_message,
            "slack_list_channels": self.list_channels,
            "slack_list_users": self.list_users,
            "slack_upload_file": self.upload_file,
            "slack_status": self.status_call,
        }


__all__ = ["SlackAdapter"]
