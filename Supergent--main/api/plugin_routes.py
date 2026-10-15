"""Same-origin human extension lifecycle. No credential values in responses."""
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from core.plugins.manager import PluginError, get_plugin_manager
from core.plugins.store import get_package_store


def same_origin(request: Request):
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
    try: return getattr(get_plugin_manager(), method)(*args, **kwargs)
    except PluginError as error: raise HTTPException(error.status, str(error)) from None
    except ValueError as error: raise HTTPException(422, str(error)[:240]) from None
    except Exception: raise HTTPException(502, "Extension operation failed; check the local runtime and retry") from None


def package_call(method, *args, **kwargs):
    try: return getattr(get_package_store(), method)(*args, **kwargs)
    except PluginError as error: raise HTTPException(error.status, str(error)) from None
    except Exception: raise HTTPException(502, "Plugin package operation failed; check its source, compatibility and network, then retry") from None


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict[str, str | bool | int | float | list[str]] = Field(default_factory=dict, max_length=80)
    trusted_local: bool = False


class AuthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str = Field(default="", max_length=2048, pattern=r"^[^\x00-\x20]*$")
    client_metadata_url: str = Field(default='',max_length=2048,pattern=r'^[^\x00-\x20]*$')


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: str = Field(max_length=100_000)


@router.get("")
def catalog(query: str = Query("", max_length=300), category: str = Query("", max_length=80),
            source: str = Query("", max_length=30), connected: bool = False,
            offset: int = Query(0, ge=0, le=6000), limit: int = Query(48, ge=1, le=48), refresh: bool = False):
    return package_call("catalog", query=query, category=category, source=source, offset=offset, limit=limit, refresh=refresh)


class MarketplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(min_length=3,max_length=300)


class InstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit: str = Field(pattern=r"^[a-fA-F0-9]{40}$")
    trusted_publisher: bool = False
    accept_partial: bool = False


@router.post("/marketplaces")
def add_marketplace(body: MarketplaceRequest): return package_call("add_marketplace",body.repository)


@router.get("/packages/{identifier}")
def package_detail(identifier: str): return package_call("detail",identifier)


@router.get("/packages/{identifier}/overview")
def package_overview(identifier: str): return package_call("overview",identifier)


@router.get("/packages/{identifier}/logo")
def package_logo(identifier: str):
    from core.plugins.logos import load_public_logo
    try:
        entry = get_package_store()._entry(identifier)
        from core.plugins.marketplace import PACKAGE_BRANDS
        brand = PACKAGE_BRANDS.get(entry['name'].casefold().removesuffix('-plugin'))
        if not brand: raise HTTPException(404,'No verified service artwork')
        data,media = load_public_logo(f'https://cdn.simpleicons.org/{brand}')
        return Response(data,media_type=media,headers={'Cache-Control':'public, max-age=86400','X-Content-Type-Options':'nosniff'})
    except PluginError as error: raise HTTPException(error.status,str(error)) from None
    except Exception: raise HTTPException(404,'Service artwork unavailable') from None


@router.post("/packages/{identifier}/install")
def install_package(identifier: str, body: InstallRequest):
    return package_call("install",identifier,body.commit,body.trusted_publisher,body.accept_partial)


@router.post("/packages/{identifier}/disable")
def disable_package(identifier: str): return package_call("disable",identifier)


@router.delete("/packages/{identifier}")
def uninstall_package(identifier: str): return package_call("disable",identifier,True)


@router.post("/packages/{identifier}/connections/{name}/connect")
def connect_package(identifier: str,name: str,body: ConnectRequest):
    return package_call("connect",identifier,name,body.fields,body.trusted_local)


@router.post("/packages/{identifier}/connections/{name}/authorize")
def authorize_package(identifier: str,name: str,body: AuthRequest):
    return package_call("authorize",identifier,name,body.client_id)


@router.delete("/packages/{identifier}/connections/{name}")
def reset_package_connection(identifier: str,name: str):
    return package_call("reset_connection",identifier,name)


@router.get("/pending")
def pending(): return {"attempts":call("pending")}


@router.get('/connections')
def service_connections(query: str = Query('',max_length=300), category: str = Query('',max_length=80),
                        offset: int = Query(0,ge=0,le=8000),limit: int = Query(48,ge=1,le=48)):
    from core.plugins.connectors import catalog
    return catalog(get_plugin_manager(),query=query,category=category,offset=offset,limit=limit)


@router.post('/connections/{identifier}/connect')
def connect_service(identifier: str, body: ConnectRequest):
    return call('connect_service',identifier,body.fields,body.trusted_local)


@router.get('/connections/{identifier}/readiness')
def application_readiness(identifier: str): return call('probe_application',identifier)


@router.post('/bundles')
async def import_bundle(request: Request):
    from core.plugins.bundles import MAX_UPLOAD
    if request.headers.get('content-type','').split(';')[0]!='application/octet-stream':
        raise HTTPException(415,'Upload a .mcpb file as binary data')
    data=bytearray()
    async for chunk in request.stream():
        if len(data)+len(chunk)>MAX_UPLOAD: raise HTTPException(413,'MCPB file exceeds 64 MB')
        data.extend(chunk)
    # Bounded extraction/hash I/O should not block chat's event loop.
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(call,'import_bundle',bytes(data))


@router.post("/import")
def import_config(body: ImportRequest): return call("import_config", body.document)


@router.get("/integrations/{identifier}")
def detail(identifier: str): return call("detail", identifier)


@router.post("/integrations/{identifier}/connect")
def connect(identifier: str, body: ConnectRequest): return call("connect", identifier, body.fields, body.trusted_local)


@router.post("/integrations/{identifier}/authorize")
def authorize(identifier: str, body: AuthRequest): return call("authorize", identifier, body.client_id, body.client_metadata_url)


@router.post("/integrations/{identifier}/disable")
def disable(identifier: str): return call("disable", identifier)


@router.delete("/integrations/{identifier}")
def uninstall(identifier: str): return call("disable", identifier, True)


@router.get("/attempts/{identifier}")
def poll(identifier: str): return call("poll", identifier)


@router.post("/attempts/{identifier}/launch")
def launch(identifier: str): return {"browser_opened":call("launch", identifier)}


@router.post("/attempts/{identifier}/cancel")
def cancel(identifier: str): return call("cancel", identifier)


@router.get("/logos/{filename}")
def logo(filename: str):
    from core.plugins.logos import load_logo
    try: data, media = load_logo(filename)
    except (ValueError, OSError): raise HTTPException(404, "Logo unavailable") from None
    return Response(data, media_type=media, headers={"Cache-Control":"private, max-age=86400",
        "X-Content-Type-Options":"nosniff", "Content-Security-Policy":"default-src 'none'; sandbox"})
