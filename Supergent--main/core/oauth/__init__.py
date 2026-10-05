"""OAuth 2.0 / PKCE flow with loopback redirect.

Public surface:

    from core.oauth import (
        OAUTH_PROVIDERS,
        SCOPE_BUNDLES,
        start_link_flow,
        complete_link_flow,
        get_link_status,
        get_account,
        list_accounts,
        revoke_account,
    )

Tokens are persisted via :class:`core.llm.keystore.KeyStore` so they
share the same XOR-obfuscation + device-bound secret as LLM API keys.
The token bundle (``access_token``, ``refresh_token``, ``expiry``,
``scopes``, ``account_email``) is JSON-encoded into the ``api_key``
slot.

Why the loopback redirect: to keep this working on Linux desktop,
Linux and Windows without forcing the user to host a public domain,
we run a tiny HTTP server on ``127.0.0.1:<random>`` and use that as
the OAuth ``redirect_uri``. Google, GitHub, and Microsoft all accept
loopback redirect URIs (RFC 8252).
"""

from __future__ import annotations

from .providers import OAUTH_PROVIDERS, OAuthProvider
from .scopes import SCOPE_BUNDLES, resolve_scopes
from .flow import (
    LinkFlowState,
    start_link_flow,
    complete_link_flow,
    get_link_status,
)
from .store import (
    OAuthAccount,
    get_account,
    list_accounts,
    revoke_account,
    save_account,
    refresh_token_if_needed,
)

__all__ = [
    "OAUTH_PROVIDERS",
    "OAuthProvider",
    "SCOPE_BUNDLES",
    "resolve_scopes",
    "LinkFlowState",
    "start_link_flow",
    "complete_link_flow",
    "get_link_status",
    "OAuthAccount",
    "get_account",
    "list_accounts",
    "revoke_account",
    "save_account",
    "refresh_token_if_needed",
]
