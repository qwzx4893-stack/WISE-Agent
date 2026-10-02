"""Academic knowledge sources."""

from __future__ import annotations

import os
import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException
from typing import List

from ..base import KnowledgeSource, SearchResult, http_get


class ArxivSource(KnowledgeSource):
    name = "arxiv"
    category = "academic"
    description = "arXiv.org pre-prints (physics, CS, math, stats)."
    rate_limit = (10, 60.0)

    def search(self, query: str, *,
               max_results: int = 5) -> List[SearchResult]:
        self._gate()
        status, body, _ = http_get(
            "http://export.arxiv.org/api/query",
            params={"search_query": f"all:{query}",
                     "start": 0, "max_results": max_results,
                     "sortBy": "relevance", "sortOrder": "descending"},
        )
        if status != 200 or not isinstance(body, str):
            return []
        try:
            ns = {"a": "http://www.w3.org/2005/Atom"}
            if len(body) > 4 * 1024 * 1024:
                return []
            root = ET.fromstring(body)
        except (ET.ParseError, DefusedXmlException):
            return []
        out: List[SearchResult] = []
        for entry in root.findall("a:entry", ns)[:max_results]:
            title = (entry.findtext("a:title", default="", namespaces=ns)
                      or "").strip()
            summary = (entry.findtext("a:summary", default="",
                                       namespaces=ns) or "").strip()
            url = (entry.findtext("a:id", default="", namespaces=ns)
                   or "").strip()
            published = entry.findtext("a:published", default="",
                                        namespaces=ns)
            authors = [a.findtext("a:name", default="", namespaces=ns)
                       for a in entry.findall("a:author", ns)]
            out.append(SearchResult(
                title=title,
                snippet=summary.replace("\n", " ")[:1500],
                url=url,
                source=self.name,
                metadata={"authors": authors, "published": published},
            ))
        return out


class SemanticScholarSource(KnowledgeSource):
    name = "semantic_scholar"
    category = "academic"
    description = "Semantic Scholar paper search."
    rate_limit = (100, 300.0)

    def search(self, query: str, *,
               max_results: int = 5) -> List[SearchResult]:
        self._gate()
        headers = {}
        api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        if api_key:
            headers["x-api-key"] = api_key
        status, body, _ = http_get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={"query": query, "limit": max_results,
                     "fields": "title,abstract,year,authors,url,venue"},
            headers=headers,
        )
        if status != 200 or not isinstance(body, dict):
            return []
        out: List[SearchResult] = []
        for p in body.get("data", [])[:max_results]:
            out.append(SearchResult(
                title=p.get("title") or "",
                snippet=(p.get("abstract") or "")[:1500],
                url=p.get("url", ""),
                source=self.name,
                metadata={"year": p.get("year"),
                           "venue": p.get("venue"),
                           "authors": [a.get("name")
                                        for a in p.get("authors", [])]},
            ))
        return out


class OpenAlexSource(KnowledgeSource):
    name = "openalex"
    category = "academic"
    description = "OpenAlex.org open metadata index of scholarly works."
    rate_limit = (60, 60.0)

    def search(self, query: str, *,
               max_results: int = 5) -> List[SearchResult]:
        self._gate()
        mailto = os.environ.get("OPENALEX_EMAIL", "agent-os@example.org")
        status, body, _ = http_get(
            "https://api.openalex.org/works",
            params={"search": query, "per-page": max_results,
                     "mailto": mailto},
        )
        if status != 200 or not isinstance(body, dict):
            return []
        out: List[SearchResult] = []
        for w in body.get("results", [])[:max_results]:
            doi = w.get("doi") or ""
            url = doi if doi.startswith("http") else (
                f"https://doi.org/{doi}" if doi else (w.get("id") or "")
            )
            out.append(SearchResult(
                title=w.get("title") or w.get("display_name") or "",
                snippet=(w.get("abstract_inverted_index_text") or
                          w.get("abstract") or "")[:1500],
                url=url,
                source=self.name,
                metadata={"year": w.get("publication_year"),
                           "doi": doi,
                           "cited_by_count": w.get("cited_by_count")},
            ))
        return out


class CrossrefSource(KnowledgeSource):
    name = "crossref"
    category = "academic"
    description = "Crossref — DOI metadata for scholarly publications."
    rate_limit = (50, 60.0)

    def search(self, query: str, *,
               max_results: int = 5) -> List[SearchResult]:
        self._gate()
        status, body, _ = http_get(
            "https://api.crossref.org/works",
            params={"query": query, "rows": max_results},
            headers={"User-Agent":
                      "AgentOS-RAG/1.1 (mailto:agent-os@example.org)"},
        )
        if status != 200 or not isinstance(body, dict):
            return []
        out: List[SearchResult] = []
        for w in (body.get("message", {}).get("items") or [])[:max_results]:
            title = (w.get("title") or [""])[0]
            authors = ", ".join(
                f"{a.get('given', '')} {a.get('family', '')}".strip()
                for a in (w.get("author") or [])[:5]
            )
            out.append(SearchResult(
                title=title,
                snippet=(w.get("abstract") or
                          f"{authors} — {w.get('container-title', [''])[0]}")[:1500],
                url=w.get("URL") or
                     (f"https://doi.org/{w['DOI']}" if w.get("DOI") else ""),
                source=self.name,
                metadata={"doi": w.get("DOI"),
                           "type": w.get("type"),
                           "issued": w.get("issued")},
            ))
        return out


__all__ = [
    "ArxivSource", "SemanticScholarSource",
    "OpenAlexSource", "CrossrefSource",
]
