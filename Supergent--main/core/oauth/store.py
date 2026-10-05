"""Token persistence — bundles ride on the existing encrypted KeyStore.

Every account is one ``KeyStore`` entry whose ``provider`` is
``oauth.<provider_id>`` and whose ``api_key`` is a JSON-serialised
:class:`OAuthAccount`. This means the same XOR-obfuscation,
device-bound secret, and atomic file write used for LLM keys also
protects refresh tokens.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class OAuthAccount:
    """JSON-safe view of a linked account."""

    provider: str
    account: str
    access_token: str
    refresh_token: str = ""
    expiry: float = 0.0  # Unix timestamp; 0 means "no expiry tracked"
    scopes: List[str] = field(default_factory=list)
    token_type: str = "Bearer"
    obtained_at: float = field(default_factory=lambda: time.time())

    def is_expired(self, *, leeway_s: int = 60) -> bool:
        if self.expiry == 0.0:
            return False
        return time.time() + leeway_s >= self.expiry

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OAuthAccount":
        clean = {k: v for k, v in data.items()
                  if k in cls.__dataclass_fields__}
        return cls(**clean)


# ---------------------------------------------------------------------------
# Naming convention
# ---------------------------------------------------------------------------
def _entry_name(provider: str, account: str) -> str:
    return f"oauth:{provider}:{account}"


def _provider_id(provider: str) -> str:
    return f"oauth.{provider}"


def _is_oauth_entry(entry: Dict[str, Any]) -> bool:
    # KeyStore.add normalises ``provider`` to a known LLM provider id,
    # so we can't rely on the provider field carrying ``oauth.*``. Use
    # the structured name prefix ``oauth:<provider>:<account>`` instead.
    return str(entry.get("name", "")).startswith("oauth:")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def save_account(acct: OAuthAccount) -> None:
    """Persist (or update) an :class:`OAuthAccount`."""
    from core.llm.keystore import KeyStore
    ks = KeyStore()
    ks.add(name=_entry_name(acct.provider, acct.account),
            api_key=json.dumps(acct.to_dict(), ensure_ascii=False),
            provider=_provider_id(acct.provider),
            model="oauth",
            base_url="",
            enabled=True,
            priority=0)


def get_account(provider: str, account: str) -> Optional[OAuthAccount]:
    from core.llm.keystore import KeyStore
    ks = KeyStore()
    entry = ks.get(_entry_name(provider, account), reveal=True)
    if not entry:
        return None
    raw_key = entry.get("api_key") or ""
    try:
        data = json.loads(raw_key)
    except Exception:
        return None
    return OAuthAccount.from_dict(data)


def list_accounts() -> List[Dict[str, Any]]:
    """Public, secret-free view of every linked OAuth account."""
    from core.llm.keystore import KeyStore
    out: List[Dict[str, Any]] = []
    for entry in KeyStore().list(reveal=False):
        if not _is_oauth_entry(entry):
            continue
        # Names look like "oauth:google:user@example.com".
        name = entry.get("name", "")
        parts = name.split(":", 2)
        provider = parts[1] if len(parts) >= 2 else "?"
        account = parts[2] if len(parts) >= 3 else "?"
        out.append({
            "provider": provider,
            "account": account,
            "name": name,
            "enabled": entry.get("enabled", True),
        })
    return out


def revoke_account(provider: str, account: str) -> bool:
    """Remove the stored bundle. (Server-side revocation is in flow.py.)"""
    from core.llm.keystore import KeyStore
    return KeyStore().remove(_entry_name(provider, account))


def refresh_token_if_needed(provider: str, account: str) -> Optional[OAuthAccount]:
    """Refresh the access token via the provider's refresh-token grant.

    Returns the refreshed account on success, ``None`` if no refresh
    token is stored. Raises ``RuntimeError`` on a refresh failure so
    callers can surface a useful error.
    """
    acct = get_account(provider, account)
    if acct is None:
        return None
    if not acct.is_expired():
        return acct
    if not acct.refresh_token:
        raise RuntimeError(
            f"access token for {provider}:{account} expired and no "
            "refresh_token is stored")
    # Lazy import to avoid the dep at import time.
    from .flow import refresh_with_provider
    refreshed = refresh_with_provider(provider, acct)
    save_account(refreshed)
    return refreshed


__all__ = [
    "OAuthAccount",
    "save_account",
    "get_account",
    "list_accounts",
    "revoke_account",
    "refresh_token_if_needed",
]
