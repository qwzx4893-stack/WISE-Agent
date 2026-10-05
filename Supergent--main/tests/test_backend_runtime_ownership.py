"""Different TCP ports must not create two writers for one task runtime."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
import sys
import pytest


def test_lifespan_refuses_another_process_owner_and_releases_after_exit(tmp_path, monkeypatch):
    from api import server
    from core import paths
    monkeypatch.setattr(paths, "RUNTIME_ROOT", tmp_path)
    entered = []

    @asynccontextmanager
    async def fake_runtime(app):
        entered.append("entered")
        yield
        entered.append("drained")

    monkeypatch.setattr(server, "_runtime_lifespan", fake_runtime)
    script = (
        "import sys; from pathlib import Path; from core.durable_io import storage_writer_lock; "
        "lock=storage_writer_lock(Path(sys.argv[1]),lock_name='.backend-owner.lock',timeout=0); "
        "lock.__enter__(); print('ACQUIRED',flush=True); sys.stdin.readline()"
    )

    async def attempt():
        async with server.lifespan(server.app):
            pass

    owner = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert owner.stdout.readline().strip() == "ACQUIRED"
        with pytest.raises(TimeoutError):
            asyncio.run(attempt())
        assert entered == []
    finally:
        owner.communicate("release\n", timeout=10)
    asyncio.run(attempt())
    assert entered == ["entered", "drained"]


def test_lifespan_lock_releases_after_exception(tmp_path, monkeypatch):
    from api import server
    from core import paths
    monkeypatch.setattr(paths, "RUNTIME_ROOT", tmp_path)

    @asynccontextmanager
    async def fake_runtime(app):
        yield

    monkeypatch.setattr(server, "_runtime_lifespan", fake_runtime)

    async def attempt(fail=False):
        async with server.lifespan(server.app):
            if fail:
                raise ValueError("synthetic failure")

    with pytest.raises(ValueError):
        asyncio.run(attempt(True))
    asyncio.run(attempt())
