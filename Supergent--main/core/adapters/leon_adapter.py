"""
Leon Assistant Adapter for Supergent (Agent OS).

Provides bi-directional access from Supergent to Leon AI's core capabilities:
- Text-to-Speech (TTS) & audio output
- Environmental Context Manager (active window, battery, network, weather)
- Episodic & Owner Memory (OWNER.md, daily summaries)
- Live UI messaging to Leon's React 19 web-app
"""

from __future__ import annotations

import os
import json
import logging
from typing import Any, Dict, List, Optional

from .base import BaseAdapter, AdapterError, AdapterStatus

LOG = logging.getLogger("agent_os.adapters.leon")


class LeonAdapter(BaseAdapter):
    NAME = "leon"
    ENV_KEYS = ["LEON_HTTP_URL", "LEON_PROFILE_TOKEN"]

    def __init__(self, base_url: Optional[str] = None):
        super().__init__()
        self.base_url = (base_url or os.environ.get("LEON_HTTP_URL", "http://127.0.0.1:5366")).rstrip("/")
        self.audio_host = os.environ.get("LEON_AUDIO_HOST", "127.0.0.1")
        self.audio_port = int(os.environ.get("LEON_AUDIO_PORT", "5367"))

    def status(self) -> AdapterStatus:
        try:
            resp = self._http("GET", f"{self.base_url}/api/v1/info", timeout=3.0)
            return AdapterStatus(
                name=self.NAME,
                ready=True,
                capabilities=["voice_tts", "context_awareness", "owner_memory", "socket_ui"]
            )
        except Exception as e:
            return AdapterStatus(name=self.NAME, ready=False, reason=str(e))

    def speak(self, text: str, voice: str = "default", lang: str = "en") -> Dict[str, Any]:
        """Send utterance to Leon's voice / TTS synthesis pipeline."""
        return self._call("speak", self._do_speak, text, voice, lang)

    def _do_speak(self, text: str, voice: str, lang: str) -> Dict[str, Any]:
        try:
            # Check direct TCP audio daemon connection
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            result = sock.connect_ex((self.audio_host, self.audio_port))
            if result == 0:
                msg = json.dumps({"topic": "tts", "data": {"text": text, "voice": voice, "lang": lang}})
                sock.sendall(msg.encode("utf-8"))
                sock.close()
                return {"spoken": True, "channel": "tcp_audio_daemon", "text": text}
            sock.close()
        except Exception as e:
            LOG.debug("Direct TCP audio daemon write failed: %s, falling back to HTTP", e)

        try:
            resp = self._http(
                "POST",
                f"{self.base_url}/api/v1/command",
                json={"command": f"speak {text}"},
                timeout=5.0
            )
            return {"spoken": True, "text": text, "response": resp}
        except Exception as e:
            return {"spoken": False, "text": text, "error": str(e)}

    def get_context(self, category: str = "all") -> Dict[str, Any]:
        """Fetch Leon's real-time environmental context (System, Window, Browser, Weather)."""
        return self._call("get_context", self._do_get_context, category)

    def _do_get_context(self, category: str) -> Dict[str, Any]:
        try:
            data = self._http("GET", f"{self.base_url}/api/v1/info", timeout=5.0)
            return {
                "category": category,
                "system_info": data,
                "status": "online"
            }
        except Exception as e:
            return {
                "category": category,
                "system_info": {},
                "status": "offline",
                "error": str(e)
            }

    def get_memory(self, query: str, max_results: int = 5) -> Dict[str, Any]:
        """Query Leon's layered QMD memory and OWNER.md profile."""
        return self._call("get_memory", self._do_get_memory, query, max_results)

    def _do_get_memory(self, query: str, max_results: int) -> Dict[str, Any]:
        try:
            resp = self._http(
                "POST",
                f"{self.base_url}/api/v1/inference",
                json={"prompt": f"Search memory for: {query}", "max_results": max_results},
                timeout=10.0
            )
            return {"query": query, "results": resp}
        except Exception as e:
            return {"query": query, "results": [], "error": str(e)}

    def send_ui_message(self, message: str, title: str = "") -> Dict[str, Any]:
        """Send message or notification to Leon's React web-app client."""
        return self._call("send_ui_message", self._do_send_ui_message, message, title)

    def _do_send_ui_message(self, message: str, title: str) -> Dict[str, Any]:
        try:
            resp = self._http(
                "POST",
                f"{self.base_url}/api/v1/command",
                json={"command": message, "title": title},
                timeout=5.0
            )
            return {"delivered": True, "response": resp}
        except Exception as e:
            return {"delivered": False, "error": str(e)}


# Module level singleton
_LEON_ADAPTER: Optional[LeonAdapter] = None


def get_leon_adapter() -> LeonAdapter:
    global _LEON_ADAPTER
    if _LEON_ADAPTER is None:
        _LEON_ADAPTER = LeonAdapter()
    return _LEON_ADAPTER
