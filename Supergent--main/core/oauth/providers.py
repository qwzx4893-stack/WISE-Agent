"""Provider profiles for the OAuth flow.

Each provider exposes the four URIs we need (``auth``, ``token``,
``revoke``, ``userinfo``) plus the client-id / client-secret keys we
look up in the encrypted KeyStore at runtime.

We do not ship real client IDs in this repo. The user is expected to
register their own application with each provider and store the
``client_id`` / ``client_secret`` via the admin API. See the
README's "OAuth setup" section for the URLs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class OAuthProvider:
    id: str
    display_name: str
    auth_endpoint: str
    token_endpoint: str
    revoke_endpoint: str
    userinfo_endpoint: str
    # Where in the KeyStore we look up the application credentials.
    client_id_key: str
    client_secret_key: str
    # PKCE is supported by all three but Google/Microsoft require it
    # for "Desktop" / "Native" application types.
    supports_pkce: bool = True
    # ``true`` if the provider also accepts a basic-auth client_secret
    # for token exchange. Google requires it, GitHub doesn't.
    sends_secret_in_body: bool = True


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------
GOOGLE = OAuthProvider(
    id="google",
    display_name="Google",
    auth_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
    token_endpoint="https://oauth2.googleapis.com/token",
    revoke_endpoint="https://oauth2.googleapis.com/revoke",
    userinfo_endpoint="https://openidconnect.googleapis.com/v1/userinfo",
    client_id_key="oauth.google.client_id",
    client_secret_key="oauth.google.client_secret",
)

# ---------------------------------------------------------------------------
# GitHub (scaffolded; tools deferred)
# ---------------------------------------------------------------------------
GITHUB = OAuthProvider(
    id="github",
    display_name="GitHub",
    auth_endpoint="https://github.com/login/oauth/authorize",
    token_endpoint="https://github.com/login/oauth/access_token",
    revoke_endpoint="https://api.github.com/applications/{client_id}/grant",
    userinfo_endpoint="https://api.github.com/user",
    client_id_key="oauth.github.client_id",
    client_secret_key="oauth.github.client_secret",
    supports_pkce=False,  # GitHub Apps support PKCE, classic OAuth does not.
)

# ---------------------------------------------------------------------------
# Microsoft (scaffolded; tools deferred)
# ---------------------------------------------------------------------------
MICROSOFT = OAuthProvider(
    id="microsoft",
    display_name="Microsoft",
    auth_endpoint=("https://login.microsoftonline.com/common/"
                    "oauth2/v2.0/authorize"),
    token_endpoint=("https://login.microsoftonline.com/common/"
                     "oauth2/v2.0/token"),
    revoke_endpoint=("https://login.microsoftonline.com/common/"
                      "oauth2/v2.0/logout"),
    userinfo_endpoint="https://graph.microsoft.com/v1.0/me",
    client_id_key="oauth.microsoft.client_id",
    client_secret_key="oauth.microsoft.client_secret",
)


OAUTH_PROVIDERS: Dict[str, OAuthProvider] = {
    "google": GOOGLE,
    "github": GITHUB,
    "microsoft": MICROSOFT,
}


def get_provider(provider_id: str) -> Optional[OAuthProvider]:
    return OAUTH_PROVIDERS.get((provider_id or "").lower())


__all__ = [
    "OAuthProvider",
    "OAUTH_PROVIDERS",
    "GOOGLE",
    "GITHUB",
    "MICROSOFT",
    "get_provider",
]
