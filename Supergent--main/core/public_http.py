"""Public-only HTTP transport with checked DNS pinned to the actual socket."""
from __future__ import annotations
import ipaddress
import socket
import threading
import httpx


class PublicTransport(httpx.BaseTransport):
    def __init__(self):
        self.pools = {}
        self.lock = threading.Lock()

    def handle_request(self, request):
        url = request.url
        if url.scheme not in {"https","http"} or not url.host or url.username or url.password:
            raise ValueError("Only public HTTP(S) resources without credentials are allowed")
        addresses = socket.getaddrinfo(url.host,url.port or (443 if url.scheme == "https" else 80),type=socket.SOCK_STREAM)
        ips = {row[4][0] for row in addresses}
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise ValueError("Private/local resource addresses are blocked")
        ip = sorted(ips,key=lambda value:(":" in value,value))[0]
        # A separate pool per original host preserves certificate checks even
        # when multiple virtual hosts share an IP. Never use proxy env vars.
        key = (url.scheme,url.host,url.port,ip)
        with self.lock:
            if key not in self.pools:
                if len(self.pools) >= 24: raise ValueError("Public host limit exceeded")
                self.pools[key] = httpx.HTTPTransport(trust_env=False)
            transport = self.pools[key]
        headers = httpx.Headers(request.headers)
        headers["Host"] = url.netloc.decode("ascii")
        pinned = httpx.Request(request.method,url.copy_with(host=ip),headers=headers,stream=request.stream,
            extensions={**request.extensions,"sni_hostname":url.host})
        return transport.handle_request(pinned)

    def close(self):
        for transport in self.pools.values(): transport.close()
        self.pools.clear()
