"""Plain-Django views used by the shared fixtures.

Nothing in this module imports Django Ninja, so the ordinary URLconf remains
usable in environments where the optional Ninja dependency is not installed.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Iterator

from django.http import (
    FileResponse,
    HttpRequest,
    HttpResponse,
    StreamingHttpResponse,
)


def sync_success(request: HttpRequest) -> HttpResponse:
    return HttpResponse(b"sync-ok", content_type="text/plain")


async def async_success(request: HttpRequest) -> HttpResponse:
    return HttpResponse(b"async-ok", content_type="text/plain")


def sync_error(request: HttpRequest) -> HttpResponse:
    raise RuntimeError("sync view failure")


async def async_error(request: HttpRequest) -> HttpResponse:
    raise RuntimeError("async view failure")


def status_404(request: HttpRequest) -> HttpResponse:
    return HttpResponse(b"plain-404", status=404, content_type="text/plain")


def status_500(request: HttpRequest) -> HttpResponse:
    return HttpResponse(b"plain-500", status=500, content_type="text/plain")


def _sync_stream_chunks(fail: bool) -> Iterator[bytes]:
    yield b"sync-1\n"
    if fail:
        raise RuntimeError("sync stream failure")
    yield b"sync-2\n"


def sync_stream(request: HttpRequest) -> StreamingHttpResponse:
    fail = request.GET.get("fail") == "1"
    return StreamingHttpResponse(_sync_stream_chunks(fail), content_type="text/plain")


async def _async_stream_chunks(fail: bool) -> AsyncIterator[bytes]:
    yield b"async-1\n"
    if fail:
        raise RuntimeError("async stream failure")
    yield b"async-2\n"


def async_stream(request: HttpRequest) -> StreamingHttpResponse:
    fail = request.GET.get("fail") == "1"
    return StreamingHttpResponse(_async_stream_chunks(fail), content_type="text/plain")


def file_response(request: HttpRequest) -> FileResponse:
    return FileResponse(
        io.BytesIO(b"file-ok\n"),
        content_type="text/plain",
        filename="file-ok.txt",
    )
