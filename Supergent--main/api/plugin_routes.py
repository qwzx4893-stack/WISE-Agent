"""Same-origin human lifecycle endpoints; no arbitrary proxy or OAuth tokens."""
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.integrations.provider import IntegrationError
from core.integrations.service import get_integration_service


def same_origin(request: Request):
    # Applied to GET too: a foreign page must not read account metadata.
    if request.headers.get("sec-fetch-site") == "cross-site": raise HTTPException(403, "Cross-site integration request rejected")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if request.client and request.client.host == "testclient": allowed_hosts.add("testserver")
    if request.url.hostname not in allowed_hosts: raise HTTPException(403, "Plugins is a local desktop service; this Host is not allowed")
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if origin == "null" or parsed.scheme != request.url.scheme or parsed.netloc != request.url.netloc:
            raise HTTPException(403, "Cross-origin integration request rejected")
    if request.method not in {"GET", "HEAD"} and request.headers.get("x-wise-action") != "plugins":
        raise HTTPException(403, "A same-origin Plugins action header is required")


router = APIRouter(prefix="/api/v2/plugins", tags=["Plugins"], dependencies=[Depends(same_origin)])


def call(method, *args, **kwargs):
    try: return getattr(get_integration_service(), method)(*args, **kwargs)
    except IntegrationError as error: raise HTTPException(error.status, str(error)) from None
    except Exception: raise HTTPException(502, "Integrations service failed; check configuration and retry") from None


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str | None = Field(default=None, max_length=128)


@router.get("")
def catalog(query: str = Query("", max_length=300), category: str = Query("", max_length=80), connected: bool = False,
            offset: int = Query(0, ge=0, le=2000), limit: int = Query(24, ge=1, le=48), refresh: bool = False):
    return call("catalog", query=query, category=category, connected=connected, offset=offset, limit=limit, refresh=refresh)


@router.get("/accounts")
def accounts(): return {"accounts": call("accounts", refresh=True)}


@router.get("/pending")
def pending(): return {"attempts": call("pending")}


@router.get("/integrations/{identifier}")
def detail(identifier: str): return call("detail", identifier)


@router.post("/integrations/{identifier}/connect")
def connect(identifier: str, body: ConnectRequest): return call("connect", identifier, body.account)


@router.get("/attempts/{identifier}")
def poll(identifier: str): return call("poll", identifier)


@router.post("/attempts/{identifier}/launch")
def launch(identifier: str): return {"browser_opened": call("launch", identifier)}


@router.post("/attempts/{identifier}/cancel")
def cancel(identifier: str): return call("cancel", identifier)


@router.delete("/accounts/{identifier}")
def disconnect(identifier: str): return call("disconnect", identifier)
