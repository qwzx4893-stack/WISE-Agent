"""Preview disclosure controls with synthetic files; no private files or OS changes."""
from contextlib import contextmanager
import builtins
from email.message import Message
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from core import preview_server as ps


def handler(root, path):
    instance = object.__new__(ps._SilentHandler)
    instance.directory = str(root)
    instance.preview_root = root.resolve()
    instance.path = path
    instance.headers = Message()
    instance.headers["Host"] = "127.0.0.1:8765"
    instance.server = SimpleNamespace(server_address=("127.0.0.1", 8765))
    instance.responses = []
    instance.sent_headers = []
    instance.send_response = lambda code: instance.responses.append(int(code))
    instance.send_error = lambda code, *args: instance.responses.append(int(code))
    instance.send_header = lambda key, value: instance.sent_headers.append((key, value))
    instance.end_headers = lambda: None
    return instance


def body(instance):
    stream = instance.send_head()
    if stream is None:
        return b""
    try:
        return stream.read()
    finally:
        stream.close()


@contextmanager
def directory_link(path, target):
    """Create/remove only a link under pytest's disposable root."""
    if os.name == "nt":
        result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(path), str(target)],
                                capture_output=True, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            pytest.skip("Disposable junction creation unavailable; not a security PASS")
    else:
        path.symlink_to(target, target_is_directory=True)
    try:
        yield
    finally:
        if os.name == "nt":
            os.rmdir(path)  # Never recurse into a junction target.
        else:
            path.unlink()


def test_external_junction_file_is_not_served(tmp_path):
    root = tmp_path / "public"; root.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "marker.txt").write_text("PRIVATE_SYNTHETIC_MARKER")
    with directory_link(root / "linked", outside):
        request = handler(root, "/linked/marker.txt")
        assert b"PRIVATE_SYNTHETIC_MARKER" not in body(request)
        assert request.responses == [403]


def test_external_junction_index_and_listing_are_not_served(tmp_path):
    root = tmp_path / "public"; root.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "index.html").write_text("PRIVATE_SYNTHETIC_INDEX")
    with directory_link(root / "linked", outside):
        request = handler(root, "/linked/")
        assert b"PRIVATE_SYNTHETIC_INDEX" not in body(request)
        assert request.responses == [403]
        (outside / "index.html").unlink()
        (outside / "PRIVATE_SYNTHETIC_FILENAME").write_text("fixture")
        request = handler(root, "/linked/")
        assert b"PRIVATE_SYNTHETIC_FILENAME" not in body(request)
        assert request.responses == [403]


def test_opened_file_identity_not_only_preopen_resolution(tmp_path, monkeypatch):
    root = tmp_path / "public"; root.mkdir()
    normal = root / "normal.txt"; normal.write_text("PUBLIC")
    private = tmp_path / "private.txt"; private.write_text("PRIVATE_RACE_MARKER")
    real_open = builtins.open
    def switched_open(path, *args, **kwargs):
        return real_open(private if Path(path) == normal else path, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", switched_open)
    request = handler(root, "/normal.txt")
    assert b"PRIVATE_RACE_MARKER" not in body(request)
    assert request.responses == [403]


def test_normal_files_and_index_preserved(tmp_path):
    (tmp_path / "index.html").write_text("PUBLIC_INDEX")
    (tmp_path / "normal.txt").write_text("PUBLIC_FILE")
    for url, expected in (("/", b"PUBLIC_INDEX"), ("/normal.txt", b"PUBLIC_FILE")):
        request = handler(tmp_path, url)
        assert body(request) == expected
        assert request.responses == [200]
        assert ("Content-Length", str(len(expected))) in request.sent_headers


@pytest.mark.parametrize("url", ["/normal.txt:stream", "/NUL", "/COM1", "/normal.txt%00"])
def test_windows_devices_ads_and_nul_not_opened(tmp_path, monkeypatch, url):
    (tmp_path / "normal.txt").write_text("PUBLIC")
    def forbidden_open(*args, **kwargs):
        pytest.fail("Unsafe path reached open")
    monkeypatch.setattr(builtins, "open", forbidden_open)
    request = handler(tmp_path, url)
    assert body(request) == b""
    assert request.responses == [403]


def test_unknown_opened_file_identity_fails_closed(tmp_path, monkeypatch):
    (tmp_path / "normal.txt").write_text("PUBLIC")
    monkeypatch.setattr(ps, "_opened_file_path", lambda file: (_ for _ in ()).throw(OSError("fixture identity failure")))
    request = handler(tmp_path, "/normal.txt")
    assert body(request) == b""
    assert request.responses == [403]


@pytest.mark.parametrize("authority", ["rebinding.example:8765", "127.0.0.1:9999",
                                      "localhost:8765@external.example", "127.0.0.1:8765/path"])
def test_foreign_preview_authority_cannot_read_fixture(tmp_path, authority):
    (tmp_path / "normal.txt").write_text("PRIVATE_PREVIEW_FIXTURE")
    request = handler(tmp_path, "/normal.txt")
    request.headers.replace_header("Host", authority)
    assert body(request) == b""
    assert request.responses == [403]


@pytest.mark.parametrize("header,value", [("Origin", "https://external.example"),
                                          ("Origin", "null"),
                                          ("Sec-Fetch-Site", "cross-site"),
                                          ("Host", "127.0.0.1:8765")])
def test_cross_origin_or_ambiguous_preview_headers_denied(tmp_path, header, value):
    (tmp_path / "normal.txt").write_text("PRIVATE_PREVIEW_FIXTURE")
    request = handler(tmp_path, "/normal.txt")
    request.headers[header] = value
    assert body(request) == b""
    assert request.responses == [403]


def test_native_and_same_origin_preview_clients_preserved(tmp_path):
    (tmp_path / "normal.txt").write_text("PUBLIC")
    for authority in ("127.0.0.1:8765", "localhost:8765"):
        request = handler(tmp_path, "/normal.txt")
        request.headers.replace_header("Host", authority)
        request.headers["Origin"] = "http://" + authority
        request.headers["Sec-Fetch-Site"] = "same-origin"
        assert body(request) == b"PUBLIC"
        assert request.responses == [200]
