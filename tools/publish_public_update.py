"""Publish a verified, isolated checkout to existing main without force.

Use --verify-only without credentials. For --publish, supply an ephemeral token
on stdin (not an argument, URL or saved config). No API response bodies or Git
stderr are printed: these can contain unexpected sensitive data.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "Supergent--main"
REPO = "qwzx4893-stack/WISE-Agent"
URL = "https://github.com/" + REPO + ".git"
DESCRIPTION = "Windows-first, local-first Agent OS: cognition and multi-agent execution, capability/skill/MCP routing, research, durable state, desktop automation, model/resource management and optional voice. Development preview."
TOPICS = ["agent-os", "ai-agent", "multi-agent", "windows", "desktop-application", "local-first", "mcp", "skills", "automation", "llm", "research", "voice-assistant", "fastapi", "python"]


def git(checkout, *args, env=None):
    p = subprocess.run(["git", "-C", str(checkout), *args], env=env, capture_output=True, text=True, encoding="utf-8")
    if p.returncode:
        raise RuntimeError("Git operation failed: " + args[0] + " (stderr withheld)")
    return p.stdout.strip()


def verify(checkout):
    checkout = checkout.resolve()
    if not checkout.is_relative_to(APP / ".tooling") or checkout == APP / ".tooling" or not (checkout / ".git").is_dir():
        raise ValueError("Expected an isolated publication clone in app .tooling")
    if git(checkout, "remote", "get-url", "origin") != URL or git(checkout, "branch", "--show-current") != "main":
        raise ValueError("Unexpected repository or branch")
    manifest = json.loads((checkout / "PUBLICATION_MANIFEST.json").read_text(encoding="utf-8"))
    if manifest.get("repository") != REPO or manifest.get("schema") != "wise.public-workspace.v1":
        raise ValueError("Unexpected manifest")
    names = set()
    for record in manifest["files"]:
        name = record["path"]
        path = checkout / name
        if name in names or not path.resolve().is_relative_to(checkout) or path.is_symlink():
            raise ValueError("Unsafe source path")
        data = path.read_bytes()
        if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError("Source changed: " + name)
        names.add(name)
    if set(manifest["required"]) - names:
        raise ValueError("Required source missing")
    # Validate tracked file set after reconciliation, not just manifest claims.
    tracked = set(git(checkout, "ls-files").splitlines())
    if tracked != names | {"PUBLICATION_MANIFEST.json"}:
        raise ValueError("Git index differs from reviewed payload")
    return manifest


def publish(checkout):
    manifest = verify(checkout)
    print("VERIFIED; waiting for ephemeral token on stdin", flush=True)
    token = (getpass.getpass("GitHub token (not saved): ") if sys.stdin.isatty() else sys.stdin.readline()).strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError("A nonempty token is required")

    def api(path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("https://api.github.com" + path, data=data, method=method,
            headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "WISE-source-publisher", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            raise RuntimeError("GitHub API status " + str(error.code) + " at " + path) from None

    owner = api("/user")
    if owner["login"].casefold() != "qwzx4893-stack":
        raise ValueError("Token owner does not match requested repository account")
    remote = api("/repos/" + REPO)
    if remote["private"] or remote["default_branch"] != "main" or not remote.get("permissions", {}).get("push"):
        raise ValueError("Repository visibility/branch/push permissions differ from request")
    remote_sha = api("/repos/" + REPO + "/git/ref/heads/main")["object"]["sha"]
    parent = git(checkout, "rev-parse", "HEAD")
    if remote_sha != parent:
        raise RuntimeError("Remote main advanced; review/rebase before publication")
    if not git(checkout, "diff", "--cached", "--name-only"):
        raise ValueError("No staged source update")
    git(checkout, "config", "user.name", owner["login"])
    git(checkout, "config", "user.email", f'{owner["id"]}+{owner["login"]}@users.noreply.github.com')
    git(checkout, "commit", "-m", "Publish current WISE Agent OS workspace, setup and architecture documentation")
    sha = git(checkout, "rev-parse", "HEAD")
    env = os.environ.copy()
    env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
        GIT_CONFIG_VALUE_0="Authorization: Basic " + base64.b64encode(("x-access-token:" + token).encode()).decode(), GIT_TERMINAL_PROMPT="0")
    git(checkout, "push", "origin", "HEAD:refs/heads/main", env=env)
    env.pop("GIT_CONFIG_VALUE_0", None)
    confirmed = api("/repos/" + REPO + "/git/ref/heads/main")["object"]["sha"]
    if confirmed != sha:
        raise RuntimeError("Remote commit confirmation failed")
    print("SOURCE_PUSH_CONFIRMED " + sha, flush=True)
    api("/repos/" + REPO, "PATCH", {"description": DESCRIPTION, "has_issues": True})
    old_topics = api("/repos/" + REPO + "/topics").get("names", [])
    topics = list(dict.fromkeys(TOPICS + old_topics))[:20]
    api("/repos/" + REPO + "/topics", "PUT", {"names": topics})
    metadata = api("/repos/" + REPO)
    actual_topics = api("/repos/" + REPO + "/topics").get("names", [])
    if metadata["description"] != DESCRIPTION or set(topics) != set(actual_topics):
        raise RuntimeError("Repository metadata verification failed")
    receipt = {"repository": "https://github.com/" + REPO, "branch": "main", "parent": parent, "commit": sha,
        "files": len(manifest["files"]), "source_stamp": manifest["source_stamp"], "description": DESCRIPTION,
        "topics": actual_topics, "credentials_saved": False, "push_verified": True}
    (APP / ".tooling/publication-current-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    try:
        if args.publish:
            publish(args.checkout)
        else:
            result = verify(args.checkout)
            print(json.dumps({"verification": "PASS", "files": len(result["files"]), "source_stamp": result["source_stamp"]}))
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        raise SystemExit(1)
