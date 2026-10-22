"""Shared pytest fixtures and Django initialization for the test suite."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from django.test import AsyncClient, Client
    from opentelemetry.sdk._logs import LoggerProvider, ReadableLogRecord
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
    from opentelemetry.sdk.metrics.export import MetricsData
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )


def pytest_configure(config: pytest.Config) -> None:
    """Select the example project before test modules import Django or Ninja."""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.project.settings")

    import django
    from django.apps import apps

    if not apps.ready:
        django.setup()


@dataclass(frozen=True, slots=True)
class TelemetryProviders:
    """Explicit SDK providers and their in-memory inspection handles."""

    tracer_provider: TracerProvider
    logger_provider: LoggerProvider
    meter_provider: Any
    span_exporter: InMemorySpanExporter
    log_exporter: InMemoryLogRecordExporter
    metric_reader: Any
    resource: Resource

    @property
    def spans(self) -> Sequence[ReadableSpan]:
        """Return spans finished so far, in export order."""

        return self.span_exporter.get_finished_spans()

    @property
    def logs(self) -> Sequence[ReadableLogRecord]:
        """Return log records exported so far, in export order."""

        return self.log_exporter.get_finished_logs()

    def metrics(self) -> MetricsData | None:
        """Collect the latest metrics snapshot from the in-memory reader."""

        return self.metric_reader.get_metrics_data()


@pytest.fixture
def telemetry_providers() -> Iterator[TelemetryProviders]:
    """Create isolated SDK providers backed by in-memory exporters/readers.

    The fixture never changes OpenTelemetry's process-global providers.  SDK
    imports are delayed until the fixture is requested so API-only test jobs
    can still collect and run plain-Django tests.
    """

    pytest.importorskip(
        "opentelemetry.sdk",
        reason="the OpenTelemetry SDK is required for telemetry fixture tests",
    )

    from opentelemetry.sdk._logs import LoggerProvider

    try:
        from opentelemetry.sdk._logs.export import (
            InMemoryLogRecordExporter as InMemoryLogExporter,
        )
    except ImportError:  # pragma: no cover - compatibility with older SDKs
        from opentelemetry.sdk._logs.export import InMemoryLogExporter
    from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    resource = Resource.create({"service.name": "django-api-opentelemetry-tests"})

    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))

    log_exporter = InMemoryLogExporter()
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))

    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[metric_reader],
    )

    providers = TelemetryProviders(
        tracer_provider=tracer_provider,
        logger_provider=logger_provider,
        meter_provider=meter_provider,
        span_exporter=span_exporter,
        log_exporter=log_exporter,
        metric_reader=metric_reader,
        resource=resource,
    )

    try:
        yield providers
    finally:
        meter_provider.shutdown()
        logger_provider.shutdown()
        tracer_provider.shutdown()


@pytest.fixture
def wsgi_client() -> Client:
    """Return a real Django WSGI test client."""

    from django.test import Client

    return Client()


@pytest.fixture
def asgi_client() -> AsyncClient:
    """Return a real Django ASGI test client."""

    from django.test import AsyncClient

    return AsyncClient()
