"""Named scope bundles surfaced to the user during the link flow.

Whenever the user kicks off ``start_link_flow``, they pick one or more
of these bundles. The resulting OAuth scope list is the union of the
selected bundles, deduplicated. The agent later cannot exceed these
scopes — every account-backed tool (e.g. ``google.gmail.send``)
checks the bundle membership before issuing the API call.
"""

from __future__ import annotations

from typing import Dict, Iterable, List


# Scope strings are exactly what each provider's docs publish.
SCOPE_BUNDLES: Dict[str, Dict[str, List[str]]] = {
    "google": {
        "openid": ["openid", "email", "profile"],
        "gmail.send": [
            "https://www.googleapis.com/auth/gmail.send",
        ],
        "gmail.readonly": [
            "https://www.googleapis.com/auth/gmail.readonly",
        ],
        "calendar.readonly": [
            "https://www.googleapis.com/auth/calendar.readonly",
        ],
        "calendar.events": [
            "https://www.googleapis.com/auth/calendar.events",
        ],
        "drive.readonly": [
            "https://www.googleapis.com/auth/drive.readonly",
        ],
        "drive.file": [
            "https://www.googleapis.com/auth/drive.file",
        ],
    },
    "github": {
        "openid": ["read:user", "user:email"],
        "repo.read": ["repo"],
    },
    "microsoft": {
        "openid": ["openid", "profile", "email", "offline_access"],
        "mail.send": ["Mail.Send"],
        "mail.read": ["Mail.Read"],
        "calendar.readwrite": ["Calendars.ReadWrite"],
    },
}


def resolve_scopes(provider: str, bundles: Iterable[str]) -> List[str]:
    """Resolve a provider + a list of bundle names to a deduplicated scope list."""
    table = SCOPE_BUNDLES.get(provider, {})
    out: List[str] = []
    seen = set()
    # Always include the openid bundle so we can fetch userinfo.
    for b in ("openid", *bundles):
        for s in table.get(b, []):
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


__all__ = ["SCOPE_BUNDLES", "resolve_scopes"]
