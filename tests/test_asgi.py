"""Optional ASGI lifespan wrapper and real-Uvicorn lifecycle checks."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from django_api_opentelemetry import _runtime, instrument_asgi, shutdown


async def _wait_for_send(sent: list[dict[str, Any]]) -> None:
    for _ in range(200):
        if sent:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("lifespan startup was not acknowledged")


async def test_non_lifespan_scope_is_forwarded_unchanged() -> None:
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    calls: list[tuple[Any, Any, Any]] = []
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def application(scope: Any, receive: Any, send: Any) -> None:
        calls.append((scope, receive, send))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    await instrument_asgi(application)(scope, receive, send)

    assert calls == [(scope, receive, send)]
    assert sent == [
        {"type": "http.response.start", "status": 200, "headers": []},
        {"type": "http.response.body", "body": b"ok"},
    ]


async def test_lifespan_acknowledges_startup_and_shuts_down_once(
    monkeypatch: Any,
) -> None:
    events: asyncio.Queue[dict[str, str]] = asyncio.Queue()
    sent: list[dict[str, Any]] = []
    forwarded: list[Any] = []
    shutdown_threads: list[int] = []
    event_loop_thread = threading.get_ident()

    async def application(scope: Any, receive: Any, send: Any) -> None:
        forwarded.append(scope)

    def fake_shutdown() -> None:
        shutdown_threads.append(threading.get_ident())

    monkeypatch.setattr(_runtime, "_shutdown", fake_shutdown)

    async def receive() -> dict[str, str]:
        return await events.get()

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    task = asyncio.create_task(
        instrument_asgi(application)(
            {"type": "lifespan"},
            receive,
            send,
        )
    )
    await events.put({"type": "lifespan.startup"})
    await _wait_for_send(sent)

    assert sent == [{"type": "lifespan.startup.complete"}]
    assert shutdown_threads == []
    assert forwarded == []

    await events.put({"type": "lifespan.shutdown"})
    await asyncio.wait_for(task, timeout=2)

    assert sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]
    assert len(shutdown_threads) == 1
    assert shutdown_threads[0] != event_loop_thread
    assert forwarded == []


def test_public_shutdown_is_runtime_shutdown() -> None:
    assert shutdown is _runtime.shutdown


def test_package_import_and_lifespan_startup_without_optional_dependencies() -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_STARTUP_PROBE)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "startup-ok"


def test_real_uvicorn_sends_lifespan_shutdown(tmp_path: Path) -> None:
    marker = tmp_path / "shutdown.txt"
    (tmp_path / "uvicorn_app.py").write_text(
        textwrap.dedent(
            """
            import os
            from pathlib import Path

            from django_api_opentelemetry import _runtime, instrument_asgi


            def fake_shutdown() -> None:
                Path(os.environ["SHUTDOWN_MARKER"]).write_text("closed")


            _runtime._shutdown = fake_shutdown


            async def app(scope, receive, send):
                await send(
                    {"type": "http.response.start", "status": 200, "headers": []}
                )
                await send({"type": "http.response.body", "body": b"ok"})


            application = instrument_asgi(app)
            """
        ),
        encoding="utf-8",
    )

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    env = {
        **os.environ,
        "PYTHONPATH": str(tmp_path),
        "SHUTDOWN_MARKER": str(marker),
        "PYTHONUNBUFFERED": "1",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "uvicorn_app:application",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = ""
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"uvicorn exited early: {process.stdout.read()}")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=0.5
                ) as response:
                    assert response.status == 200
                    assert response.read() == b"ok"
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("uvicorn did not accept requests")

        process.terminate()
        output, _ = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            output, _ = process.communicate(timeout=5)

    assert marker.read_text(encoding="utf-8") == "closed", output


_STARTUP_PROBE = """
import asyncio
import sys
import threading


class BlockOptionalImports:
    def find_spec(self, fullname, path=None, target=None):
        blocked = (
            fullname == "ninja"
            or fullname.startswith("ninja.")
            or fullname == "opentelemetry.sdk"
            or fullname.startswith("opentelemetry.sdk.")
            or fullname == "opentelemetry.exporter"
            or fullname.startswith("opentelemetry.exporter.")
        )
        if blocked:
            raise ModuleNotFoundError(fullname)
        return None


sys.meta_path.insert(0, BlockOptionalImports())

import django_api_opentelemetry as package
from django.conf import settings

assert not settings.configured
assert package.__version__ == "0.1.0"
assert package.shutdown is package._runtime.shutdown

from django_api_opentelemetry import _runtime

active_before = threading.active_count()
assert _runtime._state.cleanup is None


async def application(scope, receive, send):
    raise AssertionError("lifespan must not reach the native Django application")


async def main():
    events = asyncio.Queue()
    sent = []
    startup_sent = asyncio.Event()

    async def receive():
        return await events.get()

    async def send(message):
        sent.append(message)
        startup_sent.set()

    task = asyncio.create_task(
        package.instrument_asgi(application)(
            {"type": "lifespan"},
            receive,
            send,
        )
    )
    await events.put({"type": "lifespan.startup"})
    await asyncio.wait_for(startup_sent.wait(), timeout=2)

    assert sent == [{"type": "lifespan.startup.complete"}]
    assert _runtime._state.cleanup is None
    assert threading.active_count() == active_before

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


asyncio.run(main())

assert threading.active_count() == active_before
assert _runtime._state.cleanup is None
forbidden = ("ninja", "opentelemetry.sdk", "opentelemetry.exporter")
loaded = [
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
]
assert not loaded, loaded
print("startup-ok")
"""
