"""Install the pinned official FlaUI net48 assemblies (no .NET SDK needed)."""
from __future__ import annotations
import hashlib
import io
import json
import zipfile
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / ".tooling" / "flaui"
PACKAGES = {"flaui.core": "5.0.0", "flaui.uia3": "5.0.0", "interop.uiautomationclient": "10.19041.0"}
HASHES = {"flaui.core":"191cc65ea82036b77f1872e6d4ebf743d3d120895bba2fa5248d126cd6f568a7",
          "flaui.uia3":"d5d2e083539a04bf6c9053781dfd47332854a76faeb8e9c3c68cc82109f711f2",
          "interop.uiautomationclient":"0d2ed17db2cb13a262f0580ab992db87577651c25a9899053df4114b4d9ff3b1"}


def install():
    DEST.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, version in PACKAGES.items():
        url = f"https://api.nuget.org/v3-flatcontainer/{name}/{version}/{name}.{version}.nupkg"
        response = httpx.get(url, timeout=60, follow_redirects=False)
        response.raise_for_status()
        data = response.content
        if hashlib.sha256(data).hexdigest() != HASHES[name]:
            raise RuntimeError(f"Package hash mismatch: {name}")
        archive = zipfile.ZipFile(io.BytesIO(data))
        files = [item for item in archive.namelist() if item.lower().endswith(".dll") and "/net48/" in item]
        if not files and name.startswith("interop"):
            files = [item for item in archive.namelist() if item.lower().endswith(".dll") and "/net40/" in item]
            if not files:
                files = [item for item in archive.namelist() if item.lower().endswith(".dll")][:1]
        if not files:
            raise RuntimeError(f"No compatible assemblies in {name}: {archive.namelist()}")
        for item in files:
            target = DEST / Path(item).name
            target.write_bytes(archive.read(item))
        manifest.append({"name": name, "version": version, "source": url,
                         "sha256": hashlib.sha256(data).hexdigest(), "assemblies": files})
    (DEST / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    install()
