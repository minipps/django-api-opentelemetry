"""Conformance tests for the optional Django REST framework integration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from django.test import AsyncClient, Client, RequestFactory, override_settings
from opentelemetry._logs import SeverityNumber
from opentelemetry.trace import SpanKind, StatusCode

from django_api_opentelemetry._telemetry import REQUEST_STATE_ATTRIBUTE

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.conftest import TelemetryProviders

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.project.settings")

_MIDDLEWARE = "django_api_opentelemetry.middleware.TelemetryMiddleware"
_URLCONF = "tests.project.drf_urls"

_DISPATCH = "django_drf.dispatch"
_INITIAL = "django_drf.initial"
_RENDER = "django_drf.render"
_VALIDATION_EVENT = "django_drf.validation.failed"
_EXCEPTION_EVENT = "http.server.request.exception"
_VALIDATION_COUNT = "django_drf.validation.error_count"

_DRF_SETTINGS = {
    # No database or auth tables are available, and the tests do not rely on
    # Django's AnonymousUser being constructed from the auth app.
    "UNAUTHENTICATED_USER": None,
    "EXCEPTION_HANDLER": "tests.project.drf_urls.test_exception_handler",
}


@dataclass(frozen=True)
class DrfHarness:
    providers: TelemetryProviders
    urls: Any


def _telemetry_settings(
    providers: TelemetryProviders, **overrides: Any
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "tracer_provider": providers.tracer_provider,
        "meter_provider": providers.meter_provider,
        "logger_provider": providers.logger_provider,
    }
    values.update(overrides)
    return values


@pytest.fixture
def drf_stack(telemetry_providers: TelemetryProviders) -> Iterator[DrfHarness]:
    pytest.importorskip(
        "rest_framework",
        reason="Django REST framework is an optional integration dependency",
    )

    from rest_framework.views import APIView

    from django_api_opentelemetry import _drf

    # An earlier test may have deliberately left installation unpatched while
    # restoring the module's one-shot flag. Recover only for the real hooks.
    if not getattr(APIView.dispatch, "_django_drf_otel_patched", False):
        _drf._install_attempted = False
        _drf._warned.clear()
        _drf.install()

    from tests.project import drf_urls

    drf_urls.PROBES.clear()
    with override_settings(
        ROOT_URLCONF=_URLCONF,
        MIDDLEWARE=[_MIDDLEWARE],
        DEBUG=False,
        REST_FRAMEWORK=dict(_DRF_SETTINGS),
        DJANGO_NINJA_OTEL=_telemetry_settings(telemetry_providers),
    ):
        yield DrfHarness(providers=telemetry_providers, urls=drf_urls)


def _clear(providers: TelemetryProviders) -> None:
    providers.span_exporter.clear()
    providers.log_exporter.clear()


def _spans(providers: TelemetryProviders, name: str | None = None) -> list[Any]:
    spans = list(providers.spans)
    if name is None:
        return spans
    return [span for span in spans if span.name == name]


def _span(providers: TelemetryProviders, name: str) -> Any:
    return next(span for span in providers.spans if span.name == name)


def _server_span(providers: TelemetryProviders) -> Any:
    return next(span for span in providers.spans if span.kind is SpanKind.SERVER)


def _records(providers: TelemetryProviders) -> list[Any]:
    return [record.log_record for record in providers.logs]


def _records_for(providers: TelemetryProviders, event_name: str) -> list[Any]:
    return [record for record in _records(providers) if record.event_name == event_name]


def _assert_operation_ok(providers: TelemetryProviders, *names: str) -> None:
    for name in names:
        span = _span(providers, name)
        assert span.status.status_code is StatusCode.UNSET, name
        assert "error.type" not in span.attributes, name


def _assert_one_exception(providers: TelemetryProviders, type_fragment: str) -> Any:
    records = _records_for(providers, _EXCEPTION_EVENT)
    assert len(records) == 1
    record = records[0]
    assert record.severity_number is SeverityNumber.ERROR
    assert record.severity_text == "ERROR"
    assert type_fragment in str(record.attributes.get("exception.type"))
    return record


def _post_json(path: str, payload: dict[str, Any]) -> Any:
    return Client(raise_request_exception=False).post(
        path,
        data=json.dumps(payload),
        content_type="application/json",
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/drf/api-view/", {"family": "api-view"}),
        (
            "/drf/generic/list/",
            [
                {"item_id": 1, "name": "one"},
                {"item_id": 2, "name": "two"},
            ],
        ),
        ("/drf/generic/detail/7/", {"item_id": 7, "name": "item-7"}),
        ("/drf/viewset/things/", {"things": [1, 2]}),
        ("/drf/function/", {"family": "api-view-function"}),
    ],
)
def test_view_families_keep_responses_and_operation_spans(
    drf_stack: DrfHarness, path: str, expected: Any
) -> None:
    _clear(drf_stack.providers)

    response = Client().get(path)

    assert response.status_code == 200
    assert response.json() == expected
    assert response.headers["Content-Type"] == "application/json"
    assert _records(drf_stack.providers) == []
    assert len(_spans(drf_stack.providers, _DISPATCH)) == 1
    assert len(_spans(drf_stack.providers, _INITIAL)) == 1
    assert len(_spans(drf_stack.providers, _RENDER)) == 1
    _assert_operation_ok(drf_stack.providers, _DISPATCH, _INITIAL, _RENDER)

    server = _server_span(drf_stack.providers)
    assert server.status.status_code is StatusCode.UNSET
    assert server.attributes["http.response.status_code"] == 200


def test_router_create_stays_a_single_dispatch(drf_stack: DrfHarness) -> None:
    _clear(drf_stack.providers)

    response = Client().post(
        "/drf/viewset/things/",
        data=json.dumps({"name": "created-thing"}),
        content_type="application/json",
    )

    assert response.status_code == 201
    assert response.json() == {"created": "created-thing"}
    assert len(_spans(drf_stack.providers, _DISPATCH)) == 1
    assert len(_spans(drf_stack.providers, _INITIAL)) == 1
    assert len(_spans(drf_stack.providers, _RENDER)) == 1
    assert _records(drf_stack.providers) == []


def test_operation_parentage_and_route_on_wsgi(drf_stack: DrfHarness) -> None:
    _clear(drf_stack.providers)

    response = Client().get("/drf/api-view/")

    assert response.status_code == 200
    server = _server_span(drf_stack.providers)
    dispatch = _span(drf_stack.providers, _DISPATCH)
    initial = _span(drf_stack.providers, _INITIAL)
    render = _span(drf_stack.providers, _RENDER)

    assert server.name == "GET drf/api-view/"
    assert server.attributes["http.route"] == "drf/api-view/"
    assert dispatch.context.trace_id == server.context.trace_id
    assert initial.context.trace_id == server.context.trace_id
    assert render.context.trace_id == server.context.trace_id

    assert dispatch.parent is not None
    assert dispatch.parent.span_id == server.context.span_id
    assert initial.parent is not None
    assert initial.parent.span_id == dispatch.context.span_id
    # Deferred rendering happens after dispatch has finished, but still under
    # the HTTP request span.
    assert render.parent is not None
    assert render.parent.span_id == server.context.span_id
    assert render.context.span_id != dispatch.context.span_id


async def test_operation_parentage_and_route_on_asgi(drf_stack: DrfHarness) -> None:
    _clear(drf_stack.providers)

    response = await AsyncClient().get("/drf/generic/detail/3/")

    assert response.status_code == 200
    assert response.json() == {"item_id": 3, "name": "item-3"}
    server = _server_span(drf_stack.providers)
    dispatch = _span(drf_stack.providers, _DISPATCH)
    initial = _span(drf_stack.providers, _INITIAL)
    render = _span(drf_stack.providers, _RENDER)

    assert server.name == "GET drf/generic/detail/<int:pk>/"
    assert dispatch.parent is not None
    assert dispatch.parent.span_id == server.context.span_id
    assert initial.parent is not None
    assert initial.parent.span_id == dispatch.context.span_id
    assert render.parent is not None
    assert render.parent.span_id == server.context.span_id
    assert _records(drf_stack.providers) == []


def test_initial_wraps_authentication_permission_and_throttling(
    drf_stack: DrfHarness,
) -> None:
    cases = [
        ("/drf/expected/auth/", 401, "authenticate"),
        ("/drf/expected/permission/", 403, "permission"),
        ("/drf/expected/throttled/", 429, "throttle"),
    ]
    for path, status_code, label in cases:
        _clear(drf_stack.providers)
        drf_stack.urls.PROBES.clear()

        response = Client().get(path)

        assert response.status_code == status_code, path
        assert drf_stack.urls.PROBES == [
            (label, _INITIAL, _probe_span_id(drf_stack, _INITIAL))
        ]
        _assert_operation_ok(drf_stack.providers, _DISPATCH, _INITIAL, _RENDER)
        assert _server_span(drf_stack.providers).status.status_code is StatusCode.UNSET
        assert _records(drf_stack.providers) == []


def _probe_span_id(drf_stack: DrfHarness, name: str) -> int:
    return _span(drf_stack.providers, name).context.span_id


def test_dispatch_wraps_handler_and_serializer_work(drf_stack: DrfHarness) -> None:
    _clear(drf_stack.providers)
    drf_stack.urls.PROBES.clear()

    response = Client().get("/drf/api-view/")

    assert response.status_code == 200
    assert drf_stack.urls.PROBES == [
        ("handler", _DISPATCH, _probe_span_id(drf_stack, _DISPATCH))
    ]

    _clear(drf_stack.providers)
    drf_stack.urls.PROBES.clear()
    response = _post_json(
        "/drf/validation/nested/",
        {"item": {"name": "widget", "private_code": 2}, "tags": [2]},
    )

    assert response.status_code == 201
    assert drf_stack.urls.PROBES == [
        ("serializer", _DISPATCH, _probe_span_id(drf_stack, _DISPATCH))
    ]
    assert _records(drf_stack.providers) == []


@pytest.mark.parametrize(
    ("path", "status_code"),
    [
        ("/drf/expected/not-found/", 404),
        ("/drf/expected/permission/", 403),
        ("/drf/expected/auth/", 401),
        ("/drf/expected/throttled/", 429),
        ("/drf/expected/api-409/", 409),
    ],
)
def test_expected_4xx_does_not_log_or_mark_operation_error(
    drf_stack: DrfHarness, path: str, status_code: int
) -> None:
    _clear(drf_stack.providers)

    response = Client().get(path)

    assert response.status_code == status_code
    assert _records(drf_stack.providers) == []
    assert _server_span(drf_stack.providers).status.status_code is StatusCode.UNSET
    _assert_operation_ok(drf_stack.providers, _DISPATCH, _INITIAL, _RENDER)


def test_method_not_allowed_is_an_expected_4xx(drf_stack: DrfHarness) -> None:
    _clear(drf_stack.providers)

    response = Client().post("/drf/api-view/")

    assert response.status_code == 405
    assert _records(drf_stack.providers) == []
    assert _server_span(drf_stack.providers).status.status_code is StatusCode.UNSET
    _assert_operation_ok(drf_stack.providers, _DISPATCH, _INITIAL, _RENDER)


def test_nested_serializer_validation_is_one_private_warn(
    drf_stack: DrfHarness,
) -> None:
    _clear(drf_stack.providers)

    response = _post_json(
        "/drf/validation/nested/",
        {"item": {"name": "widget", "private_code": 0}, "tags": [0]},
    )

    assert response.status_code == 400
    # The application response is unchanged and still contains the details.
    assert "item" in response.json()
    assert "tags" in response.json()

    records = _records(drf_stack.providers)
    assert len(records) == 1
    record = records[0]
    assert record.event_name == _VALIDATION_EVENT
    assert record.severity_number is SeverityNumber.WARN
    assert record.severity_text == "WARN"
    assert record.body == "Request validation failed"
    assert record.attributes == {
        _VALIDATION_COUNT: 2,
        "http.route": "drf/validation/nested/",
    }
    rendered_record = str(record.attributes) + str(record.body)
    assert "private_code" not in rendered_record
    assert "tags" not in rendered_record
    assert "widget" not in rendered_record
    assert "Ensure this value" not in rendered_record
    assert _records_for(drf_stack.providers, _EXCEPTION_EVENT) == []

    assert _server_span(drf_stack.providers).status.status_code is StatusCode.UNSET
    _assert_operation_ok(drf_stack.providers, _DISPATCH, _INITIAL, _RENDER)


def test_manual_nested_validation_counts_scalar_leaves_only(
    drf_stack: DrfHarness,
) -> None:
    _clear(drf_stack.providers)

    response = _post_json("/drf/validation/manual/", {})

    assert response.status_code == 400
    assert "secret-value-one" in response.text

    [record] = _records(drf_stack.providers)
    assert record.event_name == _VALIDATION_EVENT
    assert record.attributes == {
        _VALIDATION_COUNT: 3,
        "http.route": "drf/validation/manual/",
    }
    rendered_record = str(record.attributes) + str(record.body)
    assert "private_field" not in rendered_record
    assert "other_field" not in rendered_record
    assert "secret-value-one" not in rendered_record
    assert "secret-value-two" not in rendered_record
    assert "secret-value-three" not in rendered_record
    _assert_operation_ok(drf_stack.providers, _DISPATCH, _INITIAL, _RENDER)


def test_non_raising_validation_followed_by_400_is_not_detected(
    drf_stack: DrfHarness,
) -> None:
    _clear(drf_stack.providers)

    response = _post_json(
        "/drf/validation/non-raising/",
        {"item": {"name": "widget", "private_code": 0}, "tags": [0]},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid"
    assert _records(drf_stack.providers) == []
    assert _server_span(drf_stack.providers).status.status_code is StatusCode.UNSET
    _assert_operation_ok(drf_stack.providers, _DISPATCH, _INITIAL, _RENDER)


def test_unexpected_exception_logs_once_and_marks_dispatch_and_http(
    drf_stack: DrfHarness,
) -> None:
    _clear(drf_stack.providers)

    response = Client(raise_request_exception=False).get("/drf/unexpected/")

    assert response.status_code == 500
    _assert_one_exception(drf_stack.providers, "RuntimeError")
    assert len(_records(drf_stack.providers)) == 1

    dispatch = _span(drf_stack.providers, _DISPATCH)
    assert dispatch.status.status_code is StatusCode.ERROR
    assert "RuntimeError" in str(dispatch.attributes.get("error.type"))
    _assert_operation_ok(drf_stack.providers, _INITIAL)
    server = _server_span(drf_stack.providers)
    assert server.status.status_code is StatusCode.ERROR
    assert server.attributes["http.response.status_code"] == 500
    assert "RuntimeError" in str(server.attributes.get("error.type"))


def test_custom_handler_for_4xx_does_not_log(drf_stack: DrfHarness) -> None:
    _clear(drf_stack.providers)

    response = Client().get("/drf/custom/409/")

    assert response.status_code == 409
    assert response.json() == {"detail": "converted client"}
    assert _records(drf_stack.providers) == []
    assert _server_span(drf_stack.providers).status.status_code is StatusCode.UNSET
    _assert_operation_ok(drf_stack.providers, _DISPATCH, _INITIAL, _RENDER)


def test_custom_handler_for_5xx_logs_original_exception_once(
    drf_stack: DrfHarness,
) -> None:
    _clear(drf_stack.providers)

    response = Client(raise_request_exception=False).get("/drf/custom/503/")

    assert response.status_code == 503
    assert response.json() == {"detail": "converted server"}
    _assert_one_exception(drf_stack.providers, "ConvertedServerError")
    assert len(_records(drf_stack.providers)) == 1

    dispatch = _span(drf_stack.providers, _DISPATCH)
    assert dispatch.status.status_code is StatusCode.ERROR
    _assert_operation_ok(drf_stack.providers, _INITIAL)
    server = _server_span(drf_stack.providers)
    assert server.status.status_code is StatusCode.ERROR
    assert "ConvertedServerError" in str(server.attributes.get("error.type"))


def test_failing_custom_handler_logs_the_new_exception_once(
    drf_stack: DrfHarness,
) -> None:
    _clear(drf_stack.providers)

    response = Client(raise_request_exception=False).get("/drf/custom/raise/")

    assert response.status_code == 500
    record = _assert_one_exception(drf_stack.providers, "ValueError")
    assert "HandlerFailure" not in str(record.attributes.get("exception.type"))
    assert len(_records(drf_stack.providers)) == 1

    dispatch = _span(drf_stack.providers, _DISPATCH)
    assert dispatch.status.status_code is StatusCode.ERROR
    assert "ValueError" in str(dispatch.attributes.get("error.type"))
    _assert_operation_ok(drf_stack.providers, _INITIAL)


def test_api_exception_with_5xx_status_is_an_error(drf_stack: DrfHarness) -> None:
    _clear(drf_stack.providers)

    response = Client(raise_request_exception=False).get("/drf/expected/api-503/")

    assert response.status_code == 503
    _assert_one_exception(drf_stack.providers, "CustomServerAPIException")
    assert len(_records(drf_stack.providers)) == 1

    dispatch = _span(drf_stack.providers, _DISPATCH)
    assert dispatch.status.status_code is StatusCode.ERROR
    _assert_operation_ok(drf_stack.providers, _INITIAL)
    assert _server_span(drf_stack.providers).status.status_code is StatusCode.ERROR


def test_render_failure_is_logged_once_and_marks_render_span(
    drf_stack: DrfHarness,
) -> None:
    _clear(drf_stack.providers)
    drf_stack.urls.PROBES.clear()

    response = Client(raise_request_exception=False).get("/drf/render/failure/")

    assert response.status_code == 500
    _assert_one_exception(drf_stack.providers, "RuntimeError")
    assert len(_records(drf_stack.providers)) == 1

    render = _span(drf_stack.providers, _RENDER)
    assert render.status.status_code is StatusCode.ERROR
    assert "RuntimeError" in str(render.attributes.get("error.type"))
    # The view returned a normal 200 response; failure happened after dispatch.
    _assert_operation_ok(drf_stack.providers, _DISPATCH, _INITIAL)
    server = _server_span(drf_stack.providers)
    assert server.status.status_code is StatusCode.ERROR
    assert server.attributes["http.response.status_code"] == 500

    assert drf_stack.urls.PROBES == [
        ("render-failure", _RENDER, render.context.span_id)
    ]


def test_already_rendered_response_does_not_add_a_render_span(
    drf_stack: DrfHarness,
) -> None:
    _clear(drf_stack.providers)

    response = Client().get("/drf/render/already/")

    assert response.status_code == 200
    assert response.json() == {"already": True}
    # One render span for the view's own render call; Django's later render()
    # call must not create another.
    assert len(_spans(drf_stack.providers, _RENDER)) == 1
    assert len(_spans(drf_stack.providers, _DISPATCH)) == 1
    assert len(_spans(drf_stack.providers, _INITIAL)) == 1
    assert _records(drf_stack.providers) == []


def test_sync_streaming_stays_owned_by_http_middleware(
    drf_stack: DrfHarness,
) -> None:
    _clear(drf_stack.providers)
    drf_stack.urls.PROBES.clear()

    response = Client().get("/drf/stream/sync/")

    assert response.status_code == 200
    assert len(_spans(drf_stack.providers, _DISPATCH)) == 1
    assert len(_spans(drf_stack.providers, _INITIAL)) == 1
    assert _spans(drf_stack.providers, _RENDER) == []
    # Initial completes before dispatch, so the exporter sees it first.
    assert [span.name for span in drf_stack.providers.spans] == [_INITIAL, _DISPATCH]
    assert drf_stack.urls.PROBES == []

    chunks = list(response.streaming_content)
    response.close()

    assert chunks == [b"drf-sync-1\n", b"drf-sync-2\n"]
    server = _server_span(drf_stack.providers)
    assert server.status.status_code is StatusCode.UNSET
    assert drf_stack.urls.PROBES == [
        ("stream-1", "GET", server.context.span_id),
        ("stream-2", "GET", server.context.span_id),
    ]
    assert _records(drf_stack.providers) == []


def test_sync_streaming_failure_is_logged_once_by_http_middleware(
    drf_stack: DrfHarness,
) -> None:
    _clear(drf_stack.providers)

    response = Client().get("/drf/stream/failure/")
    chunks = iter(response.streaming_content)

    assert next(chunks) == b"drf-sync-error-1\n"
    with pytest.raises(RuntimeError, match="stream private failure"):
        next(chunks)
    response.close()

    _assert_one_exception(drf_stack.providers, "RuntimeError")
    assert len(_records(drf_stack.providers)) == 1
    server = _server_span(drf_stack.providers)
    assert server.status.status_code is StatusCode.ERROR
    assert "RuntimeError" in str(server.attributes.get("error.type"))
    _assert_operation_ok(drf_stack.providers, _DISPATCH, _INITIAL)


async def test_async_streaming_stays_owned_by_http_middleware(
    drf_stack: DrfHarness,
) -> None:
    _clear(drf_stack.providers)
    drf_stack.urls.PROBES.clear()

    response = await AsyncClient().get("/drf/stream/async/")

    assert response.status_code == 200
    assert len(_spans(drf_stack.providers, _DISPATCH)) == 1
    assert len(_spans(drf_stack.providers, _INITIAL)) == 1
    assert _spans(drf_stack.providers, _RENDER) == []

    content = response.streaming_content
    chunks = [chunk async for chunk in content]
    await content.aclose()
    response.close()

    assert chunks == [b"drf-async-1\n", b"drf-async-2\n"]
    server = _server_span(drf_stack.providers)
    assert server.status.status_code is StatusCode.UNSET
    assert drf_stack.urls.PROBES == [
        ("stream-async-1", "GET", server.context.span_id),
        ("stream-async-2", "GET", server.context.span_id),
    ]
    assert _records(drf_stack.providers) == []


def test_exclusion_suppresses_hooks_even_with_an_ambient_span(
    drf_stack: DrfHarness,
) -> None:
    _clear(drf_stack.providers)
    tracer = drf_stack.providers.tracer_provider.get_tracer(__name__)
    excluded = _telemetry_settings(drf_stack.providers, exclude=lambda request: True)

    with override_settings(DJANGO_NINJA_OTEL=excluded):
        with tracer.start_as_current_span("ambient"):
            response = Client().get("/drf/api-view/")

    assert response.status_code == 200
    assert [span.name for span in drf_stack.providers.spans] == ["ambient"]
    assert not drf_stack.providers.logs


def test_explicit_none_state_is_not_inferred_from_ambient_span(
    drf_stack: DrfHarness,
) -> None:
    _clear(drf_stack.providers)
    request = RequestFactory().get("/drf/api-view/")
    setattr(request, REQUEST_STATE_ATTRIBUTE, None)
    tracer = drf_stack.providers.tracer_provider.get_tracer(__name__)
    view = drf_stack.urls.ApiView.as_view()

    with tracer.start_as_current_span("ambient-direct"):
        response = view(request)

    assert response.status_code == 200
    assert [span.name for span in drf_stack.providers.spans] == ["ambient-direct"]
    assert not drf_stack.providers.logs


def test_operation_spans_can_be_disabled(drf_stack: DrfHarness) -> None:
    _clear(drf_stack.providers)
    disabled = _telemetry_settings(drf_stack.providers, operation_spans=False)

    with override_settings(DJANGO_NINJA_OTEL=disabled):
        response = Client().get("/drf/api-view/")

    assert response.status_code == 200
    assert [span.kind for span in drf_stack.providers.spans] == [SpanKind.SERVER]
    assert _records(drf_stack.providers) == []


def test_tracing_disabled_keeps_validation_log(drf_stack: DrfHarness) -> None:
    _clear(drf_stack.providers)
    disabled = _telemetry_settings(drf_stack.providers, tracing=False)

    with override_settings(DJANGO_NINJA_OTEL=disabled):
        response = _post_json("/drf/validation/manual/", {})

    assert response.status_code == 400
    assert drf_stack.providers.spans == ()
    records = _records_for(drf_stack.providers, _VALIDATION_EVENT)
    assert len(records) == 1
    assert records[0].attributes[_VALIDATION_COUNT] == 3


def test_logs_disabled_keeps_spans_and_error_status(drf_stack: DrfHarness) -> None:
    _clear(drf_stack.providers)
    disabled = _telemetry_settings(drf_stack.providers, logs=False)

    with override_settings(DJANGO_NINJA_OTEL=disabled):
        response = Client(raise_request_exception=False).get("/drf/unexpected/")

    assert response.status_code == 500
    assert _records(drf_stack.providers) == []
    assert _span(drf_stack.providers, _DISPATCH).status.status_code is StatusCode.ERROR
    assert _server_span(drf_stack.providers).status.status_code is StatusCode.ERROR


def test_http_instrumentation_disabled_keeps_drf_operations(
    drf_stack: DrfHarness,
) -> None:
    _clear(drf_stack.providers)
    disabled = _telemetry_settings(drf_stack.providers, http_instrumentation=False)

    with override_settings(DJANGO_NINJA_OTEL=disabled):
        response = Client().get("/drf/api-view/")

    assert response.status_code == 200
    assert not any(span.kind is SpanKind.SERVER for span in drf_stack.providers.spans)
    assert len(_spans(drf_stack.providers, _DISPATCH)) == 1
    assert len(_spans(drf_stack.providers, _INITIAL)) == 1
    assert len(_spans(drf_stack.providers, _RENDER)) == 1
    assert _records(drf_stack.providers) == []


def test_disabled_http_ownership_parents_drf_operations_to_ambient_span(
    drf_stack: DrfHarness,
) -> None:
    _clear(drf_stack.providers)
    disabled = _telemetry_settings(drf_stack.providers, http_instrumentation=False)
    tracer = drf_stack.providers.tracer_provider.get_tracer(__name__)

    with override_settings(DJANGO_NINJA_OTEL=disabled):
        with tracer.start_as_current_span("ambient-owner") as ambient:
            response = Client().get("/drf/api-view/")

    assert response.status_code == 200
    names = [span.name for span in drf_stack.providers.spans]
    assert "ambient-owner" in names
    assert not any(span.kind is SpanKind.SERVER for span in drf_stack.providers.spans)

    dispatch = _span(drf_stack.providers, _DISPATCH)
    initial = _span(drf_stack.providers, _INITIAL)
    render = _span(drf_stack.providers, _RENDER)
    assert dispatch.parent is not None
    assert dispatch.parent.span_id == ambient.get_span_context().span_id
    assert initial.parent is not None
    assert initial.parent.span_id == dispatch.context.span_id
    assert render.parent is not None
    assert render.parent.span_id == ambient.get_span_context().span_id


def test_install_is_idempotent_and_does_not_double_wrap(
    drf_stack: DrfHarness,
) -> None:
    from rest_framework.response import Response
    from rest_framework.views import APIView

    from django_api_opentelemetry import _drf

    before = (
        APIView.dispatch,
        APIView.initial,
        APIView.handle_exception,
        Response.render,
    )
    _drf.install()
    _drf.install()
    after = (
        APIView.dispatch,
        APIView.initial,
        APIView.handle_exception,
        Response.render,
    )
    assert after == before
    assert all(
        getattr(function, "_django_drf_otel_patched", False) for function in after
    )

    _clear(drf_stack.providers)
    response = Client().get("/drf/api-view/")
    assert response.status_code == 200
    assert len(_spans(drf_stack.providers, _DISPATCH)) == 1
    assert len(_spans(drf_stack.providers, _INITIAL)) == 1
    assert len(_spans(drf_stack.providers, _RENDER)) == 1


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


_MISSING_DRF_SCRIPT = """
    import importlib.metadata
    import logging

    import django

    django.setup()

    # Patch the metadata lookup before the guarded module imports it, so this
    # behaves exactly like an environment where DRF is not installed.
    original = importlib.metadata.version

    def missing(name):
        from importlib.metadata import PackageNotFoundError

        raise PackageNotFoundError(name)

    importlib.metadata.version = missing

    from django_api_opentelemetry import _drf

    messages = []

    class Handler(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    logging.getLogger().addHandler(Handler())
    logging.getLogger().setLevel(logging.WARNING)

    _drf.install()
    _drf.install()
    importlib.metadata.version = original
    print(len(messages))
    print(_drf._install_attempted)
"""


def test_missing_drf_is_silent_and_install_is_one_shot() -> None:
    result = _run_script(_MISSING_DRF_SCRIPT)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["0", "True"]


_VERSION_SCRIPT = """
    import importlib.metadata
    import logging

    import django

    django.setup()

    import importlib.metadata

    importlib.metadata.version = lambda name: {version!r}

    from rest_framework.response import Response
    from rest_framework.views import APIView

    before = (
        APIView.dispatch,
        APIView.initial,
        APIView.handle_exception,
        Response.render,
    )

    from django_api_opentelemetry import _drf

    messages = []

    class Handler(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    logging.getLogger().addHandler(Handler())
    logging.getLogger().setLevel(logging.WARNING)

    _drf.install()
    _drf.install()

    after = (
        APIView.dispatch,
        APIView.initial,
        APIView.handle_exception,
        Response.render,
    )
    print(len(messages))
    print(all(actual is expected for actual, expected in zip(after, before)))
"""


@pytest.mark.parametrize("installed_version", ["3.15.9", "3.19.0", "4.0.0"])
def test_unsupported_drf_version_warns_once_without_patching(
    installed_version: str,
) -> None:
    pytest.importorskip("rest_framework")
    result = _run_script(_VERSION_SCRIPT.format(version=installed_version))

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["1", "True"]


_BAD_SIGNATURE_SCRIPT = """
    import logging

    import django

    django.setup()

    from rest_framework.response import Response
    from rest_framework.views import APIView

    def unexpected_dispatch(self, request):
        return None

    APIView.dispatch = unexpected_dispatch
    before = (
        APIView.dispatch,
        APIView.initial,
        APIView.handle_exception,
        Response.render,
    )

    from django_api_opentelemetry import _drf

    messages = []

    class Handler(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    logging.getLogger().addHandler(Handler())
    logging.getLogger().setLevel(logging.WARNING)

    _drf.install()
    _drf.install()

    after = (
        APIView.dispatch,
        APIView.initial,
        APIView.handle_exception,
        Response.render,
    )
    print(len(messages))
    print(all(actual is expected for actual, expected in zip(after, before)))
"""


def test_bad_hook_signature_warns_once_without_partial_patching() -> None:
    pytest.importorskip("rest_framework")
    result = _run_script(_BAD_SIGNATURE_SCRIPT)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["1", "True"]


def test_public_module_exports_are_unchanged() -> None:
    import django_api_opentelemetry

    assert django_api_opentelemetry.__all__ == [
        "__version__",
        "instrument_asgi",
        "shutdown",
    ]
