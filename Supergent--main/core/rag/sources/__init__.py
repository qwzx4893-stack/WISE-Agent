"""Concrete RAG source connectors.

Importing this module registers every default source on the global
:class:`KnowledgeRouter`.
"""
from .academic import (
    ArxivSource, CrossrefSource, OpenAlexSource, SemanticScholarSource,
)
from .general import (
    DictionarySource, DuckDuckGoSource, GitHubReposSource,
    NominatimSource, StackExchangeSource, WikipediaSource,
)
from .medical import PubMedSource
from .security import (
    CVESource, GTFOBinsSource, LOLBASSource, MitreAttackSource,
)


def all_sources():
    """Return a fresh instance of every default source.

    Only keyless sources are registered by default. Sources that strictly
    require an API key (e.g. NewsAPI, paid Exploit-DB) have been removed
    so the RAG layer is fully usable out of the box.
    """
    return [
        # general (6)
        WikipediaSource(),
        DuckDuckGoSource(),
        DictionarySource(),
        NominatimSource(),
        StackExchangeSource(),
        GitHubReposSource(),
        # academic (4)
        ArxivSource(),
        SemanticScholarSource(),
        OpenAlexSource(),
        CrossrefSource(),
        # medical (1)
        PubMedSource(),
        # security (4)
        CVESource(),
        MitreAttackSource(),
        GTFOBinsSource(),
        LOLBASSource(),
    ]


__all__ = [
    # general
    "WikipediaSource", "DuckDuckGoSource", "DictionarySource",
    "NominatimSource", "StackExchangeSource", "GitHubReposSource",
    # academic
    "ArxivSource", "SemanticScholarSource", "OpenAlexSource", "CrossrefSource",
    # medical
    "PubMedSource",
    # security
    "CVESource", "MitreAttackSource", "GTFOBinsSource", "LOLBASSource",
    "all_sources",
]
