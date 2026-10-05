"""Emit reviewed public Nango brand SVGs; never fetch renderer-supplied URLs."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
NAMES = ("github", "slack", "google-mail", "google-drive", "google-calendar", "google-docs",
         "google", "microsoft", "microsoft-teams", "microsoft-outlook", "notion", "linear", "discord",
         "telegram", "mattermost", "twilio", "dropbox", "box", "hubspot", "salesforce", "asana", "trello")


def main():
    catalog = json.loads((ROOT / "core/integrations/provider_catalog.json").read_text(encoding="utf-8"))
    revision = catalog["revision"]
    known = {row[0]: row[5] for row in catalog["providers"]}
    files = sorted({known[name] for name in NAMES if name in known and known[name].endswith(".svg")})
    def fetch(name):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+\.svg", name): raise ValueError("Invalid static asset name")
        source = f"https://raw.githubusercontent.com/NangoHQ/nango/{revision}/packages/webapp/public/images/template-logos/{name}"
        with urllib.request.urlopen(source, timeout=25) as response: data = response.read(150_001)
        if len(data) > 150_000: raise ValueError("Logo exceeded the static asset limit")
        text = data.decode("utf-8")
        root = ET.fromstring(text)
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] in {"script", "foreignObject"}: raise ValueError("Active SVG rejected")
            for key, value in node.attrib.items():
                if key.lower().startswith("on") or (key.rsplit("}", 1)[-1] == "href" and not value.startswith("#")):
                    raise ValueError("External or active SVG rejected")
        return name, text
    with ThreadPoolExecutor(max_workers=4) as pool: output = dict(pool.map(fetch, files))
    print(json.dumps({"revision":revision, "assets":output}, ensure_ascii=False))


if __name__ == "__main__": main()
