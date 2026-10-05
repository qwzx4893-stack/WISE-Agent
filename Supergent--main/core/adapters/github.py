"""GitHub adapter — REST API with optional ``gh`` CLI fallback.

Capabilities exposed as tools:

- ``github_list_issues``      list issues (state, labels, paginated).
- ``github_create_issue``     open an issue.
- ``github_comment_issue``    post a comment.
- ``github_close_issue``      close an issue.
- ``github_list_prs``         list pull requests (state, base, head).
- ``github_get_pr``           PR + review comments.
- ``github_create_pr``        open a PR.
- ``github_merge_pr``         merge a PR (squash/merge/rebase).
- ``github_list_branches``    branches in a repo.
- ``github_create_branch``    create a branch from a base SHA.
- ``github_search_code``      code-search across repos.
- ``github_get_file``         fetch a file's contents at a ref.
- ``github_put_file``         create or update a file.
- ``github_create_release``   tag + release.
- ``github_dispatch_workflow``  trigger a workflow_dispatch run.
- ``github_status``           rate-limit / auth status.
"""

from __future__ import annotations

import base64
import os
import shlex
import subprocess
from typing import Any, Callable, Dict, List, Optional

from .base import AdapterError, BaseAdapter

_API = "https://api.github.com"


class GitHubAdapter(BaseAdapter):
    NAME = "github"
    KEYSTORE_NAMES = ["github", "GITHUB_TOKEN", "GITHUB_API_KEY"]
    ENV_KEYS = ["GITHUB_TOKEN", "GH_TOKEN", "GITHUB_API_KEY"]
    REQUIRED: List[str] = []  # we accept either token or CLI fallback
    CLI_FALLBACK = True
    CLI_BIN = "gh"
    RATE_PER_SEC = 5.0
    RATE_BURST = 10

    # ------------------------------------------------------------------
    def _token(self) -> Optional[str]:
        return self.cred(*self.KEYSTORE_NAMES, *self.ENV_KEYS)

    def _headers(self) -> Dict[str, str]:
        token = self._token()
        if not token:
            raise AdapterError("github: لا يوجد token")
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ------------------------------------------------------------------
    def is_ready(self) -> bool:
        return bool(self._token()) or self._cli_ok

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------
    def list_issues(self, repo: str, *, state: str = "open",
                    labels: str = "", per_page: int = 30,
                    page: int = 1) -> List[Dict[str, Any]]:
        return self._call(
            "list_issues",
            self._http,
            "GET", f"{_API}/repos/{repo}/issues",
            headers=self._headers(),
            params={
                "state": state, "labels": labels,
                "per_page": min(per_page, 100), "page": page,
            },
        )

    def create_issue(self, repo: str, title: str, body: str = "",
                     labels: Optional[List[str]] = None,
                     assignees: Optional[List[str]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if assignees:
            payload["assignees"] = assignees
        return self._call(
            "create_issue", self._http,
            "POST", f"{_API}/repos/{repo}/issues",
            headers=self._headers(), json=payload,
        )

    def comment_issue(self, repo: str, number: int, body: str) -> Dict[str, Any]:
        return self._call(
            "comment_issue", self._http,
            "POST", f"{_API}/repos/{repo}/issues/{number}/comments",
            headers=self._headers(), json={"body": body},
        )

    def close_issue(self, repo: str, number: int,
                    reason: str = "completed") -> Dict[str, Any]:
        return self._call(
            "close_issue", self._http,
            "PATCH", f"{_API}/repos/{repo}/issues/{number}",
            headers=self._headers(),
            json={"state": "closed", "state_reason": reason},
        )

    # ------------------------------------------------------------------
    # PRs
    # ------------------------------------------------------------------
    def list_prs(self, repo: str, *, state: str = "open",
                 base: str = "", head: str = "",
                 per_page: int = 30, page: int = 1) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "state": state, "per_page": min(per_page, 100), "page": page,
        }
        if base:
            params["base"] = base
        if head:
            params["head"] = head
        return self._call(
            "list_prs", self._http,
            "GET", f"{_API}/repos/{repo}/pulls",
            headers=self._headers(), params=params,
        )

    def get_pr(self, repo: str, number: int) -> Dict[str, Any]:
        return self._call(
            "get_pr", self._http,
            "GET", f"{_API}/repos/{repo}/pulls/{number}",
            headers=self._headers(),
        )

    def create_pr(self, repo: str, *, title: str, head: str, base: str,
                  body: str = "", draft: bool = False) -> Dict[str, Any]:
        return self._call(
            "create_pr", self._http,
            "POST", f"{_API}/repos/{repo}/pulls",
            headers=self._headers(),
            json={
                "title": title, "head": head, "base": base,
                "body": body, "draft": draft,
            },
        )

    def merge_pr(self, repo: str, number: int, *,
                 method: str = "squash",
                 commit_title: str = "",
                 commit_message: str = "") -> Dict[str, Any]:
        if method not in ("merge", "squash", "rebase"):
            raise AdapterError(f"github: merge method invalid: {method}")
        payload: Dict[str, Any] = {"merge_method": method}
        if commit_title:
            payload["commit_title"] = commit_title
        if commit_message:
            payload["commit_message"] = commit_message
        return self._call(
            "merge_pr", self._http,
            "PUT", f"{_API}/repos/{repo}/pulls/{number}/merge",
            headers=self._headers(), json=payload,
        )

    # ------------------------------------------------------------------
    # Branches
    # ------------------------------------------------------------------
    def list_branches(self, repo: str, *, per_page: int = 30,
                      page: int = 1) -> List[Dict[str, Any]]:
        return self._call(
            "list_branches", self._http,
            "GET", f"{_API}/repos/{repo}/branches",
            headers=self._headers(),
            params={"per_page": min(per_page, 100), "page": page},
        )

    def create_branch(self, repo: str, branch: str, base_sha: str) -> Dict[str, Any]:
        return self._call(
            "create_branch", self._http,
            "POST", f"{_API}/repos/{repo}/git/refs",
            headers=self._headers(),
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )

    # ------------------------------------------------------------------
    # Files / contents
    # ------------------------------------------------------------------
    def get_file(self, repo: str, path: str,
                 ref: str = "") -> Dict[str, Any]:
        params = {"ref": ref} if ref else None
        result = self._call(
            "get_file", self._http,
            "GET", f"{_API}/repos/{repo}/contents/{path}",
            headers=self._headers(), params=params,
        )
        if isinstance(result, dict) and result.get("encoding") == "base64":
            try:
                result["decoded"] = base64.b64decode(
                    result.get("content", "")
                ).decode("utf-8", errors="replace")
            except Exception:
                pass
        return result

    def put_file(self, repo: str, path: str, *, content: str,
                 message: str, branch: str = "",
                 sha: str = "") -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if branch:
            payload["branch"] = branch
        if sha:
            payload["sha"] = sha
        return self._call(
            "put_file", self._http,
            "PUT", f"{_API}/repos/{repo}/contents/{path}",
            headers=self._headers(), json=payload,
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def search_code(self, query: str, *, per_page: int = 20,
                    page: int = 1) -> Dict[str, Any]:
        return self._call(
            "search_code", self._http,
            "GET", f"{_API}/search/code",
            headers=self._headers(),
            params={"q": query, "per_page": min(per_page, 100), "page": page},
        )

    # ------------------------------------------------------------------
    # Releases / actions
    # ------------------------------------------------------------------
    def create_release(self, repo: str, *, tag: str, name: str = "",
                       body: str = "", draft: bool = False,
                       prerelease: bool = False,
                       target_commitish: str = "") -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "tag_name": tag, "name": name or tag, "body": body,
            "draft": draft, "prerelease": prerelease,
        }
        if target_commitish:
            payload["target_commitish"] = target_commitish
        return self._call(
            "create_release", self._http,
            "POST", f"{_API}/repos/{repo}/releases",
            headers=self._headers(), json=payload,
        )

    def dispatch_workflow(self, repo: str, workflow_id: str, *,
                          ref: str, inputs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._call(
            "dispatch_workflow", self._http,
            "POST", f"{_API}/repos/{repo}/actions/workflows/{workflow_id}/dispatches",
            headers=self._headers(),
            json={"ref": ref, "inputs": inputs or {}},
        )
        return {"dispatched": True, "workflow": workflow_id, "ref": ref}

    # ------------------------------------------------------------------
    def status_call(self) -> Dict[str, Any]:
        token = self._token()
        if not token:
            return {
                "authenticated": False,
                "cli_fallback_available": self._cli_ok,
            }
        rate = self._call(
            "status", self._http,
            "GET", f"{_API}/rate_limit", headers=self._headers(),
        )
        return {"authenticated": True, "rate_limit": rate.get("rate", {})}

    # ------------------------------------------------------------------
    # CLI fallback (best-effort)
    # ------------------------------------------------------------------
    def _gh(self, *args: str) -> str:
        if not self._cli_ok:
            raise AdapterError("github: gh CLI غير متاح")
        cp = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=60,
        )
        if cp.returncode != 0:
            raise AdapterError(f"gh CLI failed: {cp.stderr.strip()[:300]}")
        return cp.stdout

    # ------------------------------------------------------------------
    # Tool surface
    # ------------------------------------------------------------------
    def tools_for(self) -> Dict[str, Callable[..., Any]]:
        return {
            "github_list_issues": self.list_issues,
            "github_create_issue": self.create_issue,
            "github_comment_issue": self.comment_issue,
            "github_close_issue": self.close_issue,
            "github_list_prs": self.list_prs,
            "github_get_pr": self.get_pr,
            "github_create_pr": self.create_pr,
            "github_merge_pr": self.merge_pr,
            "github_list_branches": self.list_branches,
            "github_create_branch": self.create_branch,
            "github_get_file": self.get_file,
            "github_put_file": self.put_file,
            "github_search_code": self.search_code,
            "github_create_release": self.create_release,
            "github_dispatch_workflow": self.dispatch_workflow,
            "github_status": self.status_call,
        }


__all__ = ["GitHubAdapter"]
