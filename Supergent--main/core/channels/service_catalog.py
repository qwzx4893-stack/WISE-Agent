"""Schema-driven connection forms from Apprise, not invented app login flows."""
from __future__ import annotations
import re
import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlsplit
from .unified import _apprise_module, register_channel

LOCAL_BRAND_ICONS = {"tgram":"telegram", "whatsapp":"whatsapp", "discord":"discord",
    "signal":"signal", "signals":"signal", "matrix":"matrix", "matrixs":"matrix", "zulip":"zulip"}


@lru_cache(maxsize=1)
def _catalog_brand_icons():
    """Use verified brand paths, not guessed app-site favicon locations."""
    root = Path(__file__).resolve().parents[2]
    catalog = json.loads((root / "core/integrations/provider_catalog.json").read_text(encoding="utf-8"))
    revision = catalog["revision"]
    if not re.fullmatch(r"[a-f0-9]{40}", revision): return {}
    icons = {}
    for provider, name, _categories, _mode, _docs, filename in catalog["providers"]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+\.(svg|png|webp)", filename): continue
        local = root / "ui/wise_web/assets/integrations" / filename
        url = "/app/assets/integrations/" + filename if local.is_file() else f"https://raw.githubusercontent.com/NangoHQ/nango/{revision}/packages/webapp/public/images/template-logos/{filename}"
        for key in (provider, name):
            icons.setdefault(re.sub(r"[^a-z0-9]", "", key.casefold()), url)
    return icons


def services():
    mod = _apprise_module()
    if mod is None:
        return []
    result = []
    for entry in mod.Apprise().details().get("schemas", []):
        details = entry.get("details", {})
        templates = details.get("templates", [])
        if not templates:
            continue
        tokens = details.get("tokens", {})
        protocols = list(entry.get("secure_protocols") or entry.get("protocols") or [])
        if not protocols:
            continue
        template = str(templates[0])
        # Sending Telegram without an explicit recipient triggers owner
        # discovery. A desktop configuration should specify the destination.
        if protocols[0] == "tgram":
            template = next(str(value) for value in templates if "{targets}" in str(value))
        fields = []
        for key in dict.fromkeys(re.findall(r"\{(\w+)\}", template)):
            if key == "schema":
                continue
            info = tokens.get(key, {})
            default = info.get("default")
            fields.append({"id":key, "label":str(info.get("name", key)), "required":bool(info.get("required")) or (protocols[0] == "tgram" and key == "targets"),
                "secret":bool(info.get("private")), "type":str(info.get("type", "string")),
                "choices":[str(value) for value in (info.get("values") or [])],
                "default":None if default is None else str(default),
                "delimiter":str(next(iter(info.get("delim") or ["/"])))})
        website = entry.get("service_url", "")
        parsed = urlsplit(website)
        icon_url = f"https://{parsed.hostname}/favicon.ico" if parsed.hostname else ""
        if protocols[0] in LOCAL_BRAND_ICONS:
            icon_url = "/app/assets/messaging/" + LOCAL_BRAND_ICONS[protocols[0]] + ".svg"
        else:
            aliases = {"azure":"microsoft", "workflow":"microsoft-teams", "gchat":"google", "mmosts":"mattermost",
                       "x":"twitter", "ringc":"ringcentral", "wxteams":"webex"}
            label = aliases.get(protocols[0], str(entry.get("service_name", protocols[0])))
            verified = _catalog_brand_icons().get(re.sub(r"[^a-z0-9]", "", label.casefold()))
            if verified: icon_url = verified
        result.append({"id":protocols[0], "name":str(entry.get("service_name", protocols[0])),
            "protocols":protocols, "template":template, "fields":fields,
            "setup_url":entry.get("setup_url", "https://appriseit.com/services/"),
            "icon_url":icon_url, "supports_incoming":False,
            "authentication":"provider credentials or webhook; consult official setup", "attachment_support":bool(entry.get("attachment_support"))})
    return sorted(result, key=lambda row: row["name"].casefold())


def configure(service_id, name, fields):
    if not isinstance(fields, dict):
        raise ValueError("Connection fields must be an object")
    service = next((row for row in services() if row["id"] == service_id), None)
    if service is None:
        raise ValueError("Service is not supported by the installed Apprise version")
    known = {field["id"] for field in service["fields"]}
    if set(fields) - known:
        raise ValueError("Unknown connection fields")
    values = {"schema":service_id}
    for field in service["fields"]:
        value = str(fields.get(field["id"], field["default"] or "")).strip()
        if field["required"] and not value:
            raise ValueError("Missing required field: " + field["label"])
        if len(value) > 4096 or any(ord(char) < 32 for char in value):
            raise ValueError("Invalid connection field: " + field["label"])
        if field["choices"] and value not in map(str, field["choices"]):
            raise ValueError("Invalid choice: " + field["label"])
        if "list:" in field["type"]:
            values[field["id"]] = field["delimiter"].join(quote(item.strip(), safe="") for item in value.split(",") if item.strip())
        elif field["id"] in ("host", "hostname", "port"):
            if value and not re.fullmatch(r"[A-Za-z0-9.:-]+", value):
                raise ValueError("Invalid host or port")
            values[field["id"]] = value
        else:
            values[field["id"]] = quote(value, safe="")
    url = re.sub(r"\{(\w+)\}", lambda match: values[match[1]], service["template"])
    register_channel(name, url)  # Apprise validates its own URL; no message sent.
    return {"ok":True, "name":name, "service":service_id, "authentication_status":"configured_unverified", "message_sent":False}
