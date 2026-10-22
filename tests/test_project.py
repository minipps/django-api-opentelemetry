"""Baseline checks for the shared Django fixture project.

These tests describe request/streaming behavior only; instrumentation
assertions belong to the middleware, Ninja and telemetry test modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from django.http import FileResponse, StreamingHttpResponse

if TYPE_CHECKING:
    from django.test import AsyncClient, Client

    from tests.conftest import TelemetryProviders


def test_sync_success(wsgi_client: Client) -> None:
    response = wsgi_client.get("/sync/")

    assert response.status_code == 200
    assert response.content == b"sync-ok"


async def test_async_success(asgi_client: AsyncClient) -> None:
    response = await asgi_client.get("/async/")

    assert response.status_code == 200
    assert response.content == b"async-ok"


def test_unexpected_sync_error_returns_500() -> None:
    from django.test import Client

    client = Client(raise_request_exception=False)
    response = client.get("/error/")

    assert response.status_code == 500


async def test_unexpected_async_error_returns_500() -> None:
    from django.test import AsyncClient

    client = AsyncClient(raise_request_exception=False)
    response = await client.get("/async-error/")

    assert response.status_code == 500


@pytest.mark.parametrize(
    ("path", "status"),
    [("/status/404/", 404), ("/status/500/", 500)],
)
def test_plain_status_responses(wsgi_client: Client, path: str, status: int) -> None:
    response = wsgi_client.get(path)

    assert response.status_code == status


def test_sync_stream_is_lazy_and_streaming(wsgi_client: Client) -> None:
    response = wsgi_client.get("/stream/sync/")

    assert isinstance(response, StreamingHttpResponse)
    assert response.status_code == 200
    assert list(response.streaming_content) == [b"sync-1\n", b"sync-2\n"]

    response.close()


def test_sync_stream_failure_is_observable(wsgi_client: Client) -> None:
    response = wsgi_client.get("/stream/sync/?fail=1")
    chunks = iter(response.streaming_content)

    assert next(chunks) == b"sync-1\n"
    with pytest.raises(RuntimeError, match="sync stream failure"):
        next(chunks)

    response.close()


async def test_async_stream_is_lazy_and_streaming(
    asgi_client: AsyncClient,
) -> None:
    response = await asgi_client.get("/stream/async/")

    assert isinstance(response, StreamingHttpResponse)
    assert response.status_code == 200
    content = response.streaming_content
    assert [chunk async for chunk in content] == [
        b"async-1\n",
        b"async-2\n",
    ]

    await content.aclose()
    response.close()


async def test_async_stream_failure_is_observable(
    asgi_client: AsyncClient,
) -> None:
    response = await asgi_client.get("/stream/async/?fail=1")
    content = response.streaming_content
    chunks = content.__aiter__()

    assert await anext(chunks) == b"async-1\n"
    with pytest.raises(RuntimeError, match="async stream failure"):
        await anext(chunks)

    await content.aclose()
    response.close()


def test_file_response(wsgi_client: Client) -> None:
    response = wsgi_client.get("/file/")

    assert isinstance(response, FileResponse)
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"file-ok\n"

    response.close()


@pytest.fixture
def ninja_urlconf(settings: Any) -> None:
    pytest.importorskip(
        "ninja",
        reason="the optional django-ninja dependency is not installed",
    )
    settings.ROOT_URLCONF = "tests.project.ninja_urls"


def test_ninja_success(wsgi_client: Client, ninja_urlconf: None) -> None:
    response = wsgi_client.get("/ninja/items/7?quantity=3")

    assert response.status_code == 200
    assert response.json() == {
        "item_id": 7,
        "name": "item",
        "quantity": 3,
    }


async def test_ninja_async_success(
    asgi_client: AsyncClient, ninja_urlconf: None
) -> None:
    response = await asgi_client.get("/ninja/async-success")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "async"}


def test_ninja_path_validation(wsgi_client: Client, ninja_urlconf: None) -> None:
    response = wsgi_client.get("/ninja/items/not-an-int")

    assert response.status_code == 422
    assert "detail" in response.json()


def test_ninja_body_validation(wsgi_client: Client, ninja_urlconf: None) -> None:
    response = wsgi_client.post(
        "/ninja/items",
        data={"name": "widget", "quantity": 0},
        content_type="application/json",
    )

    assert response.status_code == 422
    assert "detail" in response.json()


def test_ninja_custom_exception_handler(
    wsgi_client: Client, ninja_urlconf: None
) -> None:
    response = wsgi_client.get("/ninja/custom-error")

    assert response.status_code == 409
    assert response.json() == {"detail": "custom failure"}


def test_ninja_expected_http_error(wsgi_client: Client, ninja_urlconf: None) -> None:
    response = wsgi_client.get("/ninja/http-error")

    assert response.status_code == 404


def test_ninja_unexpected_error_returns_500() -> None:
    pytest.importorskip("ninja")
    from django.test import Client, override_settings

    client = Client(raise_request_exception=False)
    with override_settings(ROOT_URLCONF="tests.project.ninja_urls"):
        response = client.get("/ninja/error")

    assert response.status_code == 500


def test_ninja_nested_router(wsgi_client: Client, ninja_urlconf: None) -> None:
    response = wsgi_client.get("/ninja/router/nested/deep")

    assert response.status_code == 200
    assert response.json() == {"depth": 2}


def test_telemetry_providers_fixture(
    telemetry_providers: TelemetryProviders,
) -> None:
    from opentelemetry._logs import SeverityNumber

    provider = telemetry_providers

    tracer = provider.tracer_provider.get_tracer(__name__)
    with tracer.start_as_current_span("fixture.span"):
        pass

    logger = provider.logger_provider.get_logger(__name__)
    logger.emit(body="fixture.log", severity_number=SeverityNumber.WARN)

    counter = provider.meter_provider.get_meter(__name__).create_counter(
        "fixture.counter"
    )
    counter.add(1)

    assert [span.name for span in provider.spans] == ["fixture.span"]
    assert provider.logs[-1].log_record.body == "fixture.log"

    metrics = provider.metrics()
    assert metrics is not None
    metric_names = {
        metric.name
        for resource_metrics in metrics.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }
    assert "fixture.counter" in metric_names
