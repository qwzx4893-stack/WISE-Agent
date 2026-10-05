"""Read official public Nango metadata and emit a minimized offline catalog.

Run manually for reviewed updates; production never downloads/executes this data.
Output contains display metadata only, not OAuth endpoints/configuration/secrets.
The caller saves reviewed output using its normal file-edit mechanism.
"""
import hashlib
import json
import re
import urllib.request
from datetime import datetime, timezone

import yaml


def fetch(url, limit=10_000_000):
    request = urllib.request.Request(url, headers={"User-Agent": "WISE-catalog-maintenance", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Public metadata exceeded the update size limit")
    return data


def main():
    revision = json.loads(fetch("https://api.github.com/repos/NangoHQ/nango/commits/master"))["sha"]
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Invalid public repository revision")
    source = f"https://raw.githubusercontent.com/NangoHQ/nango/{revision}/packages/providers/providers.yaml"
    raw = fetch(source, 2_000_000)
    providers = yaml.safe_load(raw)
    tree = json.loads(fetch(f"https://api.github.com/repos/NangoHQ/nango/git/trees/{revision}?recursive=1"))
    if tree.get("truncated"):
        raise ValueError("Cannot verify logo paths in a truncated source tree")
    prefix = "packages/webapp/public/images/template-logos/"
    logos = {row["path"][len(prefix):] for row in tree["tree"] if row["path"].startswith(prefix)}
    rows = []
    for key, entry in providers.items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,110}", str(key)) or not isinstance(entry, dict) or not entry.get("display_name"):
            continue
        alias = entry.get("alias")
        base = providers.get(alias, {}) if isinstance(alias, str) else {}
        logo = next((candidate + ext for candidate in (key, alias) if isinstance(candidate, str) for ext in (".svg", ".png", ".webp") if candidate + ext in logos), "")
        rows.append([key, str(entry["display_name"])[:160], list(entry.get("categories") or base.get("categories") or ["other"]),
                     str(entry.get("auth_mode") or base.get("auth_mode") or ""), str(entry.get("docs") or ""), logo])
    output = {"source": source, "revision": revision, "source_sha256": hashlib.sha256(raw).hexdigest(),
              "generated_at": datetime.now(timezone.utc).isoformat(),
              "fields": ["provider", "name", "categories", "auth_mode", "docs_url", "logo_file"],
              "providers": sorted(rows, key=lambda row: row[1].casefold())}
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
