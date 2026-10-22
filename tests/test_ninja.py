"""Focused tests for the guarded Django Ninja 1.7 integration."""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import textwrap
import types
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Any

import pytest
from django.test import AsyncClient, Client, override_settings
from django.urls import path
from opentelemetry import trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.trace import SpanKind, StatusCode

from django_ninja_opentelemetry import _ninja
from django_ninja_opentelemetry._config import Config
from django_ninja_opentelemetry._telemetry import (
    REQUEST_STATE_ATTRIBUTE,
    activate,
    begin_request,
    finish_request,
)

if TYPE_CHECKING:
    from django.http import HttpRequest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.project.settings")

_VALIDATION_EVENT = "django_ninja.validation.failed"
_EXCEPTION_EVENT = "http.server.request.exception"
_INPUT_SPAN = "django_ninja.input_resolution"
_ENDPOINT_SPAN = "django_ninja.endpoint"
_SERIALIZATION_SPAN = "django_ninja.serialization"
_MIDDLEWARE = "tests.test_ninja.TelemetryMiddleware"


class TelemetryMiddleware:
    """Minimal test middleware until the installed-app middleware exists."""

    sync_capable = True
    async_capable = True

    config: Config | None = None

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response
        self.async_mode = inspect.iscoroutinefunction(get_response)
        if self.async_mode:
            inspect.markcoroutinefunction(self)

    def __call__(self, request: HttpRequest) -> Any:
        if self.async_mode:
            return self.__acall__(request)
        return self._handle_sync(request)

    async def __acall__(self, request: HttpRequest) -> Any:
        state = self._begin(request)
        try:
            with activate(state):
                response = await self.get_response(request)
        except BaseException as exc:
            self._finish(request, state, 500, exc)
            raise
        self._finish(request, state, getattr(response, "status_code", None))
        return response

    def _handle_sync(self, request: HttpRequest) -> Any:
        state = self._begin(request)
        try:
            with activate(state):
                response = self.get_response(request)
        except BaseException as exc:
            self._finish(request, state, 500, exc)
            raise
        self._finish(request, state, getattr(response, "status_code", None))
        return response

    def _begin(self, request: HttpRequest) -> Any:
        if self.config is None:
            setattr(request, REQUEST_STATE_ATTRIBUTE, None)
            return None
        return begin_request(request, self.config)

    def _finish(
        self,
        request: HttpRequest,
        state: Any,
        status_code: int | None,
        error: BaseException | None = None,
    ) -> None:
        if state is not None and getattr(state, "route", None) is None:
            match = getattr(request, "resolver_match", None)
            route = getattr(match, "route", None)
            if isinstance(route, str) and route:
                state.route = route
        finish_request(state, status_code, error)


@dataclass(frozen=True)
class NinjaHarness:
    api: Any
    other_api: Any
    providers: Any
    config: Config
    stream_probes: list[tuple[str, str | None]]


def _current_span_name() -> str | None:
    return getattr(trace.get_current_span(), "name", None)


def _build_apis() -> tuple[Any, Any, list[tuple[str, str | None]]]:
    from ninja import NinjaAPI, Router, Schema
    from ninja.errors import AuthenticationError, HttpError
    from ninja.streaming import JSONL
    from ninja.throttling import BaseThrottle
    from pydantic import Field

    class ItemIn(Schema):
        name: str
        quantity: int = Field(gt=0)

    class ItemOut(Schema):
        item_id: int
        name: str
        quantity: int

    # ``from __future__ import annotations`` makes the route annotations
    # strings.  Ninja resolves them against the defining function's module
    # globals, so expose the locally built schemas there before registering
    # routes.  This keeps the test module importable without the Ninja extra.
    globals().update(ItemIn=ItemIn, ItemOut=ItemOut)

    class Custom409(Exception):
        pass

    class Custom503(Exception):
        pass

    class CustomRaise(Exception):
        pass

    class DenyThrottle(BaseThrottle):
        def allow_request(self, request: HttpRequest) -> bool:
            return False

        def wait(self) -> float:
            return 1.0

    suffix = uuid.uuid4().hex
    api = NinjaAPI(urls_namespace=f"ninja_test_{suffix}")
    other_api = NinjaAPI(urls_namespace=f"ninja_other_{suffix}")
    probes: list[tuple[str, str | None]] = []

    @api.get("/ok")
    def ok(request: Any) -> dict[str, bool]:
        return {"ok": True}

    @api.get("/async-ok")
    async def async_ok(request: Any) -> dict[str, bool]:
        return {"ok": True}

    @api.get("/items/{item_id}", response=ItemOut)
    def item(request: Any, item_id: int, quantity: int = 1) -> ItemOut:
        return ItemOut(item_id=item_id, name="item", quantity=quantity)

    @api.post("/items", response=ItemOut)
    def create_item(request: Any, item: ItemIn) -> ItemOut:
        return ItemOut(item_id=1, name=item.name, quantity=item.quantity)

    @api.get("/error")
    def unexpected(request: Any) -> None:
        raise RuntimeError("ninja unexpected failure")

    @api.get("/async-error")
    async def async_unexpected(request: Any) -> None:
        raise RuntimeError("ninja async unexpected failure")

    @api.get("/http-error")
    def http_error(request: Any) -> None:
        raise HttpError(404, "missing item")

    @api.get("/django-404")
    def django_404(request: Any) -> None:
        from django.http import Http404

        raise Http404("missing item")

    def reject_auth(request: Any) -> None:
        raise AuthenticationError()

    @api.get("/auth", auth=reject_auth)
    def auth_route(request: Any) -> dict[str, bool]:
        return {"unreachable": True}

    @api.get("/throttled", throttle=DenyThrottle())
    def throttled(request: Any) -> dict[str, bool]:
        return {"unreachable": True}

    @api.get("/custom-409")
    def custom_409(request: Any) -> None:
        raise Custom409("custom failure")

    @api.exception_handler(Custom409)
    def handle_409(request: Any, exc: Custom409) -> Any:
        return api.create_response(request, {"detail": str(exc)}, status=409)

    @api.get("/custom-503")
    def custom_503(request: Any) -> None:
        raise Custom503("custom server failure")

    @api.exception_handler(Custom503)
    def handle_503(request: Any, exc: Custom503) -> Any:
        return api.create_response(request, {"detail": str(exc)}, status=503)

    @api.get("/custom-raise")
    def custom_raise(request: Any) -> None:
        raise CustomRaise("original failure")

    @api.exception_handler(CustomRaise)
    def handle_raise(request: Any, exc: CustomRaise) -> Any:
        raise ValueError("handler replaced the error")

    @api.get("/schema-error", response=ItemOut)
    def schema_error(request: Any) -> dict[str, Any]:
        return {"item_id": "not-an-int", "name": "item", "quantity": 1}

    def marker(function: Any) -> Any:
        @wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return function(*args, **kwargs)

        return wrapper

    @api.get("/decorated")
    @marker
    def decorated(request: Any) -> dict[str, bool]:
        return {"decorated": True}

    @api.get("/stream/sync", response=JSONL[int])
    def stream_sync(request: Any) -> Any:
        probes.append(("sync-before", _current_span_name()))
        yield 1
        probes.append(("sync-after", _current_span_name()))
        yield 2

    @api.get("/stream/async", response=JSONL[int])
    async def stream_async(request: Any) -> Any:
        probes.append(("async-before", _current_span_name()))
        yield 1
        probes.append(("async-after", _current_span_name()))
        yield 2

    shared_router = Router()

    @shared_router.get("/shared")
    def shared(request: Any) -> dict[str, bool]:
        return {"shared": True}

    api.add_router("/r1", shared_router)
    api.add_router("/r2", shared_router, url_name_prefix="r2")

    nested_router = Router()

    @nested_router.get("/deep")
    def nested_deep(request: Any) -> dict[str, bool]:
        return {"deep": True}

    parent_router = Router()
    parent_router.add_router("/nested", nested_router)
    api.add_router("/parent", parent_router)

    @other_api.get("/ok")
    def other_ok(request: Any) -> dict[str, bool]:
        return {"other": True}

    return api, other_api, probes


def _install_urlconf(api: Any, other_api: Any) -> str:
    name = f"tests._ninja_urls_{uuid.uuid4().hex}"
    module = types.ModuleType(name)
    module.urlpatterns = [
        path("ninja/", api.urls),
        path("other/", other_api.urls),
    ]
    sys.modules[name] = module
    return name


def _config(providers: Any, **overrides: Any) -> Config:
    return Config(
        tracer_provider=providers.tracer_provider,
        meter_provider=providers.meter_provider,
        logger_provider=providers.logger_provider,
        **overrides,
    )


@pytest.fixture
def ninja_stack(
    telemetry_providers: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[NinjaHarness]:
    pytest.importorskip("ninja")
    _ninja.install()
    api, other_api, probes = _build_apis()
    urlconf = _install_urlconf(api, other_api)
    config = _config(telemetry_providers)
    monkeypatch.setattr(TelemetryMiddleware, "config", config)

    try:
        with override_settings(
            ROOT_URLCONF=urlconf,
            MIDDLEWARE=[_MIDDLEWARE],
            DEBUG=False,
        ):
            yield NinjaHarness(
                api=api,
                other_api=other_api,
                providers=telemetry_providers,
                config=config,
                stream_probes=probes,
            )
    finally:
        sys.modules.pop(urlconf, None)


def _clear(providers: Any) -> None:
    providers.span_exporter.clear()
    providers.log_exporter.clear()


def _spans(providers: Any) -> list[Any]:
    return list(providers.spans)


def _span(providers: Any, name: str) -> Any:
    return next(span for span in providers.spans if span.name == name)


def _server_span(providers: Any) -> Any:
    return next(span for span in providers.spans if span.kind is SpanKind.SERVER)


def _records(providers: Any) -> list[Any]:
    return [record.log_record for record in providers.logs]


def _records_for(providers: Any, event_name: str) -> list[Any]:
    return [record for record in _records(providers) if record.event_name == event_name]


def _span_names(providers: Any) -> list[str]:
    return [span.name for span in providers.spans]


def _operation(api: Any, path: str) -> Any:
    for bound_router in api._get_bound_routers():
        path_view = bound_router.path_operations.get(path)
        if path_view is not None:
            return path_view.operations[0]
    raise AssertionError(f"operation {path!r} not found")


def _assert_operation_error(providers: Any, span_name: str, type_fragment: str) -> None:
    span = _span(providers, span_name)
    assert span.status.status_code == StatusCode.ERROR
    assert type_fragment in str(span.attributes.get("error.type"))


def _assert_operation_ok(span: Any) -> None:
    assert span.status.status_code == StatusCode.UNSET
    assert "error.type" not in span.attributes


def test_sync_success_spans_and_no_logs(ninja_stack: NinjaHarness) -> None:
    _clear(ninja_stack.providers)
    response = Client().get("/ninja/ok")

    assert response.status_code == 200
    assert _span_names(ninja_stack.providers).count(_INPUT_SPAN) == 1
    assert _span_names(ninja_stack.providers).count(_ENDPOINT_SPAN) == 1
    assert _span_names(ninja_stack.providers).count(_SERIALIZATION_SPAN) == 1
    assert _records(ninja_stack.providers) == []

    endpoint = _span(ninja_stack.providers, _ENDPOINT_SPAN)
    assert endpoint.attributes["code.function.name"].endswith(".ok")
    assert (
        endpoint.context.trace_id
        == _server_span(ninja_stack.providers).context.trace_id
    )


async def test_async_success_spans(ninja_stack: NinjaHarness) -> None:
    _clear(ninja_stack.providers)
    response = await AsyncClient().get("/ninja/async-ok")

    assert response.status_code == 200
    assert _span_names(ninja_stack.providers).count(_ENDPOINT_SPAN) == 1
    assert _span_names(ninja_stack.providers).count(_SERIALIZATION_SPAN) == 1
    assert _records(ninja_stack.providers) == []
    assert inspect.iscoroutinefunction(
        _operation(ninja_stack.api, "/async-ok").view_func
    )


def test_path_validation_is_one_warn_without_input(ninja_stack: NinjaHarness) -> None:
    _clear(ninja_stack.providers)
    response = Client().get("/ninja/items/not-an-int")

    assert response.status_code == 422
    records = _records(ninja_stack.providers)
    assert len(records) == 1
    record = records[0]
    assert record.event_name == _VALIDATION_EVENT
    assert record.severity_number is SeverityNumber.WARN
    assert record.severity_text == "WARN"
    assert record.body == "Request validation failed"
    assert record.attributes["django_ninja.validation.error_count"] == 1
    assert "not-an-int" not in str(record.attributes)
    assert "not-an-int" not in str(record.body)
    assert _records_for(ninja_stack.providers, _EXCEPTION_EVENT) == []

    assert _span_names(ninja_stack.providers).count(_ENDPOINT_SPAN) == 0
    _assert_operation_ok(_span(ninja_stack.providers, _INPUT_SPAN))
    _assert_operation_ok(_server_span(ninja_stack.providers))


def test_body_validation_is_one_warn(ninja_stack: NinjaHarness) -> None:
    _clear(ninja_stack.providers)
    response = Client().post(
        "/ninja/items",
        data={"name": "widget", "quantity": 0},
        content_type="application/json",
    )

    assert response.status_code == 422
    records = _records(ninja_stack.providers)
    assert len(records) == 1
    assert records[0].event_name == _VALIDATION_EVENT
    assert records[0].attributes["django_ninja.validation.error_count"] == 1
    assert "widget" not in str(records[0].attributes)
    assert "widget" not in str(records[0].body)


@pytest.mark.parametrize("debug", [False, True])
def test_unexpected_error_logs_once_and_marks_endpoint(
    ninja_stack: NinjaHarness, debug: bool
) -> None:
    _clear(ninja_stack.providers)
    with override_settings(DEBUG=debug):
        response = Client(raise_request_exception=False).get("/ninja/error")

    assert response.status_code == 500
    records = _records(ninja_stack.providers)
    assert len(records) == 1
    assert records[0].event_name == _EXCEPTION_EVENT
    assert records[0].severity_number is SeverityNumber.ERROR
    assert records[0].severity_text == "ERROR"
    assert "RuntimeError" in str(records[0].attributes.get("exception.type"))
    assert _records_for(ninja_stack.providers, _VALIDATION_EVENT) == []

    _assert_operation_error(ninja_stack.providers, _ENDPOINT_SPAN, "RuntimeError")
    _assert_operation_error(
        ninja_stack.providers, _server_span(ninja_stack.providers).name, "RuntimeError"
    )


async def test_async_unexpected_error_marks_endpoint(ninja_stack: NinjaHarness) -> None:
    _clear(ninja_stack.providers)
    response = await AsyncClient(raise_request_exception=False).get(
        "/ninja/async-error"
    )

    assert response.status_code == 500
    assert len(_records(ninja_stack.providers)) == 1
    _assert_operation_error(ninja_stack.providers, _ENDPOINT_SPAN, "RuntimeError")


@pytest.mark.parametrize(
    ("path", "expected_status"),
    [
        ("/ninja/http-error", 404),
        ("/ninja/django-404", 404),
        ("/ninja/auth", 401),
        ("/ninja/throttled", 429),
    ],
)
def test_expected_http_errors_do_not_log(
    ninja_stack: NinjaHarness, path: str, expected_status: int
) -> None:
    _clear(ninja_stack.providers)
    response = Client().get(path)

    assert response.status_code == expected_status
    assert _records(ninja_stack.providers) == []
    assert _server_span(ninja_stack.providers).status.status_code == StatusCode.UNSET


def test_expected_http_error_does_not_mark_endpoint(ninja_stack: NinjaHarness) -> None:
    _clear(ninja_stack.providers)
    response = Client().get("/ninja/http-error")

    assert response.status_code == 404
    _assert_operation_ok(_span(ninja_stack.providers, _ENDPOINT_SPAN))


def test_custom_handler_below_500_does_not_log(ninja_stack: NinjaHarness) -> None:
    _clear(ninja_stack.providers)
    response = Client().get("/ninja/custom-409")

    assert response.status_code == 409
    assert _records(ninja_stack.providers) == []
    assert _server_span(ninja_stack.providers).status.status_code == StatusCode.UNSET
    # The endpoint raised a non-HTTP application exception, so the operation
    # span is marked even though the custom handler kept the response below 500
    # and on_exception did not log it.
    _assert_operation_error(ninja_stack.providers, _ENDPOINT_SPAN, "Custom409")


def test_custom_handler_5xx_logs_original_exception(ninja_stack: NinjaHarness) -> None:
    _clear(ninja_stack.providers)
    response = Client().get("/ninja/custom-503")

    assert response.status_code == 503
    records = _records(ninja_stack.providers)
    assert len(records) == 1
    assert records[0].event_name == _EXCEPTION_EVENT
    assert "Custom503" in str(records[0].attributes.get("exception.type"))
    assert _server_span(ninja_stack.providers).status.status_code == StatusCode.ERROR
    _assert_operation_error(ninja_stack.providers, _ENDPOINT_SPAN, "Custom503")


def test_handler_raising_new_exception_logs_new_object(
    ninja_stack: NinjaHarness,
) -> None:
    _clear(ninja_stack.providers)
    response = Client(raise_request_exception=False).get("/ninja/custom-raise")

    assert response.status_code == 500
    records = _records(ninja_stack.providers)
    assert len(records) == 1
    assert "ValueError" in str(records[0].attributes.get("exception.type"))
    assert "CustomRaise" not in str(records[0].attributes.get("exception.type"))
    _assert_operation_error(ninja_stack.providers, _ENDPOINT_SPAN, "CustomRaise")
    _assert_operation_error(
        ninja_stack.providers, _server_span(ninja_stack.providers).name, "ValueError"
    )


@pytest.mark.parametrize("debug", [False, True])
def test_response_schema_failure_is_error_not_validation(
    ninja_stack: NinjaHarness, debug: bool
) -> None:
    _clear(ninja_stack.providers)
    with override_settings(DEBUG=debug):
        response = Client(raise_request_exception=False).get("/ninja/schema-error")

    assert response.status_code == 500
    records = _records(ninja_stack.providers)
    assert len(records) == 1
    assert records[0].event_name == _EXCEPTION_EVENT
    assert records[0].severity_number is SeverityNumber.ERROR
    assert _records_for(ninja_stack.providers, _VALIDATION_EVENT) == []
    assert "ValidationError" in str(records[0].attributes.get("exception.type"))

    _assert_operation_error(
        ninja_stack.providers, _SERIALIZATION_SPAN, "ValidationError"
    )
    _assert_operation_ok(_span(ninja_stack.providers, _INPUT_SPAN))
    _assert_operation_ok(_span(ninja_stack.providers, _ENDPOINT_SPAN))


def test_sampling_does_not_suppress_error_or_validation_logs(
    ninja_stack: NinjaHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider(sampler=ALWAYS_OFF)
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    config = Config(
        tracer_provider=tracer_provider,
        meter_provider=ninja_stack.providers.meter_provider,
        logger_provider=ninja_stack.providers.logger_provider,
    )
    monkeypatch.setattr(TelemetryMiddleware, "config", config)
    _clear(ninja_stack.providers)

    try:
        response = Client(raise_request_exception=False).get("/ninja/error")
        assert response.status_code == 500
        assert len(_records_for(ninja_stack.providers, _EXCEPTION_EVENT)) == 1

        _clear(ninja_stack.providers)
        response = Client().get("/ninja/items/not-an-int")
        assert response.status_code == 422
        assert len(_records_for(ninja_stack.providers, _VALIDATION_EVENT)) == 1
        assert span_exporter.get_finished_spans() == ()
    finally:
        tracer_provider.shutdown()


def test_operation_spans_disabled(
    ninja_stack: NinjaHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(ninja_stack.providers, operation_spans=False)
    monkeypatch.setattr(TelemetryMiddleware, "config", config)
    _clear(ninja_stack.providers)

    response = Client().get("/ninja/ok")

    assert response.status_code == 200
    assert _span_names(ninja_stack.providers) == [
        _server_span(ninja_stack.providers).name
    ]


def test_tracing_disabled_keeps_validation_log(
    ninja_stack: NinjaHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(ninja_stack.providers, tracing=False)
    monkeypatch.setattr(TelemetryMiddleware, "config", config)
    _clear(ninja_stack.providers)

    response = Client().get("/ninja/items/not-an-int")

    assert response.status_code == 422
    assert _spans(ninja_stack.providers) == []
    assert len(_records_for(ninja_stack.providers, _VALIDATION_EVENT)) == 1


def test_logs_disabled_keeps_spans(
    ninja_stack: NinjaHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(ninja_stack.providers, logs=False)
    monkeypatch.setattr(TelemetryMiddleware, "config", config)
    _clear(ninja_stack.providers)

    response = Client(raise_request_exception=False).get("/ninja/error")

    assert response.status_code == 500
    assert _records(ninja_stack.providers) == []
    _assert_operation_error(ninja_stack.providers, _ENDPOINT_SPAN, "RuntimeError")
    assert _server_span(ninja_stack.providers).status.status_code == StatusCode.ERROR


def test_explicit_none_state_skips_hooks_without_ambient_inference(
    ninja_stack: NinjaHarness,
) -> None:
    from django.test import RequestFactory

    _clear(ninja_stack.providers)
    request = RequestFactory().get("/ninja/ok")
    setattr(request, REQUEST_STATE_ATTRIBUTE, None)
    operation = _operation(ninja_stack.api, "/ok")
    tracer = ninja_stack.providers.tracer_provider.get_tracer(__name__)

    with tracer.start_as_current_span("ambient"):
        assert operation.view_func(request) == {"ok": True}

    assert _span_names(ninja_stack.providers) == ["ambient"]
    assert _records(ninja_stack.providers) == []

    with pytest.raises(RuntimeError):
        with tracer.start_as_current_span("ambient-exception"):
            ninja_stack.api.on_exception(request, RuntimeError("ambient"))
    assert _records(ninja_stack.providers) == []


def test_decorated_endpoint_preserves_signature_and_is_wrapped_once(
    ninja_stack: NinjaHarness,
) -> None:
    _clear(ninja_stack.providers)
    response = Client().get("/ninja/decorated")

    assert response.status_code == 200
    assert response.json() == {"decorated": True}
    endpoint = _span(ninja_stack.providers, _ENDPOINT_SPAN)
    assert endpoint.attributes["code.function.name"].endswith(".decorated")

    operation = _operation(ninja_stack.api, "/decorated")
    wrapped = operation.view_func
    assert inspect.signature(wrapped) == inspect.signature(wrapped.__wrapped__)
    _ = ninja_stack.api.urls
    _ = ninja_stack.api.urls
    assert operation.view_func is wrapped
    assert wrapped.__wrapped__.__name__ == "decorated"


def test_reused_nested_routers_and_multiple_apis(ninja_stack: NinjaHarness) -> None:
    for url_path in (
        "/ninja/r1/shared",
        "/ninja/r2/shared",
        "/ninja/parent/nested/deep",
        "/other/ok",
    ):
        _clear(ninja_stack.providers)
        response = Client().get(url_path)
        assert response.status_code == 200, url_path
        assert _span_names(ninja_stack.providers).count(_ENDPOINT_SPAN) == 1, url_path
        assert _span_names(ninja_stack.providers).count(_INPUT_SPAN) == 1, url_path
        assert _span_names(ninja_stack.providers).count(_SERIALIZATION_SPAN) == 1, (
            url_path
        )

    shared_wrappers = []
    for bound_router in ninja_stack.api._get_bound_routers():
        path_view = bound_router.path_operations.get("/shared")
        if path_view is not None:
            shared_wrappers.append(path_view.operations[0].view_func)
    assert len(shared_wrappers) == 2
    assert shared_wrappers[0] is shared_wrappers[1]


def test_sync_stream_ends_endpoint_span_at_creation(
    ninja_stack: NinjaHarness,
) -> None:
    _clear(ninja_stack.providers)
    response = Client().get("/ninja/stream/sync")

    assert response.status_code == 200
    assert _span_names(ninja_stack.providers).count(_ENDPOINT_SPAN) == 1
    assert _SERIALIZATION_SPAN not in _span_names(ninja_stack.providers)
    assert ninja_stack.stream_probes == []

    assert list(response.streaming_content) == [b"1\n", b"2\n"]
    assert ninja_stack.stream_probes == [
        ("sync-before", None),
        ("sync-after", None),
    ]
    assert _span_names(ninja_stack.providers).count(_ENDPOINT_SPAN) == 1
    assert (
        inspect.iscoroutinefunction(
            _operation(ninja_stack.api, "/stream/sync").view_func
        )
        is False
    )


async def test_async_stream_ends_endpoint_span_at_creation(
    ninja_stack: NinjaHarness,
) -> None:
    _clear(ninja_stack.providers)
    response = await AsyncClient().get("/ninja/stream/async")

    assert response.status_code == 200
    assert _span_names(ninja_stack.providers).count(_ENDPOINT_SPAN) == 1
    assert _SERIALIZATION_SPAN not in _span_names(ninja_stack.providers)
    assert ninja_stack.stream_probes == []

    chunks = [chunk async for chunk in response.streaming_content]
    assert chunks == [b"1\n", b"2\n"]
    assert ninja_stack.stream_probes == [
        ("async-before", None),
        ("async-after", None),
    ]
    assert _span_names(ninja_stack.providers).count(_ENDPOINT_SPAN) == 1
    assert (
        inspect.iscoroutinefunction(
            _operation(ninja_stack.api, "/stream/async").view_func
        )
        is False
    )


def test_install_is_idempotent_and_does_not_multiply_wrappers(
    ninja_stack: NinjaHarness,
) -> None:
    from ninja.main import NinjaAPI
    from ninja.operation import Operation, PathView

    before = (
        NinjaAPI.on_exception,
        Operation._get_values,
        Operation._result_to_response,
        PathView.get_view,
    )
    _ninja.install()
    _ninja.install()
    after = (
        NinjaAPI.on_exception,
        Operation._get_values,
        Operation._result_to_response,
        PathView.get_view,
    )
    assert after == before

    path_view = next(
        bound.path_operations["/ok"]
        for bound in ninja_stack.api._get_bound_routers()
        if "/ok" in bound.path_operations
    )
    operation = path_view.operations[0]
    wrapped = operation.view_func
    _ = ninja_stack.api.urls
    cloned = path_view.clone()
    cloned.get_view()
    assert operation.view_func is wrapped
    assert cloned.operations[0].view_func is wrapped
    assert wrapped.__wrapped__.__name__ == "ok"


def _run_script(script: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "tests.project.settings",
        "PYTHONUNBUFFERED": "1",
    }
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
        check=False,
    )


@pytest.mark.parametrize("installed_version", ["1.6.9", "1.8.0"])
def test_unsupported_version_warns_once_without_patching(
    installed_version: str,
) -> None:
    result = _run_script(
        f"""
        import logging

        import django

        django.setup()

        from django_ninja_opentelemetry import _ninja

        messages = []

        class Handler(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        _ninja._logger.addHandler(Handler())
        _ninja.version = lambda name: {installed_version!r}
        _ninja.install()
        _ninja.install()

        from ninja.main import NinjaAPI
        from ninja.operation import Operation, PathView

        mark = "_django_ninja_otel_patched"
        print(len(messages))
        print(
            hasattr(NinjaAPI.on_exception, mark),
            hasattr(Operation._get_values, mark),
            hasattr(Operation._result_to_response, mark),
            hasattr(PathView.get_view, mark),
        )
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["1", "False False False False"]


def test_bad_hook_signature_warns_once_without_partial_patching() -> None:
    result = _run_script(
        """
        import logging

        import django

        django.setup()

        from ninja.operation import PathView

        def unexpected_signature(self, request):
            pass

        PathView.get_view = unexpected_signature

        from django_ninja_opentelemetry import _ninja

        messages = []

        class Handler(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        _ninja._logger.addHandler(Handler())
        _ninja.version = lambda name: "1.7.1"
        _ninja.install()
        _ninja.install()

        from ninja.main import NinjaAPI
        from ninja.operation import Operation

        mark = "_django_ninja_otel_patched"
        print(len(messages))
        print(
            hasattr(NinjaAPI.on_exception, mark),
            hasattr(Operation._get_values, mark),
            hasattr(Operation._result_to_response, mark),
            hasattr(PathView.get_view, mark),
        )
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["1", "False False False False"]


def test_missing_hook_warns_once_without_partial_patching() -> None:
    result = _run_script(
        """
        import logging

        import django

        django.setup()

        from ninja.operation import Operation

        del Operation._result_to_response

        from django_ninja_opentelemetry import _ninja

        messages = []

        class Handler(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        _ninja._logger.addHandler(Handler())
        _ninja.version = lambda name: "1.7.1"
        _ninja.install()
        _ninja.install()

        from ninja.main import NinjaAPI
        from ninja.operation import PathView

        mark = "_django_ninja_otel_patched"
        print(len(messages))
        print(
            hasattr(NinjaAPI.on_exception, mark),
            hasattr(Operation._get_values, mark),
            hasattr(Operation, "_result_to_response"),
            hasattr(PathView.get_view, mark),
        )
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["1", "False False False False"]


def test_missing_ninja_is_silent_and_import_is_safe() -> None:
    result = _run_script(
        """
        import logging
        import sys

        import django

        django.setup()

        class BlockNinja:
            def find_spec(self, name, path=None, target=None):
                if name == "ninja" or name.startswith("ninja."):
                    raise ModuleNotFoundError(name)
                return None

        sys.meta_path.insert(0, BlockNinja())

        from importlib.metadata import PackageNotFoundError

        from django_ninja_opentelemetry import _ninja

        messages = []

        class Handler(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        _ninja._logger.addHandler(Handler())

        def missing(name):
            raise PackageNotFoundError(name)

        _ninja.version = missing
        _ninja.install()
        _ninja.install()
        print(len(messages))
        print("ok")
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["0", "ok"]
