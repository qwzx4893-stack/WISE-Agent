"""GitLab adapter — REST API v4.

Capabilities exposed as tools:

- ``gitlab_list_issues``    list issues (state).
- ``gitlab_create_issue``   open an issue.
- ``gitlab_list_mrs``       list merge requests.
- ``gitlab_create_mr``      open a merge request.
- ``gitlab_get_file``       fetch file at ref.
- ``gitlab_status``         identity + version.

Project IDs may be numeric or URL-encoded path (e.g. ``group%2Fproject``).
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from .base import AdapterError, BaseAdapter


class GitLabAdapter(BaseAdapter):
    NAME = "gitlab"
    KEYSTORE_NAMES = ["gitlab", "GITLAB_TOKEN"]
    ENV_KEYS = ["GITLAB_TOKEN", "GITLAB_API_KEY"]
    REQUIRED: List[str] = ["GITLAB_TOKEN"]
    CLI_FALLBACK = False
    RATE_PER_SEC = 5.0
    RATE_BURST = 10

    # ------------------------------------------------------------------
    def _token(self) -> Optional[str]:
        return self.cred(*self.KEYSTORE_NAMES, *self.ENV_KEYS)

    def _base(self) -> str:
        return self.cred("GITLAB_URL") or "https://gitlab.com/api/v4"

    def _headers(self) -> Dict[str, str]:
        token = self._token()
        if not token:
            raise AdapterError("gitlab: لا يوجد token")
        return {"PRIVATE-TOKEN": token}

    def is_ready(self) -> bool:
        return bool(self._token())

    # ------------------------------------------------------------------
    @staticmethod
    def _project(pid: Any) -> str:
        if isinstance(pid, int):
            return str(pid)
        if isinstance(pid, str) and "/" in pid:
            return urllib.parse.quote_plus(pid)
        return str(pid)

    # ------------------------------------------------------------------
    def list_issues(self, project: Any, *, state: str = "opened",
                    per_page: int = 30) -> List[Dict[str, Any]]:
        return self._call(
            "list_issues", self._http,
            "GET", f"{self._base()}/projects/{self._project(project)}/issues",
            headers=self._headers(),
            params={"state": state, "per_page": min(per_page, 100)},
        )

    def create_issue(self, project: Any, title: str,
                     description: str = "",
                     labels: Optional[List[str]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"title": title, "description": description}
        if labels:
            payload["labels"] = ",".join(labels)
        return self._call(
            "create_issue", self._http,
            "POST", f"{self._base()}/projects/{self._project(project)}/issues",
            headers=self._headers(), json=payload,
        )

    def list_mrs(self, project: Any, *, state: str = "opened",
                 per_page: int = 30) -> List[Dict[str, Any]]:
        return self._call(
            "list_mrs", self._http,
            "GET", f"{self._base()}/projects/{self._project(project)}/merge_requests",
            headers=self._headers(),
            params={"state": state, "per_page": min(per_page, 100)},
        )

    def create_mr(self, project: Any, *, title: str,
                  source_branch: str, target_branch: str,
                  description: str = "") -> Dict[str, Any]:
        return self._call(
            "create_mr", self._http,
            "POST", f"{self._base()}/projects/{self._project(project)}/merge_requests",
            headers=self._headers(),
            json={
                "title": title, "source_branch": source_branch,
                "target_branch": target_branch, "description": description,
            },
        )

    def get_file(self, project: Any, path: str, ref: str = "main") -> Dict[str, Any]:
        encoded = urllib.parse.quote_plus(path)
        return self._call(
            "get_file", self._http,
            "GET", f"{self._base()}/projects/{self._project(project)}/repository/files/{encoded}",
            headers=self._headers(), params={"ref": ref},
        )

    def status_call(self) -> Dict[str, Any]:
        if not self._token():
            return {"authenticated": False}
        try:
            user = self._call(
                "user", self._http,
                "GET", f"{self._base()}/user", headers=self._headers(),
            )
            return {"authenticated": True, "user": user.get("username")}
        except AdapterError as exc:
            return {"authenticated": False, "error": str(exc)}

    # ------------------------------------------------------------------
    def tools_for(self) -> Dict[str, Callable[..., Any]]:
        return {
            "gitlab_list_issues": self.list_issues,
            "gitlab_create_issue": self.create_issue,
            "gitlab_list_mrs": self.list_mrs,
            "gitlab_create_mr": self.create_mr,
            "gitlab_get_file": self.get_file,
            "gitlab_status": self.status_call,
        }


__all__ = ["GitLabAdapter"]
