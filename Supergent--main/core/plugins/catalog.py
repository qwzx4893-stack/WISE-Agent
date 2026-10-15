"""Public MCP discovery. Metadata is never proof of a working connection.

Registry entries are untrusted installation recipes, not commands to execute.
Only pinned npm/PyPI stdio recipes and HTTPS Streamable HTTP are interpreted.
Unsupported arguments/registries stay visible with an explicit explanation.
"""
import hashlib
import json
import re
import ipaddress
from urllib.parse import urlsplit

REGISTRY = "https://registry.modelcontextprotocol.io/v0.1/servers"


def identifier(value):
    return "mcp_" + hashlib.sha256(value.encode()).hexdigest()[:24]


def public_https(value):
    # Metadata validation is pure. Resolve/pin DNS only at network use.
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ValueError("Invalid public URL")
    p = urlsplit(value)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment or p.port not in {None, 443}:
        raise ValueError("A public HTTPS URL without credentials is required")
    if p.hostname.lower() in {"localhost", "localhost.localdomain"} or "." not in p.hostname or p.hostname.endswith((".local", ".internal", ".localhost")):
        raise ValueError("Local URLs are not catalog entries")
    try:
        if not ipaddress.ip_address(p.hostname).is_global: raise ValueError("Private URL")
    except ValueError as error:
        if str(error) == "Private URL": raise
    return value


def bounded_json(url, params=None):
    import httpx
    # GET metadata only: one bounded retry through another checked public
    # address of the SAME host. Never applied to tools/call or OAuth writes.
    for attempt in range(2):
        try: return _download_json(url, params, address=attempt)
        except httpx.HTTPError:
            if attempt: raise


def _download_json(url, params=None, *, address=0):
    import httpx
    from core.public_http import PublicTransport
    public_https(url)
    with httpx.Client(transport=PublicTransport(address_offset=address), timeout=20, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", url, params=params) as response:
            response.raise_for_status()
            data = bytearray()
            for part in response.iter_bytes():
                data.extend(part)
                if len(data) > 4_000_000: raise ValueError("Catalog response exceeds budget")
    result = json.loads(data)
    if not isinstance(result, dict): raise ValueError("Invalid catalog response")
    return result


def field(raw, group, index):
    if not isinstance(raw, dict) or not isinstance(raw.get("choices", []), list): raise ValueError("Invalid input definition")
    key = str(raw.get("name") or raw.get("valueHint") or f"{group}_{index}")
    if not re.fullmatch(r"[\w ./-]{1,100}", key): raise ValueError("Unsupported input name")
    return {"id": f"{group}_{index}", "name": key, "label": str(raw.get("description") or key)[:200],
            "required": bool(raw.get("isRequired", False)), "secret": bool(raw.get("isSecret", group in {"env", "header"})),
            "default": str(raw.get("default") or "")[:2048], "choices": [str(v) for v in raw.get("choices", [])[:50]]}


def header_fields(headers):
    fields = []
    for i, raw in enumerate(headers):
        template = raw.get("value", "")
        if not isinstance(template, str) or len(template)>4096 or any(ord(c)<32 for c in template): raise ValueError("Invalid header value")
        variables = raw.get("variables", {})
        if not isinstance(variables, dict): raise ValueError("Invalid header variables")
        names = list(dict.fromkeys(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]{0,79})\}", template)))
        if re.search(r"[{}]", re.sub(r"\{[A-Za-z_][A-Za-z0-9_]{0,79}\}", "", template)) or len(names)>12: raise ValueError("Unsupported header template")
        if names:
            for name in names:
                spec = {"name":name, "isRequired":True, "isSecret":True, **variables.get(name, {})}
                f = field(spec, "header", i)
                f["id"] += "_"+name
                fields.append(f)
        elif not template: fields.append(field(raw, "header", i))
    return fields


def from_registry(wrapper):
    if not isinstance(wrapper, dict): return None
    row = wrapper.get("server", wrapper)
    if not isinstance(row, dict): return None
    name, version = row.get("name"), row.get("version")
    if not isinstance(name, str) or not 1 <= len(name) <= 180 or not isinstance(version, str): return None
    meta = wrapper.get("_meta", {}).get("io.modelcontextprotocol.registry/official", {})
    if meta.get("status", "active") != "active" or meta.get("isLatest", True) is False: return None
    result = {"id": identifier(name), "publisher_id": name, "version": version[:100],
              "name": str(row.get("title") or name.rsplit("/", 1)[-1])[:160], "description": str(row.get("description", ""))[:600],
              "source": "community", "categories": ["MCP"], "logo": "", "fields": [],
              "docs_url": str(row.get("repository", {}).get("url") or row.get("websiteUrl") or ""), "recipe": None,
              "unsupported": "No supported installation recipe in this registry entry"}
    for image in (row.get("icons") or [])[:4]:
        try:
            url = public_https(image.get("src", ""))
            if image.get("mimeType") in {"image/svg+xml", "image/png", "image/webp"}: result["logo"] = url; break
        except (ValueError, TypeError, AttributeError): pass
    for remote in (row.get("remotes") or [])[:8]:
        if not isinstance(remote, dict): continue
        if remote.get("type") != "streamable-http": continue
        try:
            # Template URLs need explicit supported variable definitions.
            if "{" in remote.get("url", ""): continue
            url = public_https(remote["url"])
            headers = remote.get("headers", [])
            if not isinstance(headers, list) or len(headers) > 20: continue
            for h in headers:
                if not isinstance(h, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", h.get("name", "")) or h["name"].lower() in {"host", "cookie", "content-length", "proxy-authorization"}: raise ValueError("Unsafe header")
            result["fields"] = header_fields(headers)
            result["recipe"] = {"kind": "remote", "url": url, "headers": headers}
            result["unsupported"] = ""
            return result
        except (ValueError, KeyError, TypeError): continue
    for package in (row.get("packages") or [])[:8]:
        if not isinstance(package, dict): continue
        kind, pkg, pin = package.get("registryType"), package.get("identifier", ""), package.get("version", "")
        if package.get("transport", {}).get("type") != "stdio" or kind not in {"npm", "pypi"}: continue
        if not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,100}", pin): continue
        if kind == "npm" and not re.fullmatch(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*", pkg): continue
        if kind == "pypi" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", pkg): continue
        if package.get("runtimeArguments"): continue  # Never import flags capable of overriding the runner.
        args, env = package.get("packageArguments", []), package.get("environmentVariables", [])
        if not isinstance(args, list) or not isinstance(env, list) or len(args) > 30 or len(env) > 30: continue
        try:
            fields = [field(a, "arg", i) for i, a in enumerate(args) if not a.get("value")]
            fields += [field(e, "env", i) for i, e in enumerate(env) if not e.get("value")]
            for arg in args:
                if arg.get("isSecret"): raise ValueError("Secret arguments belong in environment variables, not process command lines")
                if arg.get("type") not in {"positional", "named"}: raise ValueError("Unknown argument type")
                if arg.get("variables") or arg.get("isRepeated"): raise ValueError("Unsupported variable/repeated argument")
                if arg.get("type") == "named" and not re.fullmatch(r"--?[\w-]+", arg.get("name", "")): raise ValueError("Invalid argument flag")
            for e in env:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", e.get("name", "")): raise ValueError("Invalid environment key")
            result.update(fields=fields, recipe={"kind":"package", "registry":kind, "package":pkg, "version":pin,
                                               "arguments":args, "environment":env}, unsupported="")
            return result
        except (ValueError, TypeError, AttributeError): continue
    return result


# Reviewed service endpoints. Connecting still requires a real handshake/login,
# and provider policies may disallow new clients. No ready/tool-count claims.
FEATURED = [
    ("notion", "Notion", "https://mcp.notion.com/mcp", "https://developers.notion.com/guides/mcp", "Productivity", "notion"),
    ("github", "GitHub", "https://api.githubcopilot.com/mcp/", "https://github.com/github/github-mcp-server", "Development", "github"),
    ("linear", "Linear", "https://mcp.linear.app/mcp", "https://linear.app/docs/mcp", "Productivity", "linear"),
    ("sentry", "Sentry", "https://mcp.sentry.dev/mcp", "https://docs.sentry.io/product/sentry-mcp/", "Development", ""),
    ("cloudflare-docs", "Cloudflare Docs", "https://docs.mcp.cloudflare.com/mcp", "https://developers.cloudflare.com/agents/model-context-protocol/mcp-servers-for-cloudflare/", "Research", ""),
    ("context7", "Context7", "https://mcp.context7.com/mcp", "https://github.com/upstash/context7", "Research", ""),
    ("figma", "Figma", "https://mcp.figma.com/mcp", "https://developers.figma.com/docs/figma-mcp-server/", "Design", "figma"),
    ("supabase", "Supabase", "https://mcp.supabase.com/mcp", "https://supabase.com/docs/guides/ai-tools/mcp", "Databases", ""),
    ("vercel", "Vercel", "https://mcp.vercel.com", "https://vercel.com/docs/agent-resources/vercel-mcp", "Development", ""),
    ("atlassian", "Atlassian", "https://mcp.atlassian.com/v1/mcp", "https://support.atlassian.com/atlassian-rovo-mcp-server/", "Productivity", ""),
    ("stripe", "Stripe", "https://mcp.stripe.com", "https://docs.stripe.com/mcp", "Business", ""),
    ("huggingface", "Hugging Face", "https://huggingface.co/mcp", "https://huggingface.co/docs/hub/en/hf-mcp-server", "Research", ""),
]


def featured():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "ui/wise_web/assets/integrations"
    icons = {"figma":"figma", "sentry":"sentry", "stripe":"stripe", "supabase":"supabase", "vercel":"vercel",
             "cloudflare-docs":"cloudflare", "huggingface":"huggingface", "atlassian":"atlassian", "context7":"upstash"}
    rows = [{"id":key, "publisher_id":key, "name":name, "description":"", "version":"service", "source":"featured",
             "categories":[category], "logo":f"/app/assets/integrations/{logo}.svg" if logo and (root/(logo+".svg")).is_file() else "https://cdn.simpleicons.org/"+icons[key]+"/ffffff" if key in icons else "", "fields":[],
             "docs_url":docs, "recipe":{"kind":"remote", "url":url, "headers":[]}, "unsupported":""}
            for key,name,url,docs,category,logo in FEATURED]
    for row in rows:
        if row["id"] in {"github", "huggingface", "stripe"}:
            raw = {"name":"Authorization", "description":"Service access token (optional alternative to OAuth)", "isSecret":True, "format":"bearer"}
            if row["id"] == "github": raw["description"] = "GitHub personal access token (optional; only the repository permissions you need)"
            if row["id"] == "stripe": raw["description"] = "Stripe Agent API key (optional alternative to OAuth)"
            row["fields"] = [field(raw,"header",0)]
            row["recipe"]["headers"] = [raw]
    return rows
