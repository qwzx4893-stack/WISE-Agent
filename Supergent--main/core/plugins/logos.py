"""Bounded public brand artwork fetches, addressed by catalog ID, not URL."""
from pathlib import Path
import re
import threading
import time
from urllib.parse import urljoin
import httpx
import hashlib
import html
from core.public_http import PublicTransport

_cache = {}
_lock = threading.Lock()


def load_logo(identifier):
    from .manager import get_plugin_manager
    from .catalog import public_https
    row = get_plugin_manager()._entry(identifier)
    sources=[row.get('logo','')]
    if row.get('website_url'):
        try:
            site=public_https(row['website_url']);parsed=__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(site)
            sources.append(f'https://{parsed.netloc}/favicon.ico')
        except ValueError: pass
    for source in dict.fromkeys(sources):
        try: return load_public_logo(public_https(source))
        except (ValueError,OSError): pass
    # Neutral monogram, not a fabricated publisher/brand logo. Always render
    # something useful even when the publisher provides no artwork or is offline.
    initials=''.join(word[0] for word in row['name'].split()[:2]).upper()[:2] or 'M'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#303034"/><text x="32" y="40" text-anchor="middle" font-family="sans-serif" font-size="24" fill="#ececee">{html.escape(initials)}</text></svg>').encode(),'image/svg+xml'


def artwork(content):
    if content.startswith(b"\x89PNG\r\n\x1a\n"): return content, "image/png"
    if content.startswith(b'\xff\xd8\xff'): return content,'image/jpeg'
    if content.startswith(b"RIFF") and content[8:12]==b"WEBP": return content,'image/webp'
    if content.startswith(b'\x00\x00\x01\x00') and len(content)>22: return content,'image/x-icon'
    if b"<svg" in content.lower() and not re.search(rb"<!doctype|<!entity|<script|<foreignobject|\bon\w+\s*=|(?:href|src)\s*=\s*['\"](?:https?:|data:|//)",content,re.I): return content,'image/svg+xml'
    raise ValueError('Unsupported artwork')


def disk_path(url):
    from core.paths import MEMORY_DIR
    root=Path(MEMORY_DIR)/'mcp-artwork'
    if root.is_symlink() or not root.resolve().is_relative_to(Path(MEMORY_DIR).resolve()): raise ValueError('Unsafe artwork cache')
    root.mkdir(parents=True,exist_ok=True)
    return root/(hashlib.sha256(url.encode()).hexdigest()+'.image')


def load_public_logo(url):
    from .catalog import public_https
    url = public_https(url)
    with _lock:
        found = _cache.get(url)
        if found and found[0]>time.monotonic(): return found[1]
    path=disk_path(url);stale=None
    if path.is_file() and not path.is_symlink() and path.stat().st_size<=256000:
        try:
            stale=artwork(path.read_bytes())
            if time.time()-path.stat().st_mtime<86400: return stale
        except (ValueError,OSError): pass
    try:
        with httpx.Client(transport=PublicTransport(), trust_env=False, follow_redirects=False, timeout=10) as client:
            current = url
            for attempt in range(4):
                with client.stream("GET", current) as response:
                    if response.status_code in {301,302,303,307,308}:
                        if attempt==3: raise ValueError('Too many artwork redirects')
                        # Every hop is independently validated and DNS-pinned;
                        # no credentials, proxy settings or cookies are sent.
                        current = public_https(urljoin(current,response.headers.get('location','')))
                        continue
                    response.raise_for_status()
                    data = bytearray()
                    for part in response.iter_bytes():
                        data.extend(part)
                        if len(data)>256000: raise ValueError("Oversized artwork")
                    break
    except (httpx.HTTPError,ValueError,OSError):
        if stale: return stale
        raise OSError("Public artwork unavailable") from None
    content = bytes(data)
    result=artwork(content)
    with _lock:
        # Best-effort bounded owned cache; no user files or other caches touched.
        files=sorted(path.parent.glob('*.image'),key=lambda p:p.stat().st_mtime)
        total=sum(p.stat().st_size for p in files)
        while files and (len(files)>=4096 or total+len(content)>64_000_000):
            oldest=files.pop(0);total-=oldest.stat().st_size;oldest.unlink()
        import os
        import tempfile
        with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as out:
            staging=Path(out.name);out.write(content)
        try: os.replace(staging,path)
        finally: staging.unlink(missing_ok=True)
        while len(_cache)>=64 or sum(len(v[1][0]) for v in _cache.values())+len(content)>4_000_000:
            _cache.pop(next(iter(_cache)))
        _cache[url] = (time.monotonic()+86400,result)
    return result
