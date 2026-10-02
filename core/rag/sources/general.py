"""General-purpose RAG sources."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

from ..base import KnowledgeSource, SearchResult, http_get

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


# ---------------------------------------------------------------------------
class WikipediaSource(KnowledgeSource):
    name = "wikipedia"
    category = "general"
    description = "Wikipedia article search via MediaWiki API."
    rate_limit = (200, 60.0)

    def search(self, query: str, *,
               max_results: int = 5) -> List[SearchResult]:
        self._gate()
        status, body, err = http_get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "format": "json",
                     "list": "search", "srsearch": query,
                     "srlimit": max_results},
        )
        if status != 200 or not isinstance(body, dict):
            return []
        out: List[SearchResult] = []
        for r in body.get("query", {}).get("search", [])[:max_results]:
            out.append(SearchResult(
                title=r.get("title", ""),
                snippet=_strip_html(r.get("snippet", "")),
                url=f"https://en.wikipedia.org/?curid={r.get('pageid')}",
                source=self.name,
                metadata={"pageid": r.get("pageid")},
            ))
        return out


# ---------------------------------------------------------------------------
class DuckDuckGoSource(KnowledgeSource):
    """Instant-Answer API. Limited to a single 'definitive' answer."""

    name = "duckduckgo"
    category = "general"
    description = "DuckDuckGo Instant Answers — quick disambiguation results."
    rate_limit = (60, 60.0)

    def search(self, query: str, *,
               max_results: int = 5) -> List[SearchResult]:
        self._gate()
        status, body, _ = http_get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1,
                     "skip_disambig": 1},
        )
        if status != 200 or not isinstance(body, dict):
            return []
        results: List[SearchResult] = []
        if body.get("AbstractText"):
            results.append(SearchResult(
                title=body.get("Heading", query),
                snippet=body["AbstractText"],
                url=body.get("AbstractURL", ""),
                source=self.name,
                metadata={"abstract_source": body.get("AbstractSource")},
            ))
        for topic in body.get("RelatedTopics", [])[: max(0, max_results - 1)]:
            if not isinstance(topic, dict) or "Text" not in topic:
                continue
            results.append(SearchResult(
                title=topic.get("Text", "").split(" - ")[0][:120],
                snippet=topic.get("Text", ""),
                url=topic.get("FirstURL", ""),
                source=self.name,
            ))
        return results[:max_results]


# ---------------------------------------------------------------------------
class DictionarySource(KnowledgeSource):
    name = "dictionary"
    category = "general"
    description = "Free Dictionary API — definitions, parts of speech, etymology."

    def search(self, query: str, *,
               max_results: int = 5) -> List[SearchResult]:
        self._gate()
        word = (query or "").strip().split()[0] if query.strip() else ""
        if not word:
            return []
        status, body, _ = http_get(
            f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}",
        )
        if status != 200 or not isinstance(body, list):
            return []
        out: List[SearchResult] = []
        for entry in body[:max_results]:
            meanings = entry.get("meanings", [])
            defs = []
            for m in meanings[:3]:
                for d in m.get("definitions", [])[:2]:
                    defs.append(f"({m.get('partOfSpeech', '')}) "
                                 f"{d.get('definition', '')}")
            out.append(SearchResult(
                title=entry.get("word", word),
                snippet="\n".join(defs)[:1200],
                url=f"https://www.dictionary.com/browse/{word}",
                source=self.name,
                metadata={"phonetic": entry.get("phonetic", "")},
            ))
        return out


# ---------------------------------------------------------------------------
class NominatimSource(KnowledgeSource):
    name = "nominatim"
    category = "geospatial"
    description = "OpenStreetMap Nominatim — geocoding & place lookup."
    rate_limit = (1, 1.0)  # OSM policy: 1 req/s

    def search(self, query: str, *,
               max_results: int = 5) -> List[SearchResult]:
        self._gate()
        status, body, _ = http_get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": max_results,
                     "addressdetails": 1},
        )
        if status != 200 or not isinstance(body, list):
            return []
        out: List[SearchResult] = []
        for hit in body[:max_results]:
            out.append(SearchResult(
                title=hit.get("display_name", query),
                snippet=f"lat={hit.get('lat')}, lon={hit.get('lon')}, "
                         f"class={hit.get('class')}",
                url=f"https://www.openstreetmap.org/?mlat={hit.get('lat')}"
                     f"&mlon={hit.get('lon')}",
                source=self.name,
                metadata={"lat": hit.get("lat"), "lon": hit.get("lon"),
                           "class": hit.get("class"),
                           "type": hit.get("type")},
            ))
        return out


# ---------------------------------------------------------------------------
class StackExchangeSource(KnowledgeSource):
    name = "stackexchange"
    category = "qa"
    description = ("Stack Exchange (defaults to Stack Overflow). Top "
                    "questions matching the query.")
    rate_limit = (30, 60.0)

    def __init__(self, *, site: str = "stackoverflow") -> None:
        super().__init__()
        self.site = site

    def search(self, query: str, *,
               max_results: int = 5) -> List[SearchResult]:
        self._gate()
        status, body, _ = http_get(
            "https://api.stackexchange.com/2.3/search/advanced",
            params={"order": "desc", "sort": "relevance", "q": query,
                     "site": self.site, "pagesize": max_results,
                     "filter": "default"},
        )
        if status != 200 or not isinstance(body, dict):
            return []
        out: List[SearchResult] = []
        for q in body.get("items", [])[:max_results]:
            out.append(SearchResult(
                title=q.get("title", ""),
                snippet=(f"score={q.get('score')}, "
                          f"answers={q.get('answer_count')}, "
                          f"tags={','.join(q.get('tags', []))}"),
                url=q.get("link", ""),
                source=self.name,
                metadata={"score": q.get("score"),
                           "is_answered": q.get("is_answered")},
            ))
        return out


# ---------------------------------------------------------------------------
class GitHubReposSource(KnowledgeSource):
    name = "github_repos"
    category = "code"
    description = "GitHub repository search via the public Search API."
    rate_limit = (10, 60.0)  # 10 requests/minute unauthenticated

    def search(self, query: str, *,
               max_results: int = 5) -> List[SearchResult]:
        self._gate()
        headers: Dict[str, str] = {"Accept": "application/vnd.github+json"}
        try:
            from ...secrets_store import get_secret
            token = get_secret("github_token")
        except Exception:
            token = None
        if not token:
            token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        status, body, _ = http_get(
            "https://api.github.com/search/repositories",
            params={"q": query, "per_page": max_results,
                     "sort": "stars", "order": "desc"},
            headers=headers,
        )
        if status != 200 or not isinstance(body, dict):
            return []
        out: List[SearchResult] = []
        for r in body.get("items", [])[:max_results]:
            out.append(SearchResult(
                title=r.get("full_name", ""),
                snippet=(f"⭐{r.get('stargazers_count', 0)}  "
                          f"{r.get('description', '')}"),
                url=r.get("html_url", ""),
                source=self.name,
                metadata={"stars": r.get("stargazers_count"),
                           "language": r.get("language"),
                           "forks": r.get("forks_count")},
            ))
        return out


__all__ = [
    "WikipediaSource", "DuckDuckGoSource", "DictionarySource",
    "NominatimSource", "StackExchangeSource", "GitHubReposSource",
]
