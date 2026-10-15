"""Bounded generative ingress contracts; no server, network or owner state.

These exercise the actual parser and admission policy, not an assertion that
installing a fuzzing tool equals auditing the product. Cases are deterministic
and the Hypothesis example database is disabled for isolated reproducibility.
"""
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
from starlette.datastructures import Headers

from core.security.http_origin import _origin, ingress_allowed


campaign = settings(max_examples=150, deadline=None, derandomize=True, database=None)


def connection(headers):
    return SimpleNamespace(headers=Headers(raw=[(key.encode("ascii"), value.encode("latin-1"))
                                               for key, value in headers]),
                           scope={"scheme": "http"},
                           client=SimpleNamespace(host="127.0.0.1"))


def admits(headers):
    return ingress_allowed(connection(headers), configured_host="127.0.0.1",
                           allowed_origins=[], token_configured=False)


@campaign
@given(st.text(max_size=160))
def test_origin_parser_total_and_canonical_for_generated_strings(value):
    parsed = _origin(value)
    if parsed is not None:
        scheme, host, port = parsed
        authority = f"[{host}]" if ":" in host else host
        assert scheme in {"http", "https"}
        assert 0 <= port <= 65535
        assert _origin(f"{scheme}://{authority}:{port}") == parsed
        assert not any(ord(char) <= 32 or ord(char) >= 127 or char in "\\%" for char in value)


@campaign
@given(st.integers(min_value=1, max_value=65535),
       st.sampled_from([" ", "\t", "\n", "\r", "\\", "%", "\x00", "\x7f"]),
       st.integers(min_value=0, max_value=7))
def test_ambiguous_authority_bytes_never_admit_local_control(port, bad, index):
    host = f"127.0.0.1:{port}"
    damaged = host[:index] + bad + host[index:]
    assert not admits([("host", damaged)])


@campaign
@given(st.integers(min_value=1, max_value=65535),
       st.sampled_from(["host", "origin", "sec-fetch-site"]))
def test_duplicate_browser_authority_headers_never_authorize(port, name):
    host = f"127.0.0.1:{port}"
    values = {"host": host, "origin": f"http://{host}", "sec-fetch-site": "same-origin"}
    headers = list(values.items()) + [(name, values[name])]
    assert not admits(headers)


@campaign
@given(st.integers(min_value=1, max_value=65535),
       st.from_regex(r"[a-z][a-z0-9]{0,20}", fullmatch=True))
def test_foreign_origin_and_rebinding_name_denied_without_dns(port, label):
    hostile = f"{label}.invalid:{port}"
    assert not admits([("host", hostile)])
    assert not admits([("host", f"127.0.0.1:{port}"), ("origin", "https://" + hostile)])


@campaign
@given(st.integers(min_value=1, max_value=65535),
       st.sampled_from(["127.0.0.1", "localhost", "[::1]"]))
def test_valid_dynamic_local_origins_and_native_clients_remain_usable(port, hostname):
    host = f"{hostname}:{port}"
    assert admits([("host", host)])
    assert admits([("host", host), ("origin", f"http://{host}"),
                   ("sec-fetch-site", "same-origin")])

