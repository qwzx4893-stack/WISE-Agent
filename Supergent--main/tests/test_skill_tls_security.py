"""Actual scraper methods, fake transports; no skill imports/network/browser."""

import ast
import logging
import os
from pathlib import Path
import re
import sys
from types import MethodType, ModuleType, SimpleNamespace

import pytest


SCRAPERS = Path(__file__).resolve().parents[1] / "skills/general/junta-leiloeiros/1.0.0/scripts/scraper"


def methods(filename, names, namespace):
    tree = ast.parse((SCRAPERS / filename).read_text(encoding="utf-8-sig"))
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in names]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            selected.extend(method for method in node.body
                            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and method.name in names)
    assert {node.name for node in selected} == set(names)
    source = ast.Module(body=[ast.ImportFrom(module="__future__",
                             names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(source), str(SCRAPERS / filename), "exec"), namespace)
    return namespace


def run(coroutine):
    """All fixture awaits complete inline; no event-loop socketpair needed."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("Fixture unexpectedly requested real asynchronous IO")


class TLSFailure(Exception):
    def __init__(self, message, **kwargs):
        super().__init__(message)


@pytest.fixture
def transport(monkeypatch):
    clients, launches, contexts, calls = [], [], [], []
    state = SimpleNamespace(fail=False, content_reads=0, browser_closes=0)

    class Response:
        status_code = 200
        text = '<input name="__RequestVerificationToken" value="fixture-csrf"><p>fixture</p>'
        content = text.encode()

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"content": "fixture API HTML"}}

    class Client:
        def __init__(self, **kwargs):
            clients.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            calls.append(("get", url))
            if state.fail:
                raise TLSFailure("fixture certificate rejected")
            return Response()

        async def post(self, url, **kwargs):
            calls.append(("post", url))
            if state.fail:
                raise TLSFailure("fixture certificate rejected")
            return Response()

    httpx = ModuleType("httpx")
    httpx.AsyncClient = Client
    httpx.RequestError = httpx.TimeoutException = TLSFailure
    httpx.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "httpx", httpx)
    bs4 = ModuleType("bs4")
    bs4.BeautifulSoup = lambda text, parser: text
    monkeypatch.setitem(sys.modules, "bs4", bs4)

    class Page:
        async def goto(self, url, **kwargs):
            calls.append(("goto", url))
            if state.fail:
                raise TLSFailure("fixture browser certificate rejected")

        async def content(self):
            state.content_reads += 1
            return "fixture browser HTML"

        async def fill(self, *args, **kwargs):
            pass

        async def click(self, *args, **kwargs):
            pass

        async def wait_for_load_state(self, *args, **kwargs):
            pass

        async def wait_for_timeout(self, *args):
            pass

    class Browser:
        async def new_context(self, **kwargs):
            contexts.append(kwargs)
            return self

        async def new_page(self):
            return Page()

        async def close(self):
            state.browser_closes += 1

    class Playwright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=self)

        async def __aexit__(self, *args):
            pass

        async def launch(self, **kwargs):
            launches.append(kwargs)
            return Browser()

    playwright = ModuleType("playwright")
    playwright_api = ModuleType("playwright.async_api")
    playwright_api.async_playwright = Playwright
    playwright.async_api = playwright_api
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.async_api", playwright_api)

    async def no_sleep(*args):
        pass

    ns = {"__name__": "synthetic_skill_tls", "httpx": httpx, "BeautifulSoup": bs4.BeautifulSoup,
          "re": re, "os": os, "logger": logging.getLogger("synthetic_skill_tls"),
          "asyncio": SimpleNamespace(sleep=no_sleep)}
    methods("base_scraper.py", ["should_verify_tls"], ns)
    # New shared response hooks are optional in the pre-fix proof.
    if "tls_response_hooks" in (SCRAPERS / "base_scraper.py").read_text(encoding="utf-8-sig"):
        methods("base_scraper.py", ["tls_response_hooks", "reject_tls_downgrade"], ns)
    self = SimpleNamespace(HEADERS={"User-Agent": "fixture"}, estado="fixture", max_retries=1,
                           timeout=1, url="https://example.invalid/list", _FORM_URL="https://example.invalid/form",
                           _POST_URL="https://example.invalid/post", _AGE_SITUACAO="fixture",
                           _API_URL="https://example.invalid/api",
                           _parse_plain_html=lambda text: [{"nome": "fixture"}],
                           _parse_cms_content=lambda soup: [{"nome": "fixture"}])
    return ns, self, state, clients, launches, contexts, calls


HTTP_METHODS = [("jucema.py", "fetch_insecure"), ("jucema.py", "_fetch_api"),
                ("jucesp.py", "_fetch_with_session"),
                ("jucisrs.py", "_fetch_post"), ("jucisrs.py", "_fetch_get_all")]


@pytest.mark.parametrize("filename,name", HTTP_METHODS)
@pytest.mark.parametrize("certificate_failure", [False, True])
def test_state_http_methods_always_verify_certificates(transport, filename, name, certificate_failure):
    ns, self, state, clients, _, _, _ = transport
    state.fail = certificate_failure
    fn = methods(filename, [name], ns)[name]
    args = (self, self.url) if name == "fetch_insecure" else (self,)
    if certificate_failure and name == "_fetch_with_session":
        with pytest.raises(TLSFailure):
            run(fn(*args))
    else:
        result = run(fn(*args))
        assert bool(result) is not certificate_failure
    assert clients and all(options.get("verify", True) is True for options in clients)
    assert all(options.get("follow_redirects") is True for options in clients)


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_shared_helper_cannot_disable_tls_from_environment(transport, monkeypatch, value):
    ns, *_ = transport
    monkeypatch.setenv("JUNTA_INSECURE_TLS", value)
    assert ns["should_verify_tls"]() is True


@pytest.mark.parametrize("filename,name", [("base_scraper.py", "fetch_page_js"),
                                          ("jucisrs.py", "_playwright_ssl_bypass")])
@pytest.mark.parametrize("certificate_failure", [False, True])
def test_browser_methods_keep_security_and_reject_certificate_failure(transport, monkeypatch,
                                                                     filename, name, certificate_failure):
    ns, self, state, _, launches, contexts, _ = transport
    monkeypatch.setenv("JUNTA_INSECURE_TLS", "true")
    state.fail = certificate_failure
    result = run(methods(filename, [name], ns)[name](self, self.url))
    assert bool(result) is not certificate_failure
    assert contexts and all(options.get("ignore_https_errors", False) is False for options in contexts)
    forbidden = {"--ignore-certificate-errors", "--ignore-ssl-errors", "--disable-web-security",
                 "--allow-insecure-localhost"}
    assert all(not forbidden.intersection(options.get("args", [])) for options in launches)
    assert state.content_reads == (0 if certificate_failure else 1)
    assert state.browser_closes == 1


def test_shared_http_failure_has_no_insecure_retry(transport, monkeypatch):
    ns, self, state, clients, *_ = transport
    state.fail = True
    self.max_retries = 2
    monkeypatch.setenv("JUNTA_INSECURE_TLS", "true")
    result = run(methods("base_scraper.py", ["fetch_page"], ns)["fetch_page"](self))
    assert result is None and len(clients) == 2
    assert all(options["verify"] is True for options in clients)


@pytest.mark.parametrize("filename,name", [*HTTP_METHODS, ("base_scraper.py", "fetch_page")])
@pytest.mark.parametrize("location", ["/next", "https://example.invalid/next", "http://example.invalid/next"])
def test_actual_http_client_hooks_preserve_https_redirects_and_reject_downgrades(transport,
                                                                              filename, name, location):
    ns, self, _, clients, *_ = transport
    fn = methods(filename, [name], ns)[name]
    args = (self, self.url) if name == "fetch_insecure" else (self,)
    run(fn(*args))
    response = SimpleNamespace(request=SimpleNamespace(url=self.url),
                               headers={"location": location}, is_redirect=True)
    for options in clients:
        hooks = options["event_hooks"]["response"]
        assert hooks == [ns["reject_tls_downgrade"]]
        if location.startswith("http:"):
            with pytest.raises(TLSFailure, match="HTTPS redirect to HTTP refused"):
                run(hooks[0](response))
        else:
            assert run(hooks[0](response)) is None


@pytest.mark.parametrize("state_code", ["MA", "RS"])
def test_state_fallback_chains_never_disable_tls_after_certificate_failure(transport, state_code):
    ns, self, state, clients, launches, contexts, calls = transport
    state.fail = True
    self.url_fallback = "https://example.invalid/fallback"
    self.make_leiloeiro = lambda **kwargs: kwargs

    async def no_records():
        return []

    if state_code == "MA":
        self._fetch_api = no_records
        self._parse_cms_content = lambda soup: []
        selected = [("base_scraper.py", ["fetch_page_js"]),
                    ("jucema.py", ["fetch_insecure", "parse_leiloeiros"])]
    else:
        selected = [("base_scraper.py", ["fetch_page"]),
                    ("jucisrs.py", ["_fetch_post", "_fetch_get_all", "_playwright_ssl_bypass", "parse_leiloeiros"])]
    for filename, names in selected:
        methods(filename, names, ns)
        for name in names:
            setattr(self, name, MethodType(ns[name], self))
    assert run(self.parse_leiloeiros()) == []
    assert clients and launches and contexts
    assert all(options["verify"] is True for options in clients)
    assert all(options.get("ignore_https_errors", False) is False for options in contexts)
    assert all(not options.get("args") for options in launches)
    assert all(url.startswith("https:") for _, url in calls)
    assert state.content_reads == 0
