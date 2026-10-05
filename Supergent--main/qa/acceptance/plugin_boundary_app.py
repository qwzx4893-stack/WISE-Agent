"""QA-only app: actual WISE backend, simulated external Nango/browser boundary.

Never imported by production. Requires an isolated runtime and explicit QA flag.
"""
from datetime import datetime, timedelta, timezone
import json
import os

import httpx

if os.getenv("WISE_PLUGIN_BOUNDARY_TEST") != "1" or not os.getenv("WISE_RUNTIME_ROOT"):
    raise RuntimeError("This module is an isolated credential-boundary test only")

from api.server import app
import api.plugin_routes as routes
import core.integrations.service as service_module
from core.integrations.provider import NangoProvider

state = {"remote": [], "polls": 0}


def handler(request):
    path = request.url.path
    if path == "/integrations":
        return httpx.Response(200,json={"data":[{"unique_key":"github-qa","display_name":"GitHub","provider":"github"},
            {"unique_key":"slack-qa","display_name":"Slack","provider":"slack"}]})
    if path == "/providers":
        return httpx.Response(200,json={"data":[{"name":"github","display_name":"GitHub","auth_mode":"OAUTH2","categories":["dev-tools"],
            "logo_url":"https://app.nango.dev/images/template-logos/github.svg"},{"name":"slack","auth_mode":"OAUTH2","categories":["communication"]}]})
    if path.startswith("/connect/sessions"):
        body = json.loads(request.content); state["polls"] = 0
        state["remote"] = [{"provider":"github","provider_config_key":"github-qa","connection_id":"fixture-remote-account",
            "tags":body["tags"],"errors":[],"metadata":{"username":"QA boundary account"}}]
        return httpx.Response(201,json={"data":{"token":"external-qa-only","connect_link":"https://connect.nango.dev/?session_token=external-qa-only",
            "expires_at":(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()}})
    if path == "/connections":
        state["polls"] += 1
        return httpx.Response(200,json={"connections":state["remote"] if state["polls"] > 1 else []})
    if request.method == "DELETE":
        state["remote"] = []
        return httpx.Response(200,json={"success":True})
    return httpx.Response(404,json={})


provider = NangoProvider("external-qa-only", transport=httpx.MockTransport(handler))
service = service_module.IntegrationService(provider, opener=lambda url, **kwargs: True)
routes.get_integration_service = lambda: service
service_module.get_integration_service = lambda: service
