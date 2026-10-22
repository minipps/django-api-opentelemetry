"""Conformance with the real OpenTelemetry Django instrumentation."""

from __future__ import annotations

import sys
import types
import uuid
from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip("ninja")
pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("opentelemetry.instrumentation.django")

from django.test import Client, override_settings
from django.urls import path
from ninja import NinjaAPI
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.trace import SpanKind

from django_api_opentelemetry import _ninja
from django_api_opentelemetry.apps import MIDDLEWARE_PATH

if TYPE_CHECKING:
    from tests.conftest import TelemetryProviders

PACKAGE_SCOPE = "django_api_opentelemetry"
EXTERNAL_SCOPE = "opentelemetry.instrumentation.django"
VALIDATION_EVENT = "django_ninja.validation.failed"
EXCEPTION_EVENT = "http.server.request.exception"
OPERATION_NAMES = {
    "django_ninja.input_resolution",
    "django_ninja.endpoint",
    "django_ninja.serialization",
}
DURATION = "http.server.request.duration"
ACTIVE = "http.server.active_requests"


def _status(span: Any) -> Any:
    return span.attributes.get(
        "http.response.status_code", span.attributes.get("http.status_code")
    )


def _scope_name(span: Any) -> str:
    scope = span.instrumentation_scope
    return scope.name if scope is not None else ""


def test_real_contrib_and_ninja_operation_span_coexistence(
    telemetry_providers: TelemetryProviders,
    settings: Any,
) -> None:
    providers = telemetry_providers
    original_middleware = settings.MIDDLEWARE
    had_config = hasattr(settings, "DJANGO_NINJA_OTEL")
    original_config = getattr(settings, "DJANGO_NINJA_OTEL", None)
    instrumentor = DjangoInstrumentor()
    urlconf = f"tests._coexistence_urls_{uuid.uuid4().hex}"
    module: types.ModuleType | None = None

    try:
        settings.MIDDLEWARE = []
        instrumentor.instrument(
            tracer_provider=providers.tracer_provider,
            meter_provider=providers.meter_provider,
        )
        settings.MIDDLEWARE = [
            MIDDLEWARE_PATH,
            DjangoInstrumentor._opentelemetry_middleware,
        ]
        assert settings.MIDDLEWARE == [
            MIDDLEWARE_PATH,
            DjangoInstrumentor._opentelemetry_middleware,
        ]
        settings.DJANGO_NINJA_OTEL = {
            "tracer_provider": providers.tracer_provider,
            "meter_provider": providers.meter_provider,
            "logger_provider": providers.logger_provider,
        }

        _ninja.install()
        api = NinjaAPI(urls_namespace=f"coexistence_{uuid.uuid4().hex}")

        @api.get("/ok")
        def ok(request: Any) -> dict[str, bool]:
            return {"ok": True}

        @api.get("/items/{item_id}")
        def item(request: Any, item_id: int) -> dict[str, int]:
            return {"item_id": item_id}

        @api.get("/error")
        def error_route(request: Any) -> None:
            raise RuntimeError("coexistence failure")

        module = types.ModuleType(urlconf)
        module.__dict__["urlpatterns"] = [path("ninja/", api.urls)]
        sys.modules[urlconf] = module

        with override_settings(ROOT_URLCONF=urlconf, DEBUG=False):
            client = Client(raise_request_exception=False)
            success_response = client.get("/ninja/ok")
            validation_response = client.get("/ninja/items/secret")
            error_response = client.get("/ninja/error")
    finally:
        instrumentor.uninstrument()
        settings.MIDDLEWARE = original_middleware
        if had_config:
            settings.DJANGO_NINJA_OTEL = original_config
        elif hasattr(settings, "DJANGO_NINJA_OTEL"):
            del settings.DJANGO_NINJA_OTEL
        if module is not None:
            sys.modules.pop(urlconf, None)

    assert (
        success_response.status_code,
        validation_response.status_code,
        error_response.status_code,
    ) == (200, 422, 500)

    spans = list(providers.spans)
    external = [
        span
        for span in spans
        if span.kind == SpanKind.SERVER and _scope_name(span).startswith(EXTERNAL_SCOPE)
    ]
    assert len(external) == 3, [
        (span.name, span.kind, _scope_name(span)) for span in spans
    ]
    assert not [
        span
        for span in spans
        if span.kind == SpanKind.SERVER and _scope_name(span) == PACKAGE_SCOPE
    ]

    operations = [
        span
        for span in spans
        if _scope_name(span) == PACKAGE_SCOPE and span.name in OPERATION_NAMES
    ]
    assert operations
    for operation in operations:
        matching = [
            span
            for span in external
            if span.context.trace_id == operation.context.trace_id
        ]
        assert len(matching) == 1
        root = matching[0]
        assert operation.parent is not None
        assert operation.parent.span_id == root.context.span_id
        assert operation.context.trace_id == root.context.trace_id
    for root in external:
        assert any(
            operation.context.trace_id == root.context.trace_id
            for operation in operations
        )

    records = [record.log_record for record in providers.logs]
    validation_logs = [
        record for record in records if record.event_name == VALIDATION_EVENT
    ]
    error_logs = [record for record in records if record.event_name == EXCEPTION_EVENT]
    assert len(validation_logs) == 1
    assert len(error_logs) == 1
    validation_log = validation_logs[0]
    error_log = error_logs[0]

    validation_root = next(
        (
            span
            for span in external
            if span.context.trace_id == validation_log.trace_id
            and span.context.span_id == validation_log.span_id
        ),
        None,
    )
    error_root = next(
        (
            span
            for span in external
            if span.context.trace_id == error_log.trace_id
            and span.context.span_id == error_log.span_id
        ),
        None,
    )
    assert validation_root is not None
    assert error_root is not None
    assert validation_root is not error_root
    assert _status(validation_root) == 422
    assert _status(error_root) == 500
    success_root = next(
        span for span in external if span not in (validation_root, error_root)
    )
    assert _status(success_root) == 200

    data = providers.metrics()
    assert data is not None
    package_metrics: set[str] = set()
    external_metrics: set[str] = set()
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            names = {metric.name for metric in scope_metrics.metrics}
            if scope_metrics.scope.name == PACKAGE_SCOPE:
                package_metrics.update(names)
            elif scope_metrics.scope.name.startswith(EXTERNAL_SCOPE):
                external_metrics.update(names)
    assert external_metrics
    assert DURATION not in package_metrics
    assert ACTIVE not in package_metrics
