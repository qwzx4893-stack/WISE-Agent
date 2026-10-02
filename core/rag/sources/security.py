"""Security-focused knowledge sources."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

from ..base import KnowledgeSource, SearchResult, http_get


class CVESource(KnowledgeSource):
    """NIST NVD CVE 2.0 API."""

    name = "cve_nvd"
    category = "security"
    description = "NIST NVD — CVE vulnerability database."
    rate_limit = (5, 30.0)  # public quota

    def search(self, query: str, *,
               max_results: int = 5) -> List[SearchResult]:
        self._gate()
        params: Dict[str, Any] = {"resultsPerPage": max_results}
        api_key = os.environ.get("NVD_API_KEY")
        headers = {"apiKey": api_key} if api_key else {}
        # Heuristic: bare CVE ids hit a different endpoint.
        m = re.match(r"^CVE-\d{4}-\d{4,7}$", (query or "").strip(),
                      re.IGNORECASE)
        if m:
            params["cveId"] = m.group(0).upper()
        else:
            params["keywordSearch"] = query
        status, body, _ = http_get(
            "https://services.nvd.nist.gov/rest/json/cves/2.0",
            params=params, headers=headers, timeout=15,
        )
        if status != 200 or not isinstance(body, dict):
            return []
        out: List[SearchResult] = []
        for v in (body.get("vulnerabilities") or [])[:max_results]:
            cve = v.get("cve", {})
            cve_id = cve.get("id", "")
            descs = cve.get("descriptions") or []
            desc_en = next(
                (d.get("value", "") for d in descs if d.get("lang") == "en"),
                (descs[0].get("value", "") if descs else ""),
            )
            metrics = (cve.get("metrics") or {}).get("cvssMetricV31") or []
            score = (metrics[0].get("cvssData", {}).get("baseScore")
                     if metrics else None)
            out.append(SearchResult(
                title=cve_id,
                snippet=desc_en[:1500],
                url=f"https://nvd.nist.gov/vuln/detail/{cve_id}",
                source=self.name,
                metadata={"cvss_v3": score,
                           "published": cve.get("published"),
                           "modified": cve.get("lastModified")},
            ))
        return out


# ---------------------------------------------------------------------------
class MitreAttackSource(KnowledgeSource):
    """MITRE ATT&CK — pulls from the official cti GitHub repo. Searches
    technique names + descriptions on a small in-memory index built on
    first use, so we don't repeatedly download the 50MB STIX bundle.
    """

    name = "mitre_attack"
    category = "security"
    description = "MITRE ATT&CK — adversarial tactics and techniques."

    _index: List[Dict[str, Any]] = []

    def _ensure_index(self) -> None:
        if self._index:
            return
        # Use the lightweight markdown-style index that GitHub serves.
        status, body, _ = http_get(
            "https://attack.mitre.org/api.php",
            params={"action": "ask",
                     "query": "[[Category:Technique]]|?Has display name|?Has technique ID|limit=900",
                     "format": "json"},
            timeout=20,
        )
        if status == 200 and isinstance(body, dict):
            for k, v in (body.get("query", {}).get("results", {}) or {}).items():
                printouts = v.get("printouts", {})
                tech_id = (printouts.get("Has technique ID") or [""])[0]
                name = (printouts.get("Has display name") or [k])[0]
                self._index.append({
                    "id": tech_id, "name": name,
                    "url": v.get("fullurl", f"https://attack.mitre.org/techniques/{tech_id}/"),
                })
            return
        # Fallback: a small hard-coded set of well-known techniques so the
        # source remains useful in air-gapped environments.
        self._index = [
            {"id": "T1059", "name": "Command and Scripting Interpreter",
             "url": "https://attack.mitre.org/techniques/T1059/"},
            {"id": "T1078", "name": "Valid Accounts",
             "url": "https://attack.mitre.org/techniques/T1078/"},
            {"id": "T1190", "name": "Exploit Public-Facing Application",
             "url": "https://attack.mitre.org/techniques/T1190/"},
            {"id": "T1566", "name": "Phishing",
             "url": "https://attack.mitre.org/techniques/T1566/"},
            {"id": "T1486", "name": "Data Encrypted for Impact",
             "url": "https://attack.mitre.org/techniques/T1486/"},
        ]

    def search(self, query: str, *,
               max_results: int = 5) -> List[SearchResult]:
        self._gate()
        self._ensure_index()
        q = (query or "").lower().strip()
        out: List[SearchResult] = []
        for entry in self._index:
            if (q in entry["name"].lower() or q in entry["id"].lower()
                    or q == ""):
                out.append(SearchResult(
                    title=f"{entry['id']}: {entry['name']}",
                    snippet=f"MITRE ATT&CK technique {entry['id']}.",
                    url=entry["url"],
                    source=self.name,
                    metadata={"technique_id": entry["id"]},
                ))
            if len(out) >= max_results:
                break
        return out


# ---------------------------------------------------------------------------
class GTFOBinsSource(KnowledgeSource):
    """GTFOBins — Unix binary exploitation techniques. Index is built
    from the project's published JSON list."""

    name = "gtfobins"
    category = "security"
    description = "GTFOBins — Unix binaries useful for privilege escalation."
    _index: List[str] = []

    def _ensure_index(self) -> None:
        if self._index:
            return
        status, body, _ = http_get(
            "https://gtfobins.github.io/gtfobins.json", timeout=15,
        )
        if status == 200 and isinstance(body, dict):
            self._index = sorted(body.keys())

    def search(self, query: str, *,
               max_results: int = 5) -> List[SearchResult]:
        self._gate()
        self._ensure_index()
        q = (query or "").lower().strip()
        out: List[SearchResult] = []
        for bin_name in self._index:
            if q in bin_name.lower() or q == "":
                out.append(SearchResult(
                    title=bin_name,
                    snippet=f"GTFOBins entry for `{bin_name}`.",
                    url=f"https://gtfobins.github.io/gtfobins/{bin_name}/",
                    source=self.name,
                    metadata={"binary": bin_name},
                ))
            if len(out) >= max_results:
                break
        return out


# ---------------------------------------------------------------------------
class LOLBASSource(KnowledgeSource):
    """LOLBAS — Living Off The Land Binaries (Windows). Pulls the list
    from the project's public JSON manifest."""

    name = "lolbas"
    category = "security"
    description = ("LOLBAS — Living Off The Land Binaries, Scripts and "
                    "Libraries (Windows attack techniques).")
    _index: List[Dict[str, Any]] = []

    def _ensure_index(self) -> None:
        if self._index:
            return
        status, body, _ = http_get(
            "https://lolbas-project.github.io/api/lolbas.json", timeout=15,
        )
        if status == 200 and isinstance(body, list):
            self._index = body

    def search(self, query: str, *,
               max_results: int = 5) -> List[SearchResult]:
        self._gate()
        self._ensure_index()
        q = (query or "").lower().strip()
        out: List[SearchResult] = []
        for entry in self._index:
            name = (entry.get("Name") or "").lower()
            desc = (entry.get("Description") or "").lower()
            if q in name or q in desc or q == "":
                out.append(SearchResult(
                    title=entry.get("Name", ""),
                    snippet=(entry.get("Description") or "")[:1500],
                    url=entry.get("url",
                                   f"https://lolbas-project.github.io/lolbas/Binaries/{entry.get('Name', '')}/"),
                    source=self.name,
                    metadata={"category": entry.get("Category")},
                ))
            if len(out) >= max_results:
                break
        return out


__all__ = [
    "CVESource", "MitreAttackSource", "GTFOBinsSource", "LOLBASSource",
]
