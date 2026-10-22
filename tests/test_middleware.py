"""Focused Django middleware and streaming lifecycle tests."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Any

import pytest
from asgiref.sync import iscoroutinefunction
from django.core.handlers.asgi import ASGIHandler
from django.core.handlers.wsgi import WSGIHandler
from django.core.signals import got_request_exception
from django.http import (
    FileResponse,
    HttpRequest,
    HttpResponse,
    StreamingHttpResponse,
)
from django.test import (
    AsyncClient,
    AsyncRequestFactory,
    Client,
    RequestFactory,
    override_settings,
)
from django.urls import include, path
from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, TraceFlags

from django_ninja_opentelemetry import middleware as middleware_module
from django_ninja_opentelemetry._config import Config
from django_ninja_opentelemetry._telemetry import REQUEST_STATE_ATTRIBUTE
from django_ninja_opentelemetry.middleware import (
    TelemetryMiddleware,
    observe_django_exception,
)

if TYPE_CHECKING:
    from tests.conftest import TelemetryProviders


MIDDLEWARE_PATH = "django_ninja_opentelemetry.middleware.TelemetryMiddleware"
DURATION = "http.server.request.duration"
ACTIVE = "http.server.active_requests"

EVENTS: list[tuple[str, int | None]] = []
REQUESTS: list[HttpRequest] = []
CLOSABLE_SYNC: list[ClosableSyncIterator] = []
CLOSABLE_ASYNC: list[ClosableAsyncIterator] = []
SYNC_STREAM_ERROR = RuntimeError("observed sync stream failure")
ASYNC_STREAM_ERROR = RuntimeError("observed async stream failure")


class ApplicationCancelled(BaseException):
    """A non-Exception error that Django must not convert into a response."""


APPLICATION_CANCELLED = ApplicationCancelled("application cancelled")


class ClosableSyncIterator:
    def __init__(self) -> None:
        self.close_calls = 0
        self._done = False

    def __iter__(self) -> ClosableSyncIterator:
        return self

    def __next__(self) -> bytes:
        if self._done:
            raise StopIteration
        self._done = True
        return b"closable-sync\n"

    def close(self) -> None:
        self.close_calls += 1


class ClosableAsyncIterator:
    def __init__(self) -> None:
        self.aclose_calls = 0
        self._done = False

    def __aiter__(self) -> ClosableAsyncIterator:
        return self

    async def __anext__(self) -> bytes:
        if self._done:
            raise StopAsyncIteration
        self._done = True
        return b"closable-async\n"

    async def aclose(self) -> None:
        self.aclose_calls += 1


class ShortCircuitMiddleware:
    """Return a response from process_view without calling the view."""

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_view(
        self,
        request: HttpRequest,
        view_func: Any,
        view_args: Any,
        view_kwargs: Any,
    ) -> HttpResponse:
        return HttpResponse(b"short-circuited", status=418)


def _span_id() -> int | None:
    context = trace.get_current_span().get_span_context()
    return context.span_id if context.is_valid else None


def _sync_chunks() -> Iterator[bytes]:
    try:
        EVENTS.append(("sync-start", _span_id()))
        yield b"observed-sync-1\n"
        EVENTS.append(("sync-resume", _span_id()))
        yield b"observed-sync-2\n"
    finally:
        EVENTS.append(("sync-closed", _span_id()))


def _sync_failing_chunks() -> Iterator[bytes]:
    try:
        EVENTS.append(("sync-error-start", _span_id()))
        yield b"observed-sync-error-1\n"
        raise SYNC_STREAM_ERROR
    finally:
        EVENTS.append(("sync-error-closed", _span_id()))


async def _async_chunks() -> AsyncIterator[bytes]:
    try:
        await asyncio.sleep(0)
        EVENTS.append(("async-start", _span_id()))
        yield b"observed-async-1\n"
        await asyncio.sleep(0)
        EVENTS.append(("async-resume", _span_id()))
        yield b"observed-async-2\n"
    finally:
        EVENTS.append(("async-closed", _span_id()))


async def _async_failing_chunks() -> AsyncIterator[bytes]:
    try:
        await asyncio.sleep(0)
        EVENTS.append(("async-error-start", _span_id()))
        yield b"observed-async-error-1\n"
        raise ASYNC_STREAM_ERROR
    finally:
        EVENTS.append(("async-error-closed", _span_id()))


def observed_sync_stream(request: HttpRequest) -> StreamingHttpResponse:
    REQUESTS.append(request)
    return StreamingHttpResponse(_sync_chunks(), content_type="text/plain")


async def observed_async_stream(request: HttpRequest) -> StreamingHttpResponse:
    REQUESTS.append(request)
    return StreamingHttpResponse(_async_chunks(), content_type="text/plain")


def observed_sync_error_stream(request: HttpRequest) -> StreamingHttpResponse:
    REQUESTS.append(request)
    return StreamingHttpResponse(_sync_failing_chunks(), content_type="text/plain")


async def observed_async_error_stream(request: HttpRequest) -> StreamingHttpResponse:
    REQUESTS.append(request)
    return StreamingHttpResponse(_async_failing_chunks(), content_type="text/plain")


def observed_route_error(request: HttpRequest) -> HttpResponse:
    REQUESTS.append(request)
    state = getattr(request, REQUEST_STATE_ATTRIBUTE, None)
    EVENTS.append(("view", None if state is None else state.route))
    raise RuntimeError("observed converted failure")


def observed_target(request: HttpRequest) -> HttpResponse:
    EVENTS.append(("target", None))
    return HttpResponse(b"target", content_type="text/plain")


def observed_closable_sync(request: HttpRequest) -> StreamingHttpResponse:
    REQUESTS.append(request)
    iterator = ClosableSyncIterator()
    CLOSABLE_SYNC.append(iterator)
    return StreamingHttpResponse(iterator, content_type="text/plain")


async def observed_closable_async(request: HttpRequest) -> StreamingHttpResponse:
    REQUESTS.append(request)
    iterator = ClosableAsyncIterator()
    CLOSABLE_ASYNC.append(iterator)
    return StreamingHttpResponse(iterator, content_type="text/plain")


def observed_base_exception(request: HttpRequest) -> HttpResponse:
    REQUESTS.append(request)
    raise APPLICATION_CANCELLED


urlpatterns = [
    path("", include("tests.project.urls")),
    path("observe/sync/", observed_sync_stream, name="observe-sync"),
    path("observe/async/", observed_async_stream, name="observe-async"),
    path(
        "observe/sync-error/",
        observed_sync_error_stream,
        name="observe-sync-error",
    ),
    path(
        "observe/async-error/",
        observed_async_error_stream,
        name="observe-async-error",
    ),
    path("observed-error/", observed_route_error, name="observed-error"),
    path("observed-target/", observed_target, name="observed-target"),
    path("observed-base/", observed_base_exception, name="observed-base"),
    path(
        "observe/closable-sync/",
        observed_closable_sync,
        name="observe-closable-sync",
    ),
    path(
        "observe/closable-async/",
        observed_closable_async,
        name="observe-closable-async",
    ),
]


@pytest.fixture(autouse=True)
def clear_observations() -> Iterator[None]:
    EVENTS.clear()
    REQUESTS.clear()
    CLOSABLE_SYNC.clear()
    CLOSABLE_ASYNC.clear()
    yield
    EVENTS.clear()
    REQUESTS.clear()
    CLOSABLE_SYNC.clear()
    CLOSABLE_ASYNC.clear()


@pytest.fixture
def app(
    settings: Any,
    telemetry_providers: TelemetryProviders,
) -> Any:
    """Configure the real middleware against explicit SDK providers."""

    def configure(**overrides: Any) -> TelemetryProviders:
        middleware = overrides.pop("middleware", [MIDDLEWARE_PATH])
        values: dict[str, Any] = {
            "tracer_provider": telemetry_providers.tracer_provider,
            "meter_provider": telemetry_providers.meter_provider,
            "logger_provider": telemetry_providers.logger_provider,
            "auto_configure": False,
        }
        values.update(overrides)
        settings.DJANGO_NINJA_OTEL = values
        settings.MIDDLEWARE = middleware
        settings.ROOT_URLCONF = __name__
        return telemetry_providers

    return configure


@pytest.fixture
def exception_signal() -> Iterator[None]:
    got_request_exception.connect(
        observe_django_exception,
        weak=False,
        dispatch_uid="tests.test_middleware.exception",
    )
    try:
        yield
    finally:
        got_request_exception.disconnect(dispatch_uid="tests.test_middleware.exception")


def _server_spans(providers: TelemetryProviders) -> list[Any]:
    return [span for span in providers.spans if span.kind is SpanKind.SERVER]


def _metric_data(providers: TelemetryProviders) -> dict[str, Any]:
    data = providers.metrics()
    result: dict[str, Any] = {}
    if data is None:
        return result
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                result[metric.name] = metric
    return result


def _log_records(providers: TelemetryProviders) -> list[Any]:
    return [record.log_record for record in providers.logs]


def _state(request: HttpRequest) -> Any:
    return getattr(request, REQUEST_STATE_ATTRIBUTE)


def test_middleware_uses_native_sync_and_async_modes() -> None:
    def sync_response(request: HttpRequest) -> HttpResponse:
        return HttpResponse()

    async def async_response(request: HttpRequest) -> HttpResponse:
        return HttpResponse()

    sync_middleware = TelemetryMiddleware(sync_response)
    async_middleware = TelemetryMiddleware(async_response)

    assert iscoroutinefunction(sync_middleware) is False
    assert iscoroutinefunction(sync_middleware.process_view) is False
    assert iscoroutinefunction(async_middleware) is True
    assert iscoroutinefunction(async_middleware.process_view) is True
    assert sync_middleware.sync_capable is True
    assert sync_middleware.async_capable is True


def test_sync_success_finishes_with_route_template(
    app: Any,
) -> None:
    providers = app()
    response = Client().get("/sync/")

    assert response.status_code == 200
    assert response.content == b"sync-ok"
    state = _state(response.wsgi_request)
    assert state is not None and state.finished is True
    assert state.route == "sync/"

    [server] = _server_spans(providers)
    assert server.name == "GET sync/"
    assert server.attributes["http.route"] == "sync/"
    assert server.attributes["http.response.status_code"] == 200
    assert _log_records(providers) == []

    metrics = _metric_data(providers)
    assert metrics[ACTIVE].data.data_points[0].value == 0
    assert sum(point.count for point in metrics[DURATION].data.data_points) == 1


async def test_async_success_finishes_with_route_template(
    app: Any,
) -> None:
    providers = app()
    response = await AsyncClient().get("/async/")

    assert response.status_code == 200
    assert response.content == b"async-ok"
    state = _state(response.asgi_request)
    assert state is not None and state.finished is True
    assert state.route == "async/"

    [server] = _server_spans(providers)
    assert server.name == "GET async/"
    assert server.attributes["http.route"] == "async/"
    assert _log_records(providers) == []


def test_unknown_route_uses_method_only_and_no_raw_path(
    app: Any,
) -> None:
    providers = app()
    response = Client().get("/missing/secret-123/")

    assert response.status_code == 404
    state = _state(response.wsgi_request)
    assert state is not None and state.finished is True
    assert state.route is None

    [server] = _server_spans(providers)
    assert server.name == "GET"
    assert "http.route" not in server.attributes
    assert "secret-123" not in str(server.attributes)
    assert server.attributes["http.response.status_code"] == 404

    duration = _metric_data(providers)[DURATION].data.data_points
    assert len(duration) == 1
    assert "http.route" not in duration[0].attributes
    assert "secret-123" not in str(duration[0].attributes)


def test_plain_5xx_marks_span_error_without_exception_log(
    app: Any,
    exception_signal: None,
) -> None:
    providers = app()
    response = Client().get("/status/500/")

    assert response.status_code == 500
    [server] = _server_spans(providers)
    assert server.attributes["http.response.status_code"] == 500
    assert server.status.status_code is trace.StatusCode.ERROR
    assert _log_records(providers) == []


def test_propagation_uses_remote_parent(app: Any) -> None:
    providers = app()
    remote = SpanContext(
        trace_id=0x1234567890ABCDEF1234567890ABCDEF,
        span_id=0x1234567890ABCDEF,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    carrier: dict[str, str] = {}
    inject(carrier, context=trace.set_span_in_context(NonRecordingSpan(remote)))

    response = Client().get(
        "/sync/",
        headers={"traceparent": carrier["traceparent"]},
    )

    assert response.status_code == 200
    [server] = _server_spans(providers)
    assert server.context.trace_id == remote.trace_id
    assert server.parent is not None
    assert server.parent.span_id == remote.span_id


def test_nested_local_parent_takes_precedence(app: Any) -> None:
    providers = app()
    tracer = providers.tracer_provider.get_tracer(__name__)

    with tracer.start_as_current_span("local-parent") as parent:
        response = Client().get("/sync/")

    assert response.status_code == 200
    [server] = _server_spans(providers)
    assert server.parent is not None
    assert server.parent.span_id == parent.get_span_context().span_id
    assert server.context.trace_id == parent.get_span_context().trace_id


def test_converted_error_signal_records_once_with_route(
    app: Any,
    exception_signal: None,
) -> None:
    providers = app()
    response = Client(raise_request_exception=False).get("/observed-error/")

    assert response.status_code == 500
    state = _state(response.wsgi_request)
    assert state is not None and state.finished is True
    assert state.route == "observed-error/"
    assert EVENTS == [("view", "observed-error/")]

    [server] = _server_spans(providers)
    assert server.status.status_code is trace.StatusCode.ERROR
    assert server.attributes["error.type"] == "RuntimeError"

    records = _log_records(providers)
    assert len(records) == 1
    assert records[0].event_name == "http.server.request.exception"
    assert records[0].attributes["http.route"] == "observed-error/"


async def test_converted_async_error_records_once(
    app: Any,
    exception_signal: None,
) -> None:
    providers = app()
    response = await AsyncClient(raise_request_exception=False).get("/async-error/")

    assert response.status_code == 500
    state = _state(response.asgi_request)
    assert state is not None and state.finished is True
    assert state.route == "async-error/"

    [server] = _server_spans(providers)
    assert server.status.status_code is trace.StatusCode.ERROR
    assert server.attributes["error.type"] == "RuntimeError"
    assert len(_log_records(providers)) == 1


def test_process_view_short_circuit_preserves_response(
    app: Any,
) -> None:
    providers = app(
        middleware=[
            MIDDLEWARE_PATH,
            "tests.test_middleware.ShortCircuitMiddleware",
        ]
    )
    response = Client().get("/observed-target/")

    assert response.status_code == 418
    assert response.content == b"short-circuited"
    assert EVENTS == []
    state = _state(response.wsgi_request)
    assert state is not None and state.finished is True
    assert state.route == "observed-target/"

    [server] = _server_spans(providers)
    assert server.name == "GET observed-target/"
    assert server.attributes["http.response.status_code"] == 418


def test_excluded_request_has_no_telemetry(app: Any) -> None:
    providers = app(exclude=lambda request: request.path_info == "/sync/")
    response = Client().get("/sync/")

    assert response.status_code == 200
    assert response.content == b"sync-ok"
    assert _state(response.wsgi_request) is None
    assert _server_spans(providers) == []
    assert _log_records(providers) == []
    assert _metric_data(providers) == {}


@pytest.mark.parametrize("detected", [True, False])
def test_external_http_ownership_disables_owned_http_only(
    app: Any,
    exception_signal: None,
    monkeypatch: pytest.MonkeyPatch,
    detected: bool,
) -> None:
    if detected:
        monkeypatch.setattr(
            middleware_module,
            "_external_http_instrumentation",
            lambda: True,
        )
        providers = app()
    else:
        providers = app(http_instrumentation=False)

    response = Client(raise_request_exception=False).get("/error/")

    assert response.status_code == 500
    state = _state(response.wsgi_request)
    assert state is not None
    assert state.config.http_instrumentation is False
    assert _server_spans(providers) == []
    assert DURATION not in _metric_data(providers)
    assert ACTIVE not in _metric_data(providers)
    assert len(_log_records(providers)) == 1


def test_standard_external_middleware_path_is_detected() -> None:
    path = (
        "opentelemetry.instrumentation.django.middleware."
        "otel_middleware._DjangoMiddleware"
    )
    with override_settings(MIDDLEWARE=[path], DJANGO_NINJA_OTEL={}):
        instance = TelemetryMiddleware(lambda request: HttpResponse())

    assert instance.config.http_instrumentation is False


def test_actual_contrib_middleware_supplies_error_context(
    app: Any,
    exception_signal: None,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.instrumentation.django")
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.django.middleware.otel_middleware import (
        _DjangoMiddleware,
    )

    providers = app()
    for name in (
        "_tracer",
        "_meter",
        "_duration_histogram_old",
        "_duration_histogram_new",
        "_active_request_counter",
        "_sem_conv_opt_in_mode",
        "_excluded_urls",
        "_otel_request_hook",
        "_otel_response_hook",
    ):
        monkeypatch.setattr(
            _DjangoMiddleware,
            name,
            getattr(_DjangoMiddleware, name),
            raising=False,
        )

    instrumentor = DjangoInstrumentor()
    try:
        instrumentor.instrument(
            tracer_provider=providers.tracer_provider,
            meter_provider=providers.meter_provider,
        )
        instrumentor.uninstrument()
        # Ours intentionally runs before contrib so begin_request sees the
        # ambient context before contrib opens its server span.
        settings.MIDDLEWARE = [
            MIDDLEWARE_PATH,
            DjangoInstrumentor._opentelemetry_middleware,
        ]
        response = Client(raise_request_exception=False).get("/observed-error/")
    finally:
        instrumentor.uninstrument()

    assert response.status_code == 500
    state = _state(response.wsgi_request)
    assert state is not None and state.config.http_instrumentation is False
    assert state.span is None and state.duration is None
    assert state.active_requests is None

    [server] = _server_spans(providers)
    records = _log_records(providers)
    assert len(records) == 1
    assert records[0].trace_id == server.context.trace_id
    assert records[0].span_id == server.context.span_id


def test_sync_stream_is_lazy_and_detaches_before_yield(app: Any) -> None:
    providers = app()
    response = Client().get("/observe/sync/")

    assert isinstance(response, StreamingHttpResponse)
    assert EVENTS == []
    state = _state(response.wsgi_request)
    assert state is not None and state.finished is False
    assert state.route == "observe/sync/"
    assert _server_spans(providers) == []

    chunks = iter(response.streaming_content)
    assert next(chunks) == b"observed-sync-1\n"
    assert EVENTS[0][0] == "sync-start"
    assert EVENTS[0][1] == state.span.get_span_context().span_id
    assert _span_id() is None
    assert state.finished is False

    assert next(chunks) == b"observed-sync-2\n"
    assert EVENTS[-1][0] == "sync-resume"
    assert EVENTS[-1][1] == state.span.get_span_context().span_id
    assert _span_id() is None
    with pytest.raises(StopIteration):
        next(chunks)

    assert state.finished is True
    response.close()
    assert EVENTS[-1][0] == "sync-closed"
    assert len(_server_spans(providers)) == 1
    metrics = _metric_data(providers)
    assert metrics[ACTIVE].data.data_points[0].value == 0
    assert sum(point.count for point in metrics[DURATION].data.data_points) == 1


async def test_async_stream_is_lazy_and_detaches_before_yield(app: Any) -> None:
    providers = app()
    response = await AsyncClient().get("/observe/async/")

    assert isinstance(response, StreamingHttpResponse)
    assert EVENTS == []
    state = _state(response.asgi_request)
    assert state is not None and state.finished is False
    assert state.route == "observe/async/"
    assert _server_spans(providers) == []

    content = response.streaming_content
    assert await anext(content) == b"observed-async-1\n"
    assert EVENTS[0][0] == "async-start"
    assert EVENTS[0][1] == state.span.get_span_context().span_id
    assert _span_id() is None
    assert state.finished is False

    assert await anext(content) == b"observed-async-2\n"
    assert EVENTS[-1][0] == "async-resume"
    assert EVENTS[-1][1] == state.span.get_span_context().span_id
    assert _span_id() is None
    with pytest.raises(StopAsyncIteration):
        await anext(content)

    assert state.finished is True
    await content.aclose()
    response.close()
    assert EVENTS[-1][0] == "async-closed"
    assert len(_server_spans(providers)) == 1
    metrics = _metric_data(providers)
    assert metrics[ACTIVE].data.data_points[0].value == 0
    assert sum(point.count for point in metrics[DURATION].data.data_points) == 1


def test_sync_stream_failure_is_reraised_and_finalized_once(app: Any) -> None:
    providers = app()
    response = Client().get("/observe/sync-error/")
    state = _state(response.wsgi_request)
    chunks = iter(response.streaming_content)

    assert next(chunks) == b"observed-sync-error-1\n"
    with pytest.raises(RuntimeError) as captured:
        next(chunks)
    assert captured.value is SYNC_STREAM_ERROR

    assert state.finished is True
    response.close()
    records = _log_records(providers)
    assert len(records) == 1
    assert records[0].event_name == "http.server.request.exception"
    [server] = _server_spans(providers)
    assert server.status.status_code is trace.StatusCode.ERROR
    assert server.attributes["error.type"] == "RuntimeError"
    assert _metric_data(providers)[ACTIVE].data.data_points[0].value == 0
    assert EVENTS[-1][0] == "sync-error-closed"


async def test_async_stream_failure_is_reraised_and_finalized_once(app: Any) -> None:
    providers = app()
    response = await AsyncClient().get("/observe/async-error/")
    state = _state(response.asgi_request)
    content = response.streaming_content

    assert await anext(content) == b"observed-async-error-1\n"
    with pytest.raises(RuntimeError) as captured:
        await anext(content)
    assert captured.value is ASYNC_STREAM_ERROR

    assert state.finished is True
    response.close()
    records = _log_records(providers)
    assert len(records) == 1
    assert records[0].event_name == "http.server.request.exception"
    [server] = _server_spans(providers)
    assert server.status.status_code is trace.StatusCode.ERROR
    assert server.attributes["error.type"] == "RuntimeError"
    assert _metric_data(providers)[ACTIVE].data.data_points[0].value == 0
    assert EVENTS[-1][0] == "async-error-closed"


def test_close_before_first_sync_iteration_finishes(app: Any) -> None:
    providers = app()
    response = Client().get("/observe/sync/")
    state = _state(response.wsgi_request)

    assert EVENTS == []
    assert _server_spans(providers) == []
    response.close()

    assert state.finished is True
    assert EVENTS == []
    assert len(_server_spans(providers)) == 1
    metrics = _metric_data(providers)
    assert metrics[ACTIVE].data.data_points[0].value == 0
    assert sum(point.count for point in metrics[DURATION].data.data_points) == 1


async def test_close_before_first_async_iteration_finishes(app: Any) -> None:
    providers = app()
    response = await AsyncClient().get("/observe/async/")
    state = _state(response.asgi_request)

    assert EVENTS == []
    assert _server_spans(providers) == []
    await response.aclose()

    assert state.finished is True
    assert EVENTS == []
    assert len(_server_spans(providers)) == 1
    metrics = _metric_data(providers)
    assert metrics[ACTIVE].data.data_points[0].value == 0
    assert sum(point.count for point in metrics[DURATION].data.data_points) == 1


def test_sync_early_close_runs_user_finally_under_request_context(app: Any) -> None:
    providers = app()
    handler = WSGIHandler()
    environ = RequestFactory().get("/observe/sync/").environ.copy()
    status_headers: list[Any] = []

    def start_response(status: str, headers: list[Any]) -> None:
        status_headers.append((status, headers))

    response = handler(environ, start_response)
    state = _state(REQUESTS[0])
    chunks = iter(response.streaming_content)

    assert status_headers
    assert next(chunks) == b"observed-sync-1\n"
    assert EVENTS[-1] == ("sync-start", state.span.get_span_context().span_id)

    response.close()

    assert state.finished is True
    assert EVENTS[-1] == ("sync-closed", state.span.get_span_context().span_id)
    assert _metric_data(providers)[ACTIVE].data.data_points[0].value == 0


async def test_async_early_aclose_runs_user_finally_under_request_context(
    app: Any,
) -> None:
    providers = app()
    handler = ASGIHandler()
    request = AsyncRequestFactory().get("/observe/async/")
    response = await handler.get_response_async(request)
    state = _state(request)

    content = response.streaming_content
    assert await anext(content) == b"observed-async-1\n"
    assert EVENTS[-1] == ("async-start", state.span.get_span_context().span_id)

    await response.aclose()

    assert state.finished is True
    assert EVENTS[-1] == ("async-closed", state.span.get_span_context().span_id)
    assert _metric_data(providers)[ACTIVE].data.data_points[0].value == 0


def test_sync_close_calls_custom_iterator_close_once(app: Any) -> None:
    providers = app()
    handler = WSGIHandler()
    environ = RequestFactory().get("/observe/closable-sync/").environ.copy()
    response = handler(environ, lambda status, headers: None)

    [iterator] = CLOSABLE_SYNC
    assert iterator.close_calls == 0
    response.close()
    assert iterator.close_calls == 1

    state = _state(REQUESTS[0])
    assert state.finished is True
    assert _metric_data(providers)[ACTIVE].data.data_points[0].value == 0


async def test_async_aclose_calls_custom_iterator_aclose_once(app: Any) -> None:
    providers = app()
    handler = ASGIHandler()
    request = AsyncRequestFactory().get("/observe/closable-async/")
    response = await handler.get_response_async(request)

    [iterator] = CLOSABLE_ASYNC
    assert iterator.aclose_calls == 0
    await response.aclose()
    assert iterator.aclose_calls == 1

    state = _state(request)
    assert state.finished is True
    assert _metric_data(providers)[ACTIVE].data.data_points[0].value == 0


async def test_asgi_disconnect_during_send_closes_suspended_generator(app: Any) -> None:
    providers = app()
    handler = ASGIHandler()
    scope = AsyncRequestFactory().get("/observe/async/").scope
    request_body_sent = [False]
    first_chunk_sent = asyncio.Event()
    block_send = asyncio.Event()

    async def receive() -> dict[str, Any]:
        if not request_body_sent[0]:
            request_body_sent[0] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await first_chunk_sent.wait()
        return {"type": "http.disconnect"}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            # The stream is suspended at yield, not inside __anext__, when the
            # disconnect cancels this blocked send.
            first_chunk_sent.set()
            await block_send.wait()

    await asyncio.wait_for(handler(scope, receive, send), timeout=5)

    [request] = REQUESTS
    state = _state(request)
    assert state is not None and state.finished is True
    assert any(event[0] == "async-closed" for event in EVENTS)
    assert EVENTS[-1] == ("async-closed", state.span.get_span_context().span_id)
    [server] = _server_spans(providers)
    assert server.context.span_id == state.span.get_span_context().span_id
    assert _log_records(providers) == []
    metrics = _metric_data(providers)
    assert metrics[ACTIVE].data.data_points[0].value == 0
    assert sum(point.count for point in metrics[DURATION].data.data_points) == 1


async def test_async_stream_cancellation_has_no_fabricated_log(app: Any) -> None:
    providers = app()
    response = await AsyncClient().get("/observe/async/")
    state = _state(response.asgi_request)
    started = asyncio.Event()

    async def consume() -> None:
        async for chunk in response.streaming_content:
            assert chunk == b"observed-async-1\n"
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    response.close()
    assert state.finished is True
    assert _log_records(providers) == []
    metrics = _metric_data(providers)
    assert metrics[ACTIVE].data.data_points[0].value == 0
    assert sum(point.count for point in metrics[DURATION].data.data_points) == 1


def test_base_exception_is_finished_and_reraised_without_log(app: Any) -> None:
    providers = app()

    with pytest.raises(ApplicationCancelled) as captured:
        Client().get("/observed-base/")

    assert captured.value is APPLICATION_CANCELLED
    [request] = REQUESTS
    state = _state(request)
    assert state is not None and state.finished is True
    assert state.route == "observed-base/"
    assert _log_records(providers) == []
    [server] = _server_spans(providers)
    assert server.status.status_code is trace.StatusCode.UNSET
    assert _metric_data(providers)[ACTIVE].data.data_points[0].value == 0


class _FakeFileWrapper:
    """Minimal WSGI file wrapper that closes via Django's patched file close."""

    instances: list[_FakeFileWrapper] = []

    def __init__(self, filelike: Any, block_size: int) -> None:
        self.file = filelike
        self.block_size = block_size
        self.closed = False
        self.instances.append(self)

    def close(self) -> None:
        self.closed = True
        self.file.close()


def test_file_response_wsgi_wrapper_fast_path_finalizes_on_close(app: Any) -> None:
    providers = app()
    handler = WSGIHandler()
    environ = RequestFactory().get("/file/").environ.copy()
    environ["wsgi.file_wrapper"] = _FakeFileWrapper
    _FakeFileWrapper.instances.clear()
    status_headers: list[Any] = []

    def start_response(status: str, headers: list[Any]) -> None:
        status_headers.append((status, headers))

    wrapper = handler(environ, start_response)

    assert status_headers
    assert isinstance(wrapper, _FakeFileWrapper)
    assert isinstance(wrapper.file, io.BytesIO)
    assert wrapper.file.tell() == 0
    assert wrapper.file.closed is False
    assert _server_spans(providers) == []

    wrapper.close()

    assert wrapper.closed is True
    assert wrapper.file.closed is True
    [server] = _server_spans(providers)
    assert server.attributes["http.route"] == "file/"
    metrics = _metric_data(providers)
    assert metrics[ACTIVE].data.data_points[0].value == 0
    assert sum(point.count for point in metrics[DURATION].data.data_points) == 1


def test_file_response_without_wrapper_still_streams(app: Any) -> None:
    providers = app()
    response = Client().get("/file/")

    assert isinstance(response, FileResponse)
    assert b"".join(response.streaming_content) == b"file-ok\n"
    response.close()

    [server] = _server_spans(providers)
    assert server.attributes["http.route"] == "file/"
    assert _metric_data(providers)[ACTIVE].data.data_points[0].value == 0


def test_telemetry_finish_failure_does_not_change_response(
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app()

    def broken_finish(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("telemetry failure")

    monkeypatch.setattr(
        middleware_module._telemetry,
        "finish_request",
        broken_finish,
    )
    response = Client().get("/sync/")

    assert response.status_code == 200
    assert response.content == b"sync-ok"


def test_config_fields_used_by_helper_are_real() -> None:
    """Keep the test configuration helper aligned with the frozen core."""

    names = set(Config.__dataclass_fields__)
    assert {
        "tracer_provider",
        "meter_provider",
        "logger_provider",
        "auto_configure",
        "http_instrumentation",
        "exclude",
    } <= names
