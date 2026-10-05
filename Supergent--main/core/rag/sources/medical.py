"""Medical / life-sciences sources."""

from __future__ import annotations

import os
from typing import List

from ..base import KnowledgeSource, SearchResult, http_get


class PubMedSource(KnowledgeSource):
    """NCBI E-utilities — PubMed (biomedical literature) search."""

    name = "pubmed"
    category = "medical"
    description = "PubMed (NCBI E-utilities) biomedical literature search."
    rate_limit = (3, 1.0)  # 3/s without API key, 10/s with one

    def __init__(self) -> None:
        super().__init__()
        if os.environ.get("NCBI_API_KEY"):
            self._limiter = None  # higher quota with key

    def search(self, query: str, *,
               max_results: int = 5) -> List[SearchResult]:
        self._gate()
        params = {"db": "pubmed", "term": query, "retmode": "json",
                  "retmax": max_results}
        api_key = os.environ.get("NCBI_API_KEY")
        if api_key:
            params["api_key"] = api_key
        status, body, _ = http_get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params=params,
        )
        if status != 200 or not isinstance(body, dict):
            return []
        ids = (body.get("esearchresult", {}).get("idlist") or [])[:max_results]
        if not ids:
            return []
        sparams = {"db": "pubmed", "id": ",".join(ids), "retmode": "json"}
        if api_key:
            sparams["api_key"] = api_key
        status, body, _ = http_get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            params=sparams,
        )
        if status != 200 or not isinstance(body, dict):
            return []
        out: List[SearchResult] = []
        for pmid in ids:
            entry = (body.get("result", {}) or {}).get(pmid) or {}
            if not entry:
                continue
            authors = ", ".join(a.get("name", "")
                                  for a in (entry.get("authors") or [])[:5])
            out.append(SearchResult(
                title=entry.get("title", ""),
                snippet=f"{authors} — {entry.get('source', '')} "
                         f"({entry.get('pubdate', '')})"[:1500],
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                source=self.name,
                metadata={"pmid": pmid, "pubdate": entry.get("pubdate"),
                           "source": entry.get("source")},
            ))
        return out


__all__ = ["PubMedSource"]
