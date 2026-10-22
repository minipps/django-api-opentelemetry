from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from io import BytesIO
from typing import Any, cast

import pytest
from django.conf import settings
from django.core.handlers.asgi import ASGIRequest
from django.test import RequestFactory
from opentelemetry import trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.propagate import inject
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, ParentBased
from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, TraceFlags

from django_ninja_opentelemetry import _telemetry
from django_ninja_opentelemetry._config import Config
from django_ninja_opentelemetry._telemetry import (
    REQUEST_STATE_ATTRIBUTE,
    activate,
    begin_request,
    finish_request,
    operation,
    record_exception,
    record_validation,
)

if not settings.configured:
    settings.configure()

_DURATION = "http.server.request.duration"
_ACTIVE = "http.server.active_requests"


@dataclass
class Providers:
    tracer_provider: TracerProvider
    spans: InMemorySpanExporter
    meter_provider: MeterProvider
    metrics: InMemoryMetricReader
    logger_provider: LoggerProvider
    logs: InMemoryLogRecordExporter


@pytest.fixture(autouse=True)
def resolve_config_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the explicit providers on Config instead of importing _runtime."""

    def resolve(config: Config) -> tuple[Any, Any, Any]:
        return (
            config.tracer_provider,
            config.meter_provider,
            config.logger_provider,
        )

    monkeypatch.setattr(_telemetry, "_resolve_providers", resolve)


@pytest.fixture
def providers_factory() -> Iterator[Callable[..., Providers]]:
    created: list[Providers] = []

    def make(sampler: Any = None) -> Providers:
        span_exporter = InMemorySpanExporter()
        tracer_provider = TracerProvider(
            sampler=sampler if sampler is not None else ParentBased(ALWAYS_ON)
        )
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))

        metric_reader = InMemoryMetricReader()
        meter_provider = MeterProvider(metric_readers=[metric_reader])

        log_exporter = InMemoryLogRecordExporter()
        logger_provider = LoggerProvider()
        logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))

        bundle = Providers(
            tracer_provider=tracer_provider,
            spans=span_exporter,
            meter_provider=meter_provider,
            metrics=metric_reader,
            logger_provider=logger_provider,
            logs=log_exporter,
        )
        created.append(bundle)
        return bundle

    yield make

    for bundle in created:
        bundle.tracer_provider.shutdown()
        bundle.meter_provider.shutdown()
        bundle.logger_provider.shutdown()


@pytest.fixture
def providers(providers_factory: Callable[..., Providers]) -> Providers:
    return providers_factory()


@pytest.fixture
def warning_records(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> Iterator[pytest.LogCaptureFixture]:
    monkeypatch.setattr(_telemetry, "_WARNED", set())
    monkeypatch.setattr(_telemetry, "_WARN_LOCK", threading.Lock())
    with caplog.at_level(logging.WARNING, logger=_telemetry.__name__):
        yield caplog


def make_config(providers: Providers, **overrides: Any) -> Config:
    values: dict[str, Any] = {
        "tracer_provider": providers.tracer_provider,
        "meter_provider": providers.meter_provider,
        "logger_provider": providers.logger_provider,
    }
    values.update(overrides)
    return Config(**values)


def make_request(
    method: str = "GET",
    path: str = "/items/1/",
    secure: bool = False,
    **extra: Any,
) -> Any:
    return RequestFactory().generic(method, path, secure=secure, **extra)


def make_asgi_request(method: str = "GET", scheme: str = "https") -> ASGIRequest:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": scheme,
        "path": "/items/1/",
        "raw_path": b"/items/1/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"example.com")],
        "client": ("127.0.0.1", 1234),
        "server": ("example.com", 443),
    }
    return ASGIRequest(scope, BytesIO())


def log_records(providers: Providers) -> list[Any]:
    return [record.log_record for record in providers.logs.get_finished_logs()]


def metrics_by_name(providers: Providers) -> dict[str, Any]:
    data = providers.metrics.get_metrics_data()
    result: dict[str, Any] = {}
    if data is None:
        return result
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                result[metric.name] = metric
    return result


def test_begin_request_does_not_attach_and_activate_restores(
    providers: Providers,
) -> None:
    request = make_request()
    config = make_config(providers)
    before = trace.get_current_span()

    state = begin_request(request, config)

    assert state is not None
    assert getattr(request, REQUEST_STATE_ATTRIBUTE) is state
    assert state.span is not None
    assert state.context is not None
    assert trace.get_current_span() is before
    assert providers.spans.get_finished_spans() == ()

    with activate(state):
        assert (
            trace.get_current_span().get_span_context() == state.span.get_span_context()
        )
        with activate(state):
            assert (
                trace.get_current_span().get_span_context()
                == state.span.get_span_context()
            )
        assert (
            trace.get_current_span().get_span_context() == state.span.get_span_context()
        )

    assert trace.get_current_span() is before

    finish_request(state, 200)
    assert len(providers.spans.get_finished_spans()) == 1

    finish_request(state, 500)
    assert len(providers.spans.get_finished_spans()) == 1


async def test_activate_is_synchronous_in_async_code(providers: Providers) -> None:
    state = begin_request(make_request(), make_config(providers))
    assert state is not None
    before = trace.get_current_span()

    with activate(state):
        assert (
            trace.get_current_span().get_span_context() == state.span.get_span_context()
        )
        await asyncio.sleep(0)
        assert (
            trace.get_current_span().get_span_context() == state.span.get_span_context()
        )

    assert trace.get_current_span() is before
    finish_request(state, 200)


def test_server_span_uses_local_parent_over_remote_header(
    providers: Providers,
) -> None:
    carrier: dict[str, str] = {}
    remote_context = SpanContext(
        trace_id=0x1234567890ABCDEF1234567890ABCDEF,
        span_id=0x1234567890ABCDEF,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    inject(
        carrier,
        context=trace.set_span_in_context(NonRecordingSpan(remote_context)),
    )
    request = make_request(HTTP_TRACEPARENT=carrier["traceparent"])

    tracer = providers.tracer_provider.get_tracer("test")
    with tracer.start_as_current_span("local-parent") as parent:
        state = begin_request(request, make_config(providers))
        assert state is not None
        finish_request(state, 200)

    server = next(
        span
        for span in providers.spans.get_finished_spans()
        if span.kind is SpanKind.SERVER
    )
    assert server.parent is not None
    assert server.parent.span_id == parent.get_span_context().span_id
    assert server.context.trace_id == parent.get_span_context().trace_id


def test_server_span_extracts_remote_parent_without_local_span(
    providers: Providers,
) -> None:
    carrier: dict[str, str] = {}
    remote_context = SpanContext(
        trace_id=0x0FEDCBA9876543210FEDCBA98765432,
        span_id=0x0FEDCBA987654321,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    inject(
        carrier,
        context=trace.set_span_in_context(NonRecordingSpan(remote_context)),
    )

    state = begin_request(
        make_request(HTTP_TRACEPARENT=carrier["traceparent"]),
        make_config(providers),
    )
    assert state is not None
    finish_request(state, 200)

    server = next(
        span
        for span in providers.spans.get_finished_spans()
        if span.kind is SpanKind.SERVER
    )
    assert server.parent is not None
    assert server.parent.span_id == remote_context.span_id
    assert server.context.trace_id == remote_context.trace_id


def test_ambient_remote_span_is_not_preserved_as_local_parent(
    providers: Providers,
) -> None:
    ambient = SpanContext(
        trace_id=0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,
        span_id=0xAAAAAAAAAAAAAAAA,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    inbound = SpanContext(
        trace_id=0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB,
        span_id=0xBBBBBBBBBBBBBBBB,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    carrier: dict[str, str] = {}
    inject(
        carrier,
        context=trace.set_span_in_context(NonRecordingSpan(inbound)),
    )

    with trace.use_span(NonRecordingSpan(ambient), end_on_exit=False):
        state = begin_request(
            make_request(HTTP_TRACEPARENT=carrier["traceparent"]),
            make_config(providers),
        )
        assert state is not None
        finish_request(state, 200)

    server = next(
        span
        for span in providers.spans.get_finished_spans()
        if span.kind is SpanKind.SERVER
    )
    assert server.context.trace_id == inbound.trace_id
    assert server.parent is not None
    assert server.parent.span_id == inbound.span_id
    assert server.context.trace_id != ambient.trace_id


def test_exception_log_correlates_with_sampled_request_context(
    providers: Providers,
) -> None:
    state = begin_request(make_request(), make_config(providers))
    assert state is not None
    span_context = state.span.get_span_context()

    record_exception(state, RuntimeError("boom"))
    finish_request(state, 500)

    assert [record.event_name for record in log_records(providers)] == [
        "http.server.request.exception"
    ]
    record = log_records(providers)[0]
    assert record.severity_number is SeverityNumber.ERROR
    assert record.trace_id == span_context.trace_id
    assert record.span_id == span_context.span_id
    assert record.trace_flags & TraceFlags.SAMPLED


def test_exception_log_works_with_unsampled_context(
    providers_factory: Callable[..., Providers],
) -> None:
    providers = providers_factory(ParentBased(ALWAYS_OFF))
    state = begin_request(make_request(), make_config(providers))
    assert state is not None
    assert state.span is not None
    assert state.span.is_recording() is False
    span_context = state.span.get_span_context()

    record_exception(state, RuntimeError("unsampled"))
    finish_request(state, 500)

    assert providers.spans.get_finished_spans() == ()
    records = log_records(providers)
    assert len(records) == 1
    assert records[0].trace_id == span_context.trace_id
    assert records[0].span_id == span_context.span_id
    assert records[0].trace_flags & TraceFlags.SAMPLED == 0


def test_validation_warning_contains_only_count_and_route(
    providers: Providers,
) -> None:
    state = begin_request(make_request(), make_config(providers))
    assert state is not None
    state.route = "/ninja/items/<int:pk>"

    record_validation(state, 3)
    finish_request(state, 422)

    record = log_records(providers)[0]
    assert record.event_name == "django_ninja.validation.failed"
    assert record.severity_number is SeverityNumber.WARN
    assert record.severity_text == "WARN"
    assert record.body == "Request validation failed"
    assert record.exception is None
    assert record.attributes == {
        "django_ninja.validation.error_count": 3,
        "http.route": "/ninja/items/<int:pk>",
    }


def test_logging_disabled_skips_validation_and_exception_logs(
    providers: Providers,
) -> None:
    state = begin_request(make_request(), make_config(providers, logs=False))
    assert state is not None

    record_validation(state, 1)
    record_exception(state, RuntimeError("hidden"))
    finish_request(state, 500)

    assert log_records(providers) == []


def test_metrics_balance_and_use_route_templates(providers: Providers) -> None:
    config = make_config(providers)
    first = begin_request(make_request(path="/items/1/"), config)
    second = begin_request(make_request(path="/other/2/"), config)
    assert first is not None and second is not None
    first.route = "/items/<int:pk>"
    second.route = "/other/<int:pk>"

    active = metrics_by_name(providers)[_ACTIVE].data.data_points
    assert len(active) == 1
    assert active[0].value == 2
    assert active[0].attributes == {
        "http.request.method": "GET",
        "url.scheme": "http",
    }

    finish_request(first, 200)
    finish_request(second, 404)

    active = metrics_by_name(providers)[_ACTIVE].data.data_points
    assert active[0].value == 0

    duration_metric = metrics_by_name(providers)[_DURATION]
    assert duration_metric.unit == "s"
    points = duration_metric.data.data_points
    assert sum(point.count for point in points) == 2
    attributes = {tuple(sorted(point.attributes.items())) for point in points}
    assert attributes == {
        (
            ("http.request.method", "GET"),
            ("http.response.status_code", 200),
            ("http.route", "/items/<int:pk>"),
            ("url.scheme", "http"),
        ),
        (
            ("http.request.method", "GET"),
            ("http.response.status_code", 404),
            ("http.route", "/other/<int:pk>"),
            ("url.scheme", "http"),
        ),
    }
    assert all(point.sum >= 0 for point in points)
    assert all("/items/1/" not in str(point.attributes) for point in points)


def test_unknown_method_is_bounded_on_metrics_and_named_http(
    providers: Providers,
) -> None:
    state = begin_request(make_request(method="PROPFIND"), make_config(providers))
    assert state is not None
    assert state.method == "_OTHER"
    assert state.span_method == "HTTP"
    state.route = "/items/<int:pk>"

    finish_request(state, 200)

    server = next(
        span
        for span in providers.spans.get_finished_spans()
        if span.kind is SpanKind.SERVER
    )
    assert server.name == "HTTP /items/<int:pk>"
    assert server.attributes["http.request.method"] == "_OTHER"
    assert server.attributes["http.request.method_original"] == "PROPFIND"

    duration = metrics_by_name(providers)[_DURATION].data.data_points
    assert len(duration) == 1
    assert duration[0].attributes["http.request.method"] == "_OTHER"
    assert "PROPFIND" not in str(duration[0].attributes)


def test_asgi_request_scheme_is_used_for_metrics_and_span(
    providers: Providers,
) -> None:
    request = make_asgi_request(scheme="https")
    assert "wsgi.url_scheme" not in request.META

    state = begin_request(request, make_config(providers))
    assert state is not None
    assert state.active_attributes["url.scheme"] == "https"
    finish_request(state, 200)

    server = next(
        span
        for span in providers.spans.get_finished_spans()
        if span.kind is SpanKind.SERVER
    )
    assert server.attributes["url.scheme"] == "https"
    duration = metrics_by_name(providers)[_DURATION].data.data_points
    assert duration[0].attributes["url.scheme"] == "https"


def test_borrowed_http_context_has_no_owned_server_span_or_metrics(
    providers: Providers,
) -> None:
    tracer = providers.tracer_provider.get_tracer("test")

    def endpoint() -> None:
        pass

    with tracer.start_as_current_span("existing") as parent:
        parent_context = parent.get_span_context()
        state = begin_request(
            make_request(), make_config(providers, http_instrumentation=False)
        )
        assert state is not None
        assert state.span is None
        assert state.duration is None
        assert state.active_requests is None

        with operation(state, "endpoint", endpoint):
            pass

        finish_request(state, 200)

        assert trace.get_current_span().get_span_context() == parent_context
        assert parent.is_recording()

    spans = providers.spans.get_finished_spans()
    assert not any(span.kind is SpanKind.SERVER for span in spans)
    internal = next(span for span in spans if span.name == "django_ninja.endpoint")
    assert internal.parent is not None
    assert internal.parent.span_id == parent_context.span_id
    assert internal.context.trace_id == parent_context.trace_id
    assert _DURATION not in metrics_by_name(providers)
    assert _ACTIVE not in metrics_by_name(providers)


def test_tracing_disabled_disables_operation_spans_but_not_logs(
    providers: Providers,
) -> None:
    config = make_config(providers, tracing=False)
    state = begin_request(make_request(), config)
    assert state is not None
    assert state.span is None
    assert state.tracer is None

    with operation(state, "endpoint") as span:
        assert span is None

    record_exception(state, RuntimeError("still logged"))
    finish_request(state, 500)

    assert providers.spans.get_finished_spans() == ()
    assert len(log_records(providers)) == 1


def test_operation_spans_can_be_disabled(providers: Providers) -> None:
    state = begin_request(make_request(), make_config(providers, operation_spans=False))
    assert state is not None

    with operation(state, "endpoint") as span:
        assert span is None

    finish_request(state, 200)
    assert [span.kind for span in providers.spans.get_finished_spans()] == [
        SpanKind.SERVER
    ]


def test_nested_operations_preserve_current_request_trace_context(
    providers: Providers,
) -> None:
    state = begin_request(make_request(), make_config(providers))
    assert state is not None

    with activate(state):
        with operation(state, "input_resolution") as outer:
            with operation(state, "endpoint") as inner:
                assert outer is not None and inner is not None
                assert (
                    inner.get_span_context().trace_id
                    == outer.get_span_context().trace_id
                )

    finish_request(state, 200)
    spans = {
        span.name: span
        for span in providers.spans.get_finished_spans()
        if span.name.startswith("django_ninja.")
    }
    outer_span = spans["django_ninja.input_resolution"]
    inner_span = spans["django_ninja.endpoint"]
    assert inner_span.parent is not None
    assert inner_span.parent.span_id == outer_span.context.span_id


def test_operation_span_prefix_and_no_automatic_exception_logging(
    providers: Providers,
) -> None:
    state = begin_request(make_request(), make_config(providers))
    assert state is not None

    def sample_endpoint() -> None:
        pass

    with operation(state, "endpoint", sample_endpoint):
        pass

    with pytest.raises(ValueError, match="bad"):
        with operation(state, "endpoint", sample_endpoint):
            raise ValueError("bad")

    assert log_records(providers) == []
    finish_request(state, 200)

    internal = [
        span
        for span in providers.spans.get_finished_spans()
        if span.name.startswith("django_ninja.")
    ]
    assert len(internal) == 2
    assert all(
        span.attributes["code.function.name"]
        == f"{sample_endpoint.__module__}.{sample_endpoint.__qualname__}"
        for span in internal
    )
    assert all(span.events == () for span in internal)
    assert internal[0].status.status_code is trace.StatusCode.UNSET


def test_exception_identity_deduplication_and_cleanup(
    providers: Providers,
) -> None:
    state = begin_request(make_request(), make_config(providers))
    assert state is not None
    first = RuntimeError("first")
    second = ValueError("second")

    record_exception(state, first)
    record_exception(state, first)
    assert len(log_records(providers)) == 1
    assert state.exceptions == [first]

    record_exception(state, second)
    assert len(log_records(providers)) == 2
    assert state.exceptions == [first, second]

    finish_request(state, 500, error=first)
    finish_request(state, 500, error=first)

    assert len(log_records(providers)) == 2
    assert state.finished is True
    assert state.exceptions == []
    assert state.error_type == "RuntimeError"


def test_finish_marks_5xx_and_records_unexpected_errors_once(
    providers: Providers,
) -> None:
    plain_failure = begin_request(make_request(), make_config(providers))
    assert plain_failure is not None
    finish_request(plain_failure, 500)

    cancelled = begin_request(make_request(), make_config(providers))
    assert cancelled is not None
    finish_request(cancelled, 200, error=GeneratorExit())

    unexpected = begin_request(make_request(), make_config(providers))
    assert unexpected is not None
    finish_request(unexpected, 200, error=RuntimeError("unexpected"))

    assert len(log_records(providers)) == 1
    spans = providers.spans.get_finished_spans()
    assert [span.status.status_code for span in spans] == [
        trace.StatusCode.ERROR,
        trace.StatusCode.UNSET,
        trace.StatusCode.ERROR,
    ]
    assert spans[2].attributes["error.type"] == "RuntimeError"


def test_error_type_is_shared_by_failed_spans_and_duration(
    providers: Providers,
) -> None:
    config = make_config(providers)
    plain = begin_request(make_request(path="/plain/"), config)
    assert plain is not None
    plain.route = "/plain"
    finish_request(plain, 500)

    late = begin_request(make_request(path="/after/"), config)
    assert late is not None
    late.route = "/after"
    record_exception(late, RuntimeError("after status"))
    finish_request(late, 200)

    assert len(log_records(providers)) == 1

    spans = {
        span.attributes["http.route"]: span
        for span in providers.spans.get_finished_spans()
    }
    assert spans["/plain"].status.status_code is trace.StatusCode.ERROR
    assert spans["/plain"].attributes["error.type"] == "500"
    assert spans["/after"].status.status_code is trace.StatusCode.ERROR
    assert spans["/after"].attributes["error.type"] == "RuntimeError"

    points = {
        point.attributes["http.route"]: point
        for point in metrics_by_name(providers)[_DURATION].data.data_points
    }
    assert points["/plain"].attributes["http.response.status_code"] == 500
    assert points["/plain"].attributes["error.type"] == "500"
    assert points["/after"].attributes["http.response.status_code"] == 200
    assert points["/after"].attributes["error.type"] == "RuntimeError"

    active = metrics_by_name(providers)[_ACTIVE].data.data_points
    assert len(active) == 1
    assert active[0].value == 0
    assert active[0].attributes == {
        "http.request.method": "GET",
        "url.scheme": "http",
    }


def test_server_span_has_no_automatic_exception_events(
    providers: Providers,
) -> None:
    state = begin_request(make_request(), make_config(providers))
    assert state is not None
    with activate(state):
        record_exception(state, RuntimeError("event-free"))
    finish_request(state, 500)

    server = next(
        span
        for span in providers.spans.get_finished_spans()
        if span.kind is SpanKind.SERVER
    )
    assert server.events == ()


def test_excluded_request_stores_explicit_none_and_skips_providers(
    providers: Providers, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Config] = []

    def resolve(config: Config) -> tuple[Any, Any, Any]:
        calls.append(config)
        return (None, None, None)

    monkeypatch.setattr(_telemetry, "_resolve_providers", resolve)
    request = make_request()
    setattr(request, REQUEST_STATE_ATTRIBUTE, object())

    state = begin_request(request, make_config(providers, exclude=lambda unused: True))

    assert state is None
    assert getattr(request, REQUEST_STATE_ATTRIBUTE) is None
    assert calls == []
    assert providers.spans.get_finished_spans() == ()
    assert log_records(providers) == []


def test_exclusion_errors_propagate(
    providers: Providers,
) -> None:
    def broken_exclude(unused: Any) -> bool:
        raise ValueError("bad exclusion")

    with pytest.raises(ValueError, match="bad exclusion"):
        begin_request(make_request(), make_config(providers, exclude=broken_exclude))


def test_sdk_disabled_stores_explicit_none(
    providers: Providers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Config] = []

    def resolve(config: Config) -> tuple[Any, Any, Any]:
        calls.append(config)
        return (None, None, None)

    monkeypatch.setattr(_telemetry, "_resolve_providers", resolve)
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    request = make_request()
    setattr(request, REQUEST_STATE_ATTRIBUTE, object())

    assert begin_request(request, make_config(providers)) is None
    assert getattr(request, REQUEST_STATE_ATTRIBUTE) is None
    assert calls == []
    assert providers.spans.get_finished_spans() == ()
    assert log_records(providers) == []


def test_all_signals_disabled_returns_none(providers: Providers) -> None:
    request = make_request()
    setattr(request, REQUEST_STATE_ATTRIBUTE, object())
    config = make_config(providers, tracing=False, metrics=False, logs=False)

    assert begin_request(request, config) is None
    assert getattr(request, REQUEST_STATE_ATTRIBUTE) is None


def test_finish_is_idempotent_without_server_span(
    providers: Providers,
) -> None:
    state = begin_request(
        make_request(),
        make_config(providers, http_instrumentation=False, metrics=False),
    )
    assert state is not None
    assert state.span is None
    record_exception(state, RuntimeError("recorded once"))
    finish_request(state, 500)
    finish_request(state, 500, error=RuntimeError("second"))

    assert state.finished is True
    assert state.exceptions == []
    assert providers.spans.get_finished_spans() == ()
    assert len(log_records(providers)) == 1


def test_none_state_helpers_are_noops() -> None:
    with activate(None):
        pass
    with operation(None, "endpoint") as span:
        assert span is None
    record_validation(None, 1)
    record_exception(None, RuntimeError("ignored"))
    finish_request(None, 500)


class _CountingTracerProvider:
    def __init__(self, inner: TracerProvider) -> None:
        self.inner = inner
        self.calls = 0

    def get_tracer(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self.inner.get_tracer(*args, **kwargs)


class _CountingMeter:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.histograms = 0
        self.counters = 0

    def create_histogram(self, *args: Any, **kwargs: Any) -> Any:
        self.histograms += 1
        return self.inner.create_histogram(*args, **kwargs)

    def create_up_down_counter(self, *args: Any, **kwargs: Any) -> Any:
        self.counters += 1
        return self.inner.create_up_down_counter(*args, **kwargs)


class _CountingMeterProvider:
    def __init__(self, inner: MeterProvider) -> None:
        self.inner = inner
        self.meter = _CountingMeter(inner.get_meter("test"))
        self.calls = 0

    def get_meter(self, *args: Any, **kwargs: Any) -> _CountingMeter:
        self.calls += 1
        return self.meter


class _CountingLoggerProvider:
    def __init__(self, inner: LoggerProvider) -> None:
        self.inner = inner
        self.calls = 0

    def get_logger(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self.inner.get_logger(*args, **kwargs)


def test_instruments_are_cached_by_provider_identity(
    providers: Providers,
) -> None:
    tracer_provider = _CountingTracerProvider(providers.tracer_provider)
    meter_provider = _CountingMeterProvider(providers.meter_provider)
    logger_provider = _CountingLoggerProvider(providers.logger_provider)
    config = Config(
        tracer_provider=cast(Any, tracer_provider),
        meter_provider=cast(Any, meter_provider),
        logger_provider=cast(Any, logger_provider),
    )

    first = begin_request(make_request(), config)
    second = begin_request(make_request(), config)
    assert first is not None and second is not None
    finish_request(first, 200)
    finish_request(second, 200)

    assert tracer_provider.calls == 1
    assert meter_provider.calls == 1
    assert meter_provider.meter.histograms == 1
    assert meter_provider.meter.counters == 1
    assert logger_provider.calls == 1


class _FailingLogger:
    def emit(self, **kwargs: Any) -> None:
        raise RuntimeError("logger down")


class _FailingLoggerProvider:
    def get_logger(self, *args: Any, **kwargs: Any) -> _FailingLogger:
        return _FailingLogger()


class _BrokenHistogramMeter:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def create_histogram(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("histogram down")

    def create_up_down_counter(self, *args: Any, **kwargs: Any) -> Any:
        return self.inner.create_up_down_counter(*args, **kwargs)


class _BrokenHistogramMeterProvider:
    def __init__(self, inner: MeterProvider) -> None:
        self.meter = _BrokenHistogramMeter(inner.get_meter("broken"))

    def get_meter(self, *args: Any, **kwargs: Any) -> _BrokenHistogramMeter:
        return self.meter


class _BrokenHistogram:
    def record(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("record down")


class _RecordingMeter:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def create_histogram(self, *args: Any, **kwargs: Any) -> _BrokenHistogram:
        return _BrokenHistogram()

    def create_up_down_counter(self, *args: Any, **kwargs: Any) -> Any:
        return self.inner.create_up_down_counter(*args, **kwargs)


class _RecordingMeterProvider:
    def __init__(self, inner: MeterProvider) -> None:
        self.meter = _RecordingMeter(inner.get_meter("broken-record"))

    def get_meter(self, *args: Any, **kwargs: Any) -> _RecordingMeter:
        return self.meter


class _BrokenTracerProvider:
    def get_tracer(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("tracer down")


class _BrokenTracer:
    def start_span(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("span down")

    def start_as_current_span(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("operation span down")


class _BrokenTracerProviderWithTracer:
    def get_tracer(self, *args: Any, **kwargs: Any) -> _BrokenTracer:
        return _BrokenTracer()


def test_logging_failures_do_not_break_completion(
    providers: Providers, warning_records: pytest.LogCaptureFixture
) -> None:
    config = make_config(providers, logger_provider=cast(Any, _FailingLoggerProvider()))
    state = begin_request(make_request(), config)
    assert state is not None

    record_validation(state, 1)
    record_exception(state, RuntimeError("logged once"))
    finish_request(state, 500)

    assert state.finished is True
    assert state.exceptions == []
    assert state.error_type == "RuntimeError"
    active = metrics_by_name(providers)[_ACTIVE].data.data_points
    assert active[0].value == 0
    assert len(warning_records.records) == 1
    assert "log record" in warning_records.records[0].message


def test_histogram_setup_failure_keeps_active_requests_balanced(
    providers: Providers, warning_records: pytest.LogCaptureFixture
) -> None:
    config = make_config(
        providers,
        meter_provider=cast(
            Any, _BrokenHistogramMeterProvider(providers.meter_provider)
        ),
    )
    state = begin_request(make_request(), config)
    assert state is not None
    assert state.duration is None
    assert state.active_requests is not None

    finish_request(state, 200)

    active = metrics_by_name(providers)[_ACTIVE].data.data_points
    assert active[0].value == 0
    assert _DURATION not in metrics_by_name(providers)
    assert len(warning_records.records) == 1
    assert "duration metric" in warning_records.records[0].message


def test_duration_record_failure_keeps_active_requests_balanced(
    providers: Providers, warning_records: pytest.LogCaptureFixture
) -> None:
    config = make_config(
        providers,
        meter_provider=cast(Any, _RecordingMeterProvider(providers.meter_provider)),
    )
    state = begin_request(make_request(), config)
    assert state is not None

    finish_request(state, 200)

    active = metrics_by_name(providers)[_ACTIVE].data.data_points
    assert active[0].value == 0
    assert len(warning_records.records) == 1
    assert "request duration" in warning_records.records[0].message


def test_tracer_setup_failure_degrades_to_logs_and_metrics(
    providers: Providers, warning_records: pytest.LogCaptureFixture
) -> None:
    config = make_config(providers, tracer_provider=cast(Any, _BrokenTracerProvider()))
    state = begin_request(make_request(), config)
    assert state is not None
    assert state.tracer is None
    assert state.span is None

    record_exception(state, RuntimeError("without tracer"))
    finish_request(state, 500)

    assert len(log_records(providers)) == 1
    active = metrics_by_name(providers)[_ACTIVE].data.data_points
    assert active[0].value == 0
    assert len(warning_records.records) == 1
    assert "tracer setup" in warning_records.records[0].message


def test_operation_span_failures_preserve_application_exception(
    providers: Providers, warning_records: pytest.LogCaptureFixture
) -> None:
    config = make_config(
        providers,
        tracer_provider=cast(Any, _BrokenTracerProviderWithTracer()),
    )

    for _ in range(2):
        state = begin_request(make_request(), config)
        assert state is not None
        assert state.span is None
        executed = False
        with pytest.raises(ValueError, match="application"):
            with operation(state, "endpoint"):
                executed = True
                raise ValueError("application")
        assert executed is True
        finish_request(state, 200)

    assert len(warning_records.records) == 2
    assert {record.message for record in warning_records.records} == {
        "Failed to start HTTP server span (RuntimeError)",
        "Failed to start operation span (RuntimeError)",
    }


def test_identity_cache_is_bounded_and_resettable() -> None:
    cache = _telemetry._IdentityCache()
    objects = [object() for _ in range(12)]
    for index, provider in enumerate(objects):
        assert cache.get(provider, lambda index=index: index) == index
    assert len(cache._values) == 8

    cache.reset()
    assert cache._values == {}


def test_reset_after_fork_replaces_locks_and_caches() -> None:
    old_warning_lock = _telemetry._WARN_LOCK
    _telemetry._TRACERS.get(object(), lambda: "cached")
    _telemetry._WARNED.add("warning-key")

    _telemetry._reset_after_fork()

    assert _telemetry._TRACERS._values == {}
    assert _telemetry._WARNED == set()
    assert _telemetry._WARN_LOCK is not old_warning_lock
