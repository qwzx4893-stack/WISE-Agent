"""Provider boundary based on current Nango HTTP APIs, not renderer SDK tokens."""
from abc import ABC, abstractmethod
import os
from urllib.parse import quote, urlsplit

import httpx


class IntegrationError(RuntimeError):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class IntegrationProvider(ABC):
    @abstractmethod
    def catalog(self): ...
    @abstractmethod
    def connect_session(self, integration, tags, connection=None): ...
    @abstractmethod
    def connections(self, owner, attempt=None, connection=None): ...
    @abstractmethod
    def disconnect(self, integration, connection): ...
    @abstractmethod
    def read(self, integration, connection, path, params): ...


class NangoProvider(IntegrationProvider):
    def __init__(self, secret=None, base=None, transport=None):
        self.secret = os.getenv("NANGO_SECRET_KEY", "") if secret is None else secret
        self.base = (base or os.getenv("NANGO_BASE_URL", "https://api.nango.dev")).rstrip("/")
        parsed = urlsplit(self.base)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname or (parsed.scheme != "https" and not local_http):
            raise IntegrationError("Invalid Nango backend URL", 503)
        self.transport = transport

    @property
    def configured(self): return bool(self.secret)

    def _call(self, method, path, *, params=None, body=None, headers=None):
        if not self.configured: raise IntegrationError("Integrations service is not configured", 503)
        try:
            with httpx.Client(base_url=self.base, transport=self.transport, timeout=15, trust_env=False,
                              follow_redirects=False, headers={"Authorization": "Bearer " + self.secret}) as client:
                with client.stream(method, path, params=params, json=body, headers=headers) as response:
                    if not 200 <= response.status_code < 300:
                        code = response.status_code
                        raise IntegrationError("Nango authorization failed; check the environment key or reconnect" if code in {401,403} else
                                               "Nango request failed; check integration configuration and retry", code if code in {401,403,404,429} else 502)
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 2_000_000: raise IntegrationError("Integration response exceeded its size budget")
                    import json
                    return json.loads(content)
        except IntegrationError: raise
        except (httpx.HTTPError, ValueError):
            # Never expose a connect URL, header, token or upstream response body.
            raise IntegrationError("Integration service could not be reached; retry later") from None

    def catalog(self):
        return self._call("GET", "/integrations")["data"], self._call("GET", "/providers")["data"]

    def connect_session(self, integration, tags, connection=None):
        body = {"tags": tags}
        if connection is None:
            body["allowed_integrations"] = [integration]
            endpoint = "/connect/sessions"
        else:
            body.update(integration_id=integration, connection_id=connection)
            endpoint = "/connect/sessions/reconnect"
        return self._call("POST", endpoint, body=body)["data"]

    def connections(self, owner, attempt=None, connection=None):
        params = {"tags[end_user_id]": owner, "limit": 100, "page": 1}
        if attempt: params["tags[wise_attempt_id]"] = attempt
        if connection: params["connectionId"] = connection
        # WISE owns at most 100 accounts; never request credentials.
        return self._call("GET", "/connections", params=params)["connections"]

    def disconnect(self, integration, connection):
        result = self._call("DELETE", "/connections/" + quote(connection, safe=""), params={"provider_config_key": integration})
        if result.get("success") is not True: raise IntegrationError("The remote connection was not removed")

    def read(self, integration, connection, path, params):
        return self._call("GET", "/proxy/" + path.lstrip("/"), params=params,
                          headers={"Connection-Id": connection, "Provider-Config-Key": integration})
