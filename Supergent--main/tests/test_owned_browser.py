from contextlib import contextmanager
import os
from pathlib import Path

import pytest
from qa.acceptance import owned_browser as module


@pytest.fixture
def output(tmp_path, monkeypatch):
    root = tmp_path / "project"
    folder = root / "qa-results" / "owned-case"
    folder.mkdir(parents=True)
    monkeypatch.setattr(module, "ROOT", root)
    return folder


def test_environment_filters_credentials_and_uses_only_owned_temp(output):
    env = module.browser_environment(output, {"PATH": "safe", "OpenRouter_API_KEY": "secret",
        "GH_TOKEN": "secret", "NANGO_SECRET_KEY": "secret", "COOKIE_JAR": "private",
        "AUTH_CREDENTIALS": "private", "OPENAI_KEY": "secret",
        "AWS_ACCESS_KEY_ID": "credential", "TEMP": "unowned"})
    assert set(env) == {"PATH", "TEMP", "TMP", "TMPDIR"}
    assert all(Path(env[name]) == output / "browser-temp" for name in module.TEMP_NAMES)
    assert env["PATH"] == "safe"


@pytest.mark.parametrize("failure", [False, True])
def test_context_filters_driver_environment_and_restores_on_body_error(output, monkeypatch, failure):
    monkeypatch.setenv("WISE_QA_API_KEY", "not-for-node")
    monkeypatch.delenv("TMPDIR", raising=False)
    previous = dict(os.environ)

    @contextmanager
    def fake():
        assert "WISE_QA_API_KEY" not in os.environ
        assert os.environ["TEMP"] == str(output / "browser-temp")
        yield "owned-real-api-object"

    monkeypatch.setattr(module, "_playwright_context", fake)
    if failure:
        with pytest.raises(RuntimeError):
            with module.owned_playwright(output):
                raise RuntimeError("body failed")
    else:
        with module.owned_playwright(output) as value:
            assert value == "owned-real-api-object"
    assert dict(os.environ) == previous


def test_context_restores_if_driver_startup_fails(output, monkeypatch):
    previous = dict(os.environ)

    @contextmanager
    def fail():
        raise OSError("driver launch denied")
        yield  # pragma: no cover

    monkeypatch.setattr(module, "_playwright_context", fail)
    with pytest.raises(OSError):
        with module.owned_playwright(output):
            pass
    assert dict(os.environ) == previous


def test_context_restores_if_driver_cleanup_fails(output, monkeypatch):
    previous = dict(os.environ)

    @contextmanager
    def fail_cleanup():
        yield None
        raise OSError("driver cleanup failed")

    monkeypatch.setattr(module, "_playwright_context", fail_cleanup)
    with pytest.raises(OSError):
        with module.owned_playwright(output):
            pass
    assert dict(os.environ) == previous


def test_invalid_directory_does_not_mutate_environment(output):
    previous = dict(os.environ)
    with pytest.raises(ValueError):
        with module.owned_playwright(output.parent):
            pass
    assert dict(os.environ) == previous


@pytest.mark.parametrize("kind", ["root", "outside", "missing", "file", "traversal"])
def test_output_must_be_specific_owned_existing_qa_directory(output, kind):
    choices = {"root": output.parent, "outside": output.parent.parent,
        "missing": output.parent / "absent", "file": output / "regular.txt",
        "traversal": output / ".." / output.name}
    choices["file"].write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError):
        module.owned_temp(choices[kind])


def test_temp_symlink_or_junction_is_rejected_without_following(output, monkeypatch):
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: True if self.name == "browser-temp" else original(self))
    with pytest.raises(ValueError):
        module.owned_temp(output)
    assert not (output / "browser-temp").exists()


def test_output_ancestor_link_is_rejected(output, monkeypatch):
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: True if self == output else original(self))
    with pytest.raises(ValueError):
        module.owned_temp(output)
