"""
WISE Unified Configuration & Secure Secrets Orchestration Layer.

Enforces strict isolation of secrets between Leon AI and Supergent:
- Leon AI secrets stay in Leon's profile directory (~/.leon/profiles/<profile>/.env)
- Supergent secrets stay in Supergent's keystore (CONFIG_DIR/keystore.json)
- WISE acts as the mediation layer: resolving, validating, and managing
  service endpoints without cross-contaminating secret files.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Dict, Any, Optional

# Root directory of the WISE workspace
WISE_ROOT = Path(__file__).resolve().parent.parent
LEON_ROOT = WISE_ROOT / "leon-develop"
SUPERGENT_ROOT = WISE_ROOT / "Supergent--main"

# Network Ports & Endpoints
DEFAULT_LEON_HTTP_HOST = os.environ.get("LEON_HTTP_HOST", "127.0.0.1")
DEFAULT_LEON_HTTP_PORT = int(os.environ.get("LEON_HTTP_PORT", "5366"))

DEFAULT_LEON_AUDIO_HOST = os.environ.get("LEON_AUDIO_HOST", "127.0.0.1")
DEFAULT_LEON_AUDIO_PORT = int(os.environ.get("LEON_AUDIO_PORT", "5367"))

DEFAULT_SUPERGENT_HOST = os.environ.get("SUPERGENT_HOST", "127.0.0.1")
DEFAULT_SUPERGENT_PORT = int(os.environ.get("SUPERGENT_PORT", "8765"))

DEFAULT_WEB_APP_PORT = int(os.environ.get("WEB_APP_PORT", "5173"))


class WiseConfig:
    """Central configuration loader for the unified WISE ecosystem."""

    def __init__(self):
        self.wise_root = WISE_ROOT
        self.leon_root = LEON_ROOT
        self.supergent_root = SUPERGENT_ROOT

        self.leon_url = f"http://{DEFAULT_LEON_HTTP_HOST}:{DEFAULT_LEON_HTTP_PORT}"
        self.supergent_url = f"http://{DEFAULT_SUPERGENT_HOST}:{DEFAULT_SUPERGENT_PORT}"
        self.supergent_ws_url = f"ws://{DEFAULT_SUPERGENT_HOST}:{DEFAULT_SUPERGENT_PORT}/chat/stream"

    @staticmethod
    def get_leon_profile_env_path(profile_name: str = "just-me") -> Path:
        """Returns the isolated path for Leon profile secrets."""
        leon_home = Path(os.environ.get("LEON_HOME", Path.home() / ".leon")).resolve()
        return leon_home / "profiles" / profile_name / ".env"

    @staticmethod
    def get_supergent_keystore_path() -> Path:
        """Returns the isolated path for Supergent keystore secrets."""
        return SUPERGENT_ROOT / "config" / "keystore.json"

    def get_service_status(self) -> Dict[str, Any]:
        return {
            "name": "WISE",
            "version": "1.0.0",
            "leon": {
                "http": self.leon_url,
                "tcp_audio_port": DEFAULT_LEON_AUDIO_PORT,
                "root": str(self.leon_root),
            },
            "supergent": {
                "rest": self.supergent_url,
                "ws": self.supergent_ws_url,
                "root": str(self.supergent_root),
            },
            "secrets_isolated": True,
        }


# Global singleton instance
wise_config = WiseConfig()
