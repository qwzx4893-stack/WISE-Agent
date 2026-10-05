from qa.acceptance.actionability_probe import install_probe, observe_actionability
import pytest


class Session:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.detached = False

    def send(self, command, params):
        self.calls.append((command, params))
        return self.result

    def detach(self):
        self.detached = True


class Page:
    def __init__(self, session):
        self.session = session
        self.context = self
        self.scripts = []

    def new_cdp_session(self, page):
        assert page is self
        return self.session

    def add_init_script(self, code):
        self.scripts.append(code)

    def evaluate(self, code):
        self.scripts.append(code)


def test_probe_reads_only_with_bounded_runtime_and_no_async_wait():
    session = Session({"result": {"value": {"heartbeat": {"frames": 3}}}})
    result = observe_actionability(Page(session))
    assert result["status"] == "OBSERVED"
    command, params = session.calls[0]
    assert command == "Runtime.evaluate" and params["timeout"] == 1500
    assert params["awaitPromise"] is False and params["returnByValue"] is True
    assert session.detached
    assert "click(" not in params["expression"] and "textContent" not in params["expression"]


def test_probe_cannot_accept_arbitrary_dom_or_prompt_selectors():
    with pytest.raises(ValueError):
        observe_actionability(Page(None), "#secret")


@pytest.mark.parametrize("payload", [{}, {"exceptionDetails": {"text": "timeout"}}, {"result": {"value": "wrong"}}])
def test_probe_errors_are_observations_not_success(payload):
    session = Session(payload)
    result = observe_actionability(Page(session))
    assert result["status"] == "UNAVAILABLE" and session.detached


def test_install_is_passive_bounded_and_resets_on_navigation():
    page = Page(None)
    install_probe(page)
    assert len(page.scripts) == 2
    assert "list.length > 12" in page.scripts[0]
    assert "500" in page.scripts[0]
    assert "document.addEventListener('visibilitychange'" in page.scripts[0]
    assert "click(" not in page.scripts[0] and "style." not in page.scripts[0]
