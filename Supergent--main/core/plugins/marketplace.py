"""Package marketplace catalogs, not a list of WISE's internal capabilities.

Interprets the published Claude/Codex marketplace source formats. GitHub
snapshots are pinned to commits; browsing never installs or executes a plugin.
"""
import hashlib
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from .catalog import bounded_json

DEFAULT_MARKETPLACES = [
    {"repo":"anthropics/claude-plugins-official", "label":"Anthropic directory", "branch":"main"},
    {"repo":"wshobson/agents", "label":"Community workflows", "branch":"main"},
    {"repo":"anthropics/knowledge-work-plugins", "label":"Knowledge work", "branch":"main"},
    {"repo":"anthropics/life-sciences", "label":"Life sciences", "branch":"main"},
    {"repo":"trailofbits/skills", "label":"Trail of Bits", "branch":"main"},
    {"repo":"hashicorp/agent-skills", "label":"HashiCorp", "branch":"main"},
    {"repo":"microsoft/skills", "label":"Microsoft", "branch":"main"},
]
MARKETPLACE_PATHS = (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json")
PACKAGE_BRANDS = {'figma':'figma','stripe':'stripe','sentry':'sentry','vercel':'vercel',
    'supabase':'supabase','cloudflare':'cloudflare','atlassian':'atlassian',
    'context7':'upstash','terraform':'terraform','gitlab':'gitlab','datadog':'datadog'}


def repo_name(value):
    if not isinstance(value, str): raise ValueError("Enter a public GitHub repository")
    value = value.strip()
    if value.startswith("https://github.com/"):
        parsed = urlsplit(value)
        if parsed.query or parsed.fragment: raise ValueError("Use a repository URL without a query")
        value = parsed.path.strip("/").removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", value):
        raise ValueError("Use owner/repository or its public GitHub URL")
    if any(p in {".", ".."} for p in value.split("/")): raise ValueError("Invalid repository")
    return value


def package_path(value):
    if not isinstance(value, str) or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("Invalid package-relative path")
    value = value.removeprefix("./").rstrip("/")
    if value in {"", "."}: return ""
    if value.startswith("/") or any(p in {"", ".", ".."} for p in value.split("/")):
        raise ValueError("Package paths must stay inside their repository")
    # Avoid Win32 path aliases, alternate streams and device files.
    for p in PurePosixPath(value).parts:
        if p.endswith((".", " ")) or re.match(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)",p,re.I):
            raise ValueError("Unsupported Windows package path")
        if any(ord(c)<32 or c in '<>"|?*' for c in p): raise ValueError("Invalid package path")
    return value


def package_id(repo, path, name):
    return "pkg_" + hashlib.sha256(f"{repo.lower()}:{path}:{name}".encode()).hexdigest()[:24]


def source_for(raw, marketplace, sha):
    value = raw.get("source")
    if isinstance(value, str):
        if not value.startswith("./"): raise ValueError("Unsupported package source")
        return {"repo":marketplace["repo"], "path":package_path(value), "sha":sha, "ref":sha}
    if not isinstance(value, dict): raise ValueError("Missing plugin package source")
    kind = value.get("source")
    if kind == "local":
        return {"repo":marketplace["repo"], "path":package_path(value["path"]), "sha":sha, "ref":sha}
    if kind == "github": repo = repo_name(value["repo"])
    elif kind in {"url", "git-subdir"}: repo = repo_name(value["url"])
    else: raise ValueError("Only public GitHub plugin packages are supported")
    pinned = value.get("sha", "")
    if pinned and not re.fullmatch(r"[a-fA-F0-9]{40}", pinned): raise ValueError("Invalid package commit")
    ref = pinned or value.get("ref", "HEAD")
    if not re.fullmatch(r"[A-Za-z0-9_.\-/]{1,120}",ref) or ".." in ref: raise ValueError("Invalid package ref")
    return {"repo":repo, "path":package_path(value.get("path", "")), "sha":pinned, "ref":ref}


def parse_marketplace(data, source, sha):
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list): raise ValueError("No plugin marketplace found")
    if len(data["plugins"])>2000: raise ValueError("Marketplace exceeds package budget")
    items, skipped = [], 0
    for raw in data["plugins"]:
        try:
            name = raw["name"]
            if not isinstance(name,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}",name): raise ValueError()
            origin = source_for(raw,source,sha)
            # A catalog maintainer is not necessarily the plugin's publisher.
            author = raw.get("author") or {}
            author = author.get("name","") if isinstance(author,dict) else str(author)
            items.append({"id":package_id(origin["repo"],origin["path"],name), "name":name,
                "display_name":str(raw.get("displayName") or name)[:150],
                "description":str(raw.get("description", ""))[:2500], "publisher":author[:150],
                "category":str(raw.get("category") or "Other")[:80], "version":str(raw.get("version", ""))[:80],
                "marketplace":str(data.get("name") or source["repo"])[:100], "marketplace_repo":source["repo"],
                "curated":source["repo"]=="anthropics/claude-plugins-official", "origin":origin,
                # Component overrides are part of real marketplace entries, not
                # inventions based on service names. Interpreted during preview.
                "overrides":{k:raw[k] for k in ("skills","commands","agents","hooks","mcpServers","lspServers","dependencies","userConfig") if k in raw},
                "strict":raw.get("strict",True), "kind":"plugin-package"})
        except (ValueError,KeyError,TypeError): skipped += 1
    return items, skipped


def read_marketplace(source, fetch=bounded_json):
    repo = repo_name(source["repo"])
    ref = source.get("branch", "HEAD")
    if not re.fullmatch(r"[A-Za-z0-9_.\-/]{1,120}",ref): raise ValueError("Invalid marketplace branch")
    sha = fetch(f"https://api.github.com/repos/{repo}/commits/{ref}").get("sha", "")
    if not re.fullmatch(r"[a-fA-F0-9]{40}",sha): raise ValueError("Cannot pin marketplace snapshot")
    for path in MARKETPLACE_PATHS:
        try: data = fetch(f"https://raw.githubusercontent.com/{repo}/{sha}/{path}")
        except Exception: continue
        items, skipped = parse_marketplace(data,source,sha)
        return {"repo":repo, "sha":sha, "name":data.get("name",repo), "items":items, "skipped":skipped}
    raise ValueError("No supported plugin marketplace manifest at this repository")
