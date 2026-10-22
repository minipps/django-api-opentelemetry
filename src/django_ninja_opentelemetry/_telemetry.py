"""Request-scoped telemetry recording for Django and Django Ninja."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar, cast

from opentelemetry import _logs, metrics, propagate, trace
from opentelemetry import context as otel_context
from opentelemetry._logs import Logger, LoggerProvider, SeverityNumber
from opentelemetry.context import Context
from opentelemetry.metrics import Histogram, Meter, MeterProvider, UpDownCounter
from opentelemetry.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Tracer,
    TracerProvider,
)

from ._config import Config

if TYPE_CHECKING:
    from django.http import HttpRequest

#: Explicit per-request state attribute stored on ``HttpRequest``.
REQUEST_STATE_ATTRIBUTE = "_django_ninja_otel"

_INSTRUMENTATION_NAME = "django_ninja_opentelemetry"
_DURATION_NAME = "http.server.request.duration"
_ACTIVE_REQUESTS_NAME = "http.server.active_requests"
_DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    7.5,
    10.0,
)
_METHOD_ATTRIBUTE = "http.request.method"
_METHOD_ORIGINAL_ATTRIBUTE = "http.request.method_original"
_ROUTE_ATTRIBUTE = "http.route"
_SCHEME_ATTRIBUTE = "url.scheme"
_STATUS_ATTRIBUTE = "http.response.status_code"
_ERROR_TYPE_ATTRIBUTE = "error.type"
_VALIDATION_COUNT_ATTRIBUTE = "django_ninja.validation.error_count"
_EXCEPTION_EVENT_NAME = "http.server.request.exception"
_VALIDATION_EVENT_NAME = "django_ninja.validation.failed"
_SDK_DISABLED_ENV = "OTEL_SDK_DISABLED"

_HTTP_METHODS = frozenset(
    {
        "CONNECT",
        "DELETE",
        "GET",
        "HEAD",
        "OPTIONS",
        "PATCH",
        "POST",
        "PUT",
        "QUERY",
        "TRACE",
    }
)
_OTHER_METHOD = "_OTHER"


@dataclass
class RequestTelemetry:
    """Mutable telemetry state explicitly associated with one request."""

    config: Config
    tracer_provider: TracerProvider | None
    meter_provider: MeterProvider | None
    logger_provider: LoggerProvider | None
    tracer: Tracer | None
    logger: Logger | None
    context: Context
    span: Span | None
    span_method: str
    method: str
    started: float
    route: str | None = None
    status_code: int | None = None
    exceptions: list[BaseException] = field(default_factory=list)
    error_type: str | None = None
    finished: bool = False
    duration: Histogram | None = None
    active_requests: UpDownCounter | None = None
    active_attributes: dict[str, str] = field(default_factory=dict)
    active: bool = False


_T = TypeVar("_T")
_CACHE_SIZE = 8
_WARN_LOCK = threading.Lock()
_WARNED: set[str] = set()
_WARNING_LOGGER = logging.getLogger(__name__)


class _IdentityCache:
    """Bounded identity cache with one lock held through value creation.

    Strong entries are intentional and bounded; no weak-reference lifecycle
    guarantee is claimed. A forked child starts with fresh locks and values.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[int, tuple[Any, Any]] = {}

    def get(self, provider: Any, factory: Callable[[], _T]) -> _T:
        with self._lock:
            entry = self._values.get(id(provider))
            if entry is not None and entry[0] is provider:
                return cast(_T, entry[1])
            value = factory()
            self._values[id(provider)] = (provider, value)
            while len(self._values) > _CACHE_SIZE:
                del self._values[next(iter(self._values))]
            return value

    def reset(self) -> None:
        self._lock = threading.Lock()
        self._values.clear()


_TRACERS = _IdentityCache()
_LOGGERS = _IdentityCache()
_METERS = _IdentityCache()
_DURATIONS = _IdentityCache()
_ACTIVE_REQUESTS = _IdentityCache()
_CACHES = (_TRACERS, _LOGGERS, _METERS, _DURATIONS, _ACTIVE_REQUESTS)


def _reset_after_fork() -> None:
    global _WARN_LOCK

    for cache in _CACHES:
        cache.reset()
    _WARN_LOCK = threading.Lock()
    _WARNED.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _warn_once(key: str, message: str, error: BaseException | None = None) -> None:
    with _WARN_LOCK:
        if key in _WARNED:
            return
        _WARNED.add(key)
    if error is None:
        _WARNING_LOGGER.warning(message)
    else:
        _WARNING_LOGGER.warning("%s (%s)", message, type(error).__name__)


def _resolve_providers(config: Config) -> tuple[Any, Any, Any]:
    from ._runtime import get_providers

    return get_providers(config)


def _selected_providers(config: Config) -> tuple[Any, Any, Any]:
    try:
        return _resolve_providers(config)
    except Exception as error:
        _warn_once("providers", "OpenTelemetry provider setup failed", error)
        return (
            config.tracer_provider,
            config.meter_provider,
            config.logger_provider,
        )


def _sdk_disabled() -> bool:
    return os.environ.get(_SDK_DISABLED_ENV, "").strip().lower() in {
        "true",
        "1",
    }


def _tracer_for(provider: TracerProvider | None) -> Tracer | None:
    try:
        actual = provider if provider is not None else trace.get_tracer_provider()
        return _TRACERS.get(actual, lambda: actual.get_tracer(_INSTRUMENTATION_NAME))
    except Exception as error:
        _warn_once("tracer", "OpenTelemetry tracer setup failed", error)
        return None


def _logger_for(provider: LoggerProvider | None) -> Logger | None:
    try:
        actual = provider if provider is not None else _logs.get_logger_provider()
        return _LOGGERS.get(actual, lambda: actual.get_logger(_INSTRUMENTATION_NAME))
    except Exception as error:
        _warn_once("logger", "OpenTelemetry logger setup failed", error)
        return None


def _meter_for(provider: MeterProvider | None) -> Meter | None:
    try:
        actual = provider if provider is not None else metrics.get_meter_provider()
        return _METERS.get(actual, lambda: actual.get_meter(_INSTRUMENTATION_NAME))
    except Exception as error:
        _warn_once("meter", "OpenTelemetry meter setup failed", error)
        return None


def _duration_for(meter: Meter) -> Histogram | None:
    try:
        return _DURATIONS.get(
            meter,
            lambda: meter.create_histogram(
                _DURATION_NAME,
                unit="s",
                description="Duration of HTTP server requests.",
                explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
            ),
        )
    except Exception as error:
        _warn_once("duration", "OpenTelemetry duration metric setup failed", error)
        return None


def _active_for(meter: Meter) -> UpDownCounter | None:
    try:
        return _ACTIVE_REQUESTS.get(
            meter,
            lambda: meter.create_up_down_counter(
                _ACTIVE_REQUESTS_NAME,
                unit="{request}",
                description="Number of active HTTP server requests.",
            ),
        )
    except Exception as error:
        _warn_once(
            "active-requests",
            "OpenTelemetry active request metric setup failed",
            error,
        )
        return None


def begin_request(request: HttpRequest, config: Config) -> RequestTelemetry | None:
    """Return request state, or ``None`` with an explicit disabled marker."""

    if _sdk_disabled() or not (config.tracing or config.metrics or config.logs):
        setattr(request, REQUEST_STATE_ATTRIBUTE, None)
        return None

    exclude = config.exclude
    if exclude is not None and exclude(request):
        setattr(request, REQUEST_STATE_ATTRIBUTE, None)
        return None

    started = time.monotonic()
    tracer_provider, meter_provider, logger_provider = _selected_providers(config)
    tracer = _tracer_for(tracer_provider) if config.tracing else None
    logger = _logger_for(logger_provider) if config.logs else None

    duration = active_requests = None
    if config.metrics and config.http_instrumentation:
        meter = _meter_for(meter_provider)
        if meter is not None:
            duration = _duration_for(meter)
            active_requests = _active_for(meter)

    method, span_method, original_method = _method(request)
    active_attributes = {
        _METHOD_ATTRIBUTE: method,
        _SCHEME_ATTRIBUTE: _scheme(request),
    }

    span = None
    context = _parent_context(request, config)
    if config.tracing and config.http_instrumentation and tracer is not None:
        span_attributes: dict[str, Any] = dict(active_attributes)
        if original_method is not None:
            span_attributes[_METHOD_ORIGINAL_ATTRIBUTE] = original_method
        try:
            span = tracer.start_span(
                span_method,
                context=context,
                kind=SpanKind.SERVER,
                attributes=span_attributes,
            )
            context = trace.set_span_in_context(span, context)
        except Exception as error:
            _warn_once("server-span", "Failed to start HTTP server span", error)
            if span is not None:
                try:
                    span.end()
                except Exception as end_error:
                    _warn_once(
                        "server-span-end",
                        "Failed to end HTTP server span",
                        end_error,
                    )
            span = None
            context = _parent_context(request, config)

    state = RequestTelemetry(
        config=config,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        tracer=tracer,
        logger=logger,
        context=context,
        span=span,
        span_method=span_method,
        method=method,
        started=started,
        duration=duration,
        active_requests=active_requests,
        active_attributes=active_attributes,
    )
    if active_requests is not None:
        try:
            active_requests.add(1, active_attributes)
        except Exception as error:
            _warn_once(
                "active-start",
                "Failed to increment active HTTP requests",
                error,
            )
        else:
            state.active = True

    setattr(request, REQUEST_STATE_ATTRIBUTE, state)
    return state


@contextmanager
def activate(
    state: RequestTelemetry | None,
) -> Generator[RequestTelemetry | None, None, None]:
    """Attach and restore the saved request context in the current task."""

    if state is None:
        yield None
        return

    token = otel_context.attach(state.context)
    try:
        yield state
    finally:
        otel_context.detach(token)


@contextmanager
def operation(
    state: RequestTelemetry | None,
    name: str,
    function: Callable[..., Any] | None = None,
) -> Generator[Span | None, None, None]:
    """Time an internal span without recording or swallowing exceptions."""

    if (
        state is None
        or state.finished
        or state.tracer is None
        or not state.config.tracing
        or not state.config.operation_spans
    ):
        yield None
        return

    span_name = name if name.startswith("django_ninja.") else f"django_ninja.{name}"
    attributes: dict[str, Any] = {}
    if function is not None:
        module = getattr(function, "__module__", type(function).__module__)
        qualname = getattr(function, "__qualname__", type(function).__qualname__)
        attributes["code.function.name"] = f"{module}.{qualname}"

    manager = None
    try:
        manager = state.tracer.start_as_current_span(
            span_name,
            context=_operation_parent(state),
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        )
        span = manager.__enter__()
    except Exception as error:
        _warn_once("operation-span", "Failed to start operation span", error)
        if manager is not None:
            try:
                manager.__exit__(*sys.exc_info())
            except Exception as close_error:
                _warn_once(
                    "operation-finish",
                    "Failed to finish operation span",
                    close_error,
                )
        yield None
        return

    try:
        yield span
    except BaseException:
        exc_info = sys.exc_info()
        try:
            manager.__exit__(*exc_info)
        except Exception as error:
            _warn_once("operation-finish", "Failed to finish operation span", error)
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception as error:
            _warn_once("operation-finish", "Failed to finish operation span", error)


def record_validation(state: RequestTelemetry | None, error_count: int) -> None:
    """Emit a warning with the validation count and route only."""

    if state is None or state.finished or not state.config.logs or state.logger is None:
        return

    attributes: dict[str, Any] = {_VALIDATION_COUNT_ATTRIBUTE: error_count}
    if state.route:
        attributes[_ROUTE_ATTRIBUTE] = state.route
    try:
        state.logger.emit(
            event_name=_VALIDATION_EVENT_NAME,
            timestamp=time.time_ns(),
            context=state.context,
            severity_number=SeverityNumber.WARN,
            severity_text="WARN",
            body="Request validation failed",
            attributes=attributes,
        )
    except Exception as error:
        _warn_once("log-emit", "Failed to emit OpenTelemetry log record", error)


def record_exception(state: RequestTelemetry | None, exc: BaseException) -> None:
    """Record an unexpected exception once by identity, then emit a log."""

    if state is None or state.finished or not isinstance(exc, Exception):
        return
    if any(previous is exc for previous in state.exceptions):
        return

    state.exceptions.append(exc)
    if state.error_type is None:
        state.error_type = _exception_type(exc)
    if not state.config.logs or state.logger is None:
        return

    attributes = {_ROUTE_ATTRIBUTE: state.route} if state.route else None
    try:
        state.logger.emit(
            exception=exc,
            event_name=_EXCEPTION_EVENT_NAME,
            timestamp=time.time_ns(),
            context=state.context,
            severity_number=SeverityNumber.ERROR,
            severity_text="ERROR",
            body="Unhandled exception in Django request",
            attributes=attributes,
        )
    except Exception as error:
        _warn_once("log-emit", "Failed to emit OpenTelemetry log record", error)


def finish_request(
    state: RequestTelemetry | None,
    status_code: int | None,
    error: BaseException | None = None,
) -> None:
    """Complete the owned span and metrics exactly once."""

    if state is None or state.finished:
        return

    if isinstance(error, Exception):
        record_exception(state, error)
    state.finished = True
    if status_code is not None:
        state.status_code = int(status_code)

    route = state.route or None
    failed = (
        state.status_code is not None and state.status_code >= 500
    ) or state.error_type is not None
    if failed and state.error_type is None:
        state.error_type = str(state.status_code)

    try:
        if state.span is not None:
            try:
                _finish_span(state, route, failed)
            except Exception as error_type:
                _warn_once(
                    "span-finish", "Failed to finish HTTP server span", error_type
                )
        if state.duration is not None:
            try:
                _record_duration(state, route)
            except Exception as error_type:
                _warn_once(
                    "duration-record",
                    "Failed to record HTTP request duration",
                    error_type,
                )
    finally:
        if state.active_requests is not None and state.active:
            try:
                state.active_requests.add(-1, state.active_attributes)
            except Exception as error_type:
                _warn_once(
                    "active-finish",
                    "Failed to decrement active HTTP requests",
                    error_type,
                )
            finally:
                state.active = False
        state.exceptions.clear()


def _finish_span(state: RequestTelemetry, route: str | None, failed: bool) -> None:
    span = state.span
    assert span is not None
    try:
        if route:
            span.set_attribute(_ROUTE_ATTRIBUTE, route)
            span.update_name(f"{state.span_method} {route}")
        if state.status_code is not None:
            span.set_attribute(_STATUS_ATTRIBUTE, state.status_code)
        if failed:
            if state.error_type is not None:
                span.set_attribute(_ERROR_TYPE_ATTRIBUTE, state.error_type)
            span.set_status(Status(StatusCode.ERROR))
    finally:
        span.end()


def _record_duration(state: RequestTelemetry, route: str | None) -> None:
    duration = state.duration
    assert duration is not None
    attributes: dict[str, Any] = dict(state.active_attributes)
    if route:
        attributes[_ROUTE_ATTRIBUTE] = route
    if state.status_code is not None:
        attributes[_STATUS_ATTRIBUTE] = state.status_code
    if state.error_type is not None:
        attributes[_ERROR_TYPE_ATTRIBUTE] = state.error_type
    duration.record(max(0.0, time.monotonic() - state.started), attributes)


def _exception_type(exc: BaseException) -> str:
    cls = type(exc)
    return (
        cls.__qualname__
        if cls.__module__ == "builtins"
        else f"{cls.__module__}.{cls.__qualname__}"
    )


def _method(request: HttpRequest) -> tuple[str, str, str | None]:
    raw = getattr(request, "method", "")
    original = raw if isinstance(raw, str) else ""
    normalized = original.upper()
    if normalized in _HTTP_METHODS:
        return normalized, normalized, (original if original != normalized else None)
    return _OTHER_METHOD, "HTTP", original or None


def _scheme(request: HttpRequest) -> str:
    scheme = getattr(request, "scheme", None)
    if isinstance(scheme, str) and scheme:
        return scheme
    meta = getattr(request, "META", None)
    if isinstance(meta, Mapping):
        value = meta.get("wsgi.url_scheme")
        if isinstance(value, str) and value:
            return value
    return "http"


def _parent_context(request: HttpRequest, config: Config) -> Context:
    current = otel_context.get_current()
    if not config.http_instrumentation:
        return current

    span_context = trace.get_current_span(current).get_span_context()
    if span_context.is_valid and not span_context.is_remote:
        return current

    headers = getattr(request, "headers", None)
    try:
        return propagate.extract(headers if headers is not None else {})
    except Exception as error:
        _warn_once("propagator", "Failed to extract request context", error)
        return Context()


def _operation_parent(state: RequestTelemetry) -> Context:
    current_context = otel_context.get_current()
    current = trace.get_current_span(current_context).get_span_context()
    request = trace.get_current_span(state.context).get_span_context()
    if current.is_valid and request.is_valid and current.trace_id == request.trace_id:
        return current_context
    return state.context
