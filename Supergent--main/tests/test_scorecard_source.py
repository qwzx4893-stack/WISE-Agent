"""Bounded public Scorecard contracts; fixture transport is explicitly offline."""
import copy
import pytest
from core.intelligence.scorecard_source import API_ROOT, normalize_repository, query_scorecard


@pytest.fixture
def observation():
    data = {"date": "2026-10-03T01:52:14Z", "repo": {"name": "github.com/ossf/scorecard", "commit": "f" * 40},
            "score": 8.4, "scorecard": {"version": "v5.5.0"},
            "checks": [{"name": "Code-Review", "score": 6, "reason": "observed fixture", "details": ["do not expose"]}]}
    meta = {"source_url": API_ROOT + "github.com/ossf/scorecard", "retrieved_at": "2026-10-04T00:00:00+00:00",
            "content_sha256": "a" * 64, "evidence_kind": "source_api_data", "cache_hit": False}
    return data, meta


@pytest.mark.parametrize("query", ["ossf/scorecard", "github.com/OSSF/Scorecard", "https://github.com/ossf/scorecard"])
def test_normalized_fixed_public_repository(query):
    assert normalize_repository(query) == "github.com/ossf/scorecard"


@pytest.mark.parametrize("query", ["", "file:///private", "https://127.0.0.1/a/b", "https://github.com/u:p@host/r",
    "ossf/../private", "ossf/%2e%2e", "ossf/repo?token=secret", "ossf/repo#fragment", "ossf/repo.git",
    "ossf/scorecard\n", "ossf/..", "a" * 171, "ossf/ghp_" + "a" * 36, "ossf/sk-" + "a" * 30, None])
def test_invalid_input_never_reaches_network(query):
    def forbidden(*args):
        pytest.fail("Invalid repository reached network")
    with pytest.raises(ValueError):
        query_scorecard(query, forbidden)


def test_observed_fields_and_receipts_not_fresh_scan(observation):
    urls = []
    result = query_scorecard("ossf/scorecard", lambda url: (urls.append(url) or copy.deepcopy(observation)))
    assert urls == [API_ROOT + "github.com/ossf/scorecard"]
    assert result["records"][0]["scan_date"] != result["provenance"]["retrieved_at"]
    assert result["coverage"] == "PUBLIC_PRECALCULATED_SCORECARD_ONLY"
    assert "details" not in result["records"][0]["checks"][0]
    assert len(result["documented_optional_checks_missing"]) == 3


def test_optional_checks_are_observed_not_assumed_omitted(observation):
    data, meta = observation
    for name in ("CI-Tests", "Contributors", "Dependency-Update-Tool"):
        data["checks"].append({"name": name, "score": -1, "reason": "not available"})
    assert query_scorecard("ossf/scorecard", lambda url: observation)["documented_optional_checks_missing"] == []


@pytest.mark.parametrize("fault", ["repo", "repo_type", "date", "score", "bool_score", "checks", "duplicate", "oversized_reason",
    "oversized_checks", "check_score", "provenance", "retrieved", "digest"])
def test_malformed_response_fails_closed(observation, fault):
    data, meta = observation
    if fault == "repo": data["repo"]["name"] = "github.com/other/repo"
    if fault == "repo_type": data["repo"]["name"] = 12
    if fault == "date": data.pop("date")
    if fault == "score": data["score"] = float("nan")
    if fault == "bool_score": data["score"] = True
    if fault == "checks": data["checks"] = []
    if fault == "duplicate": data["checks"] *= 2
    if fault == "oversized_reason": data["checks"][0]["reason"] = "x" * 2001
    if fault == "oversized_checks": data["checks"] *= 65
    if fault == "check_score": data["checks"][0]["score"] = 11
    if fault == "provenance": meta["source_url"] = "https://other.test/unknown"
    if fault == "retrieved": meta["retrieved_at"] = "2026-10-04"
    if fault == "digest": meta.pop("content_sha256")
    with pytest.raises(ValueError): query_scorecard("ossf/scorecard", lambda url: observation)


def test_transport_failure_is_not_an_empty_success():
    def unavailable(url): raise RuntimeError("404 unavailable public result")
    with pytest.raises(RuntimeError, match="unavailable"):
        query_scorecard("ossf/scorecard", unavailable)


def test_untrusted_details_dropped_known_credentials_redacted(observation):
    data, meta = observation
    data["checks"][0]["reason"] = "public finding ghp_" + "a" * 36
    result = query_scorecard("ossf/scorecard", lambda url: observation)
    assert "ghp_" + "a" * 36 not in repr(result)
    assert "do not expose" not in repr(result)


def test_canonical_source_execution_and_router_binding(monkeypatch, observation):
    import core.intelligence.sources as sources
    from core.intelligence.inventory import profile
    from core.intelligence.bindings import register_intelligence
    monkeypatch.setattr(sources, "_json", lambda url: observation)
    resource = profile("openssf-scorecard")
    assert resource["kind"] == "source" and resource["available"]
    assert resource["actions"] == ["read", "query"]
    descriptors = []
    class Router:
        def register(self, descriptor): descriptors.append(descriptor)
    register_intelligence(Router())
    descriptor = next(item for item in descriptors if item.id == "source.openssf-scorecard")
    result = descriptor.execution_adapter({"action": "query", "query": "ossf/scorecard"})
    assert result["records"][0]["repository"] == "github.com/ossf/scorecard"
    assert result["resource_id"] == "openssf-scorecard" and result["success"]
