"""Publish the sealed pre-Plugins snapshot. Token is stdin-only, never persisted.

Separate owned Git history; never stages the user's original working tree.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / ".tooling/publication-wise-agent-pre-plugins"
ACCOUNT = "qwzx4893-stack"
NAME = "WISE-Agent"


def main():
    token = sys.stdin.readline().strip()
    if not token: raise ValueError("A temporary token on stdin is required")
    manifest = json.loads((SNAPSHOT / "PUBLICATION_MANIFEST.json").read_text(encoding="utf-8"))
    if manifest["schema"] != "wise.pre-plugins-snapshot.v1": raise ValueError("Wrong snapshot")
    for row in manifest["files"]:
        path = (SNAPSHOT / row["path"]).resolve()
        if SNAPSHOT.resolve() not in path.parents: raise ValueError("Unsafe manifest path")
        content = path.read_bytes()
        if token.encode() in content: raise ValueError("Credential found in publication")
        if hashlib.sha256(content).hexdigest() != row["sha256"]: raise ValueError("Snapshot changed after sealing")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    with httpx.Client(base_url="https://api.github.com", headers=headers, timeout=30, trust_env=False) as client:
        user = client.get("/user")
        if user.status_code != 200: raise RuntimeError("GitHub authentication failed; token was not stored")
        profile = user.json()
        if profile["login"] != ACCOUNT: raise ValueError("Authenticated account does not match the requested owner")
        existing = client.get(f"/repos/{ACCOUNT}/{NAME}")
        if existing.status_code != 404: raise RuntimeError("Repository already exists or could not be checked; no overwrite attempted")
        # Initialize the private staging checkout BEFORE creating the public repo.
        subprocess.run(["git", "init", "--initial-branch=main", str(SNAPSHOT)], check=True, capture_output=True)
        env = os.environ.copy()
        env.update(GIT_AUTHOR_NAME=ACCOUNT, GIT_COMMITTER_NAME=ACCOUNT,
            GIT_AUTHOR_EMAIL=f'{profile["id"]}+{ACCOUNT}@users.noreply.github.com',
            GIT_COMMITTER_EMAIL=f'{profile["id"]}+{ACCOUNT}@users.noreply.github.com')
        def git(*args):
            result = subprocess.run(["git", "-C", str(SNAPSHOT), *args], env=env, capture_output=True, timeout=300)
            if result.returncode: raise RuntimeError("Git publication step failed; no credentials logged")
            return result.stdout.decode("utf-8", errors="replace").strip()
        git("add", ".")
        git("commit", "-m", "Publish WISE Agent source snapshot before Plugins")
        created = client.post("/user/repos", json={"name": NAME, "private": False,
            "description": "Local-first Windows AI agent workspace with tool routing, on-demand security tooling and configurable model providers. Active development.",
            "has_wiki": False, "has_projects": False})
        if created.status_code != 201: raise RuntimeError("GitHub repository creation failed; no source was pushed")
        url = f"https://github.com/{ACCOUNT}/{NAME}"
        git("remote", "add", "origin", url + ".git")
        auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
        # HTTP credential only in child environment, never remote URL/config/disk.
        env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="2",
            GIT_CONFIG_KEY_0="http.https://github.com/.extraheader", GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + auth,
            GIT_CONFIG_KEY_1="credential.helper", GIT_CONFIG_VALUE_1="")
        git("push", "-u", "origin", "main")
        commit = git("rev-parse", "HEAD")
        topics = client.put(f"/repos/{ACCOUNT}/{NAME}/topics", json={"names": ["ai-agent", "windows", "fastapi", "mcp", "automation", "research"]})
        remote = client.get(f"/repos/{ACCOUNT}/{NAME}/commits/main")
        if remote.status_code != 200 or remote.json()["sha"] != commit: raise RuntimeError("Push could not be independently verified")
        receipt = {"repository": url, "commit": commit, "public": True,
            "topics_updated": topics.status_code == 200, "scope": "pre-Plugins source snapshot",
            "credentials_persisted": False}
        (ROOT / ".tooling/publication-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    try: main()
    except Exception as error:
        # Do not print HTTP exceptions (may contain request headers or query tokens).
        print(json.dumps({"published": False, "error_type": type(error).__name__,
                          "message": str(error) if isinstance(error, (ValueError, RuntimeError)) else "Publication failed; inspect safe diagnostics"}), flush=True)
        sys.exit(1)
