"""Focused tests for provider selection, OTLP setup and process lifecycle."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from typing import Any

import pytest

from django_ninja_opentelemetry import _runtime
from django_ninja_opentelemetry._config import Config


# These tests exercise real SDK processors and OTLP HTTP exporters in
# subprocesses. The API-only compatibility job intentionally has neither.
def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ImportError:
        return False


_SDK_AVAILABLE = all(
    _module_available(name)
    for name in (
        "opentelemetry.sdk",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "opentelemetry.exporter.otlp.proto.http.metric_exporter",
        "opentelemetry.exporter.otlp.proto.http._log_exporter",
    )
)
pytestmark = pytest.mark.skipif(
    not _SDK_AVAILABLE,
    reason="OpenTelemetry SDK and OTLP HTTP exporters are not installed",
)

_FAKE_EXPORTERS = textwrap.indent(
    """
import os

from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

EVENTS = []


class FakeSpanExporter(SpanExporter):
    def __init__(self, endpoint):
        self.endpoint = endpoint
        EVENTS.append(["span-init", os.getpid(), endpoint])

    def export(self, spans):
        return SpanExportResult.SUCCESS

    def shutdown(self):
        EVENTS.append(["span-shutdown", os.getpid(), ""])

    def force_flush(self, timeout_millis=30000):
        return True


class FakeMetricExporter(MetricExporter):
    def __init__(self, endpoint):
        super().__init__()
        self.endpoint = endpoint
        EVENTS.append(["metric-init", os.getpid(), endpoint])

    def export(self, metrics_data, timeout_millis=10000, **kwargs):
        return MetricExportResult.SUCCESS

    def shutdown(self, timeout_millis=30000, **kwargs):
        EVENTS.append(["metric-shutdown", os.getpid(), ""])

    def force_flush(self, timeout_millis=10000):
        return True


class FakeLogExporter(LogRecordExporter):
    def __init__(self, endpoint):
        self.endpoint = endpoint
        EVENTS.append(["log-init", os.getpid(), endpoint])

    def export(self, batch):
        return LogRecordExportResult.SUCCESS

    def shutdown(self):
        EVENTS.append(["log-shutdown", os.getpid(), ""])

    def force_flush(self, timeout_millis=30000):
        return True


def make_span_processor(endpoint):
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    return BatchSpanProcessor(FakeSpanExporter(endpoint))


def make_metric_reader(endpoint):
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    return PeriodicExportingMetricReader(FakeMetricExporter(endpoint))


def make_log_processor(endpoint):
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    return BatchLogRecordProcessor(FakeLogExporter(endpoint))


def make_component(signal, endpoint):
    if signal.name == "traces":
        return make_span_processor(endpoint)
    if signal.name == "metrics":
        return make_metric_reader(endpoint)
    return make_log_processor(endpoint)
""",
    "        ",
)


def run_script(
    script: str,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 90,
) -> subprocess.CompletedProcess[str]:
    clean_env = {
        key: value for key, value in os.environ.items() if not key.startswith("OTEL_")
    }
    clean_env.pop("DJANGO_NINJA_OTEL_AUTO_CONFIGURE", None)
    clean_env["PYTHONUNBUFFERED"] = "1"
    if env:
        clean_env.update(env)

    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        env=clean_env,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, (
        f"subprocess failed with {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def run_json(script: str, *, env: dict[str, str] | None = None) -> Any:
    result = run_script(script, env=env)
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_import_is_lazy_and_without_auto_configure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    result = run_script(
        """
        import sys
        import threading

        from opentelemetry import _logs, metrics, trace

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        assert not any(
            name == "opentelemetry.sdk" or name.startswith("opentelemetry.sdk.")
            for name in sys.modules
        )
        assert not any(
            name.startswith("opentelemetry.exporter") for name in sys.modules
        )

        before = (
            trace.get_tracer_provider(),
            metrics.get_meter_provider(),
            _logs.get_logger_provider(),
        )
        before_threads = {thread.name for thread in threading.enumerate()}
        providers = _runtime.get_providers(Config())

        assert providers == before
        assert {thread.name for thread in threading.enumerate()} == before_threads
        assert not any(
            name == "opentelemetry.sdk" or name.startswith("opentelemetry.sdk.")
            for name in sys.modules
        )
        print("ok")
        """
    )
    assert result.stdout.strip() == "ok"


def test_explicit_providers_are_returned_without_touching_globals() -> None:
    class Provider:
        def get_tracer(self, *args: Any, **kwargs: Any) -> None:
            pass

        def get_meter(self, *args: Any, **kwargs: Any) -> None:
            pass

        def get_logger(self, *args: Any, **kwargs: Any) -> None:
            pass

    provider = Provider()
    config = Config(
        tracer_provider=provider,  # type: ignore[arg-type]
        meter_provider=provider,  # type: ignore[arg-type]
        logger_provider=provider,  # type: ignore[arg-type]
    )

    assert _runtime.get_providers(config) == (provider, provider, provider)


@pytest.fixture
def clean_otlp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
        "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
        "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
        "OTEL_TRACES_EXPORTER",
        "OTEL_METRICS_EXPORTER",
        "OTEL_LOGS_EXPORTER",
        "OTEL_SDK_DISABLED",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("signal", "base", "expected"),
    [
        (_runtime._TRACES, "http://collector:4318", "http://collector:4318/v1/traces"),
        (
            _runtime._TRACES,
            "http://collector:4318/",
            "http://collector:4318/v1/traces",
        ),
        (
            _runtime._METRICS,
            "https://collector:4318/otlp",
            "https://collector:4318/otlp/v1/metrics",
        ),
        (_runtime._LOGS, "http://collector:4318", "http://collector:4318/v1/logs"),
    ],
)
def test_base_endpoint_appends_signal_path(
    monkeypatch: pytest.MonkeyPatch,
    clean_otlp_env: None,
    signal: _runtime._Signal,
    base: str,
    expected: str,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", base)

    assert _runtime._resolve_endpoint(signal) == expected


def test_signal_endpoint_literal_overrides_base(
    monkeypatch: pytest.MonkeyPatch, clean_otlp_env: None
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://traces.test/custom/path"
    )

    assert (
        _runtime._resolve_endpoint(_runtime._TRACES)
        == "https://traces.test/custom/path"
    )


@pytest.mark.parametrize("disabled", ["none", "NONE", " none "])
def test_exporter_none_disables_signal(
    monkeypatch: pytest.MonkeyPatch, clean_otlp_env: None, disabled: str
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", disabled)

    assert _runtime._prepare_signal(_runtime._METRICS) is None
    assert _runtime._prepare_signal(_runtime._TRACES) is not None


@pytest.mark.parametrize("value", ["none", "NONE", " none "])
def test_endpoint_none_is_not_a_disable_sentinel(
    monkeypatch: pytest.MonkeyPatch, clean_otlp_env: None, value: str
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", value)

    with pytest.raises(_runtime._ConfigurationError):
        _runtime._resolve_endpoint(_runtime._TRACES)


def test_unsupported_exporter_names_are_skipped(
    monkeypatch: pytest.MonkeyPatch, clean_otlp_env: None
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")

    with pytest.raises(_runtime._Skip, match="only the OTLP exporter"):
        _runtime._prepare_signal(_runtime._TRACES)


def test_absent_endpoint_has_no_default(clean_otlp_env: None) -> None:
    assert _runtime._resolve_endpoint(_runtime._TRACES) is None
    assert _runtime._resolve_endpoint(_runtime._METRICS) is None
    assert _runtime._resolve_endpoint(_runtime._LOGS) is None


@pytest.mark.parametrize(
    "value",
    [
        "collector:4318",
        "localhost:4318",
        "ftp://collector:4318",
        "http://",
        "http://collector:bad",
        "http://bad host:4318",
    ],
)
def test_invalid_endpoint_is_rejected(
    monkeypatch: pytest.MonkeyPatch, clean_otlp_env: None, value: str
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", value)

    with pytest.raises(_runtime._ConfigurationError):
        _runtime._resolve_endpoint(_runtime._TRACES)


def test_protocol_defaults_to_http_protobuf(clean_otlp_env: None) -> None:
    _runtime._validate_protocol(_runtime._TRACES)


@pytest.mark.parametrize("value", ["http/protobuf", "HTTP/Protobuf", " http/protobuf "])
def test_http_protobuf_protocol_is_accepted(
    monkeypatch: pytest.MonkeyPatch, clean_otlp_env: None, value: str
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", value)

    _runtime._validate_protocol(_runtime._TRACES)


@pytest.mark.parametrize("value", ["grpc", "http/json", "none"])
def test_non_http_protobuf_protocol_is_rejected(
    monkeypatch: pytest.MonkeyPatch, clean_otlp_env: None, value: str
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", value)

    with pytest.raises(_runtime._ConfigurationError):
        _runtime._validate_protocol(_runtime._TRACES)


def test_signal_protocol_overrides_general_protocol(
    monkeypatch: pytest.MonkeyPatch, clean_otlp_env: None
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/protobuf")

    _runtime._validate_protocol(_runtime._TRACES)
    with pytest.raises(_runtime._ConfigurationError):
        _runtime._validate_protocol(_runtime._METRICS)


def test_auto_configure_without_endpoint_imports_no_sdk() -> None:
    result = run_script(
        """
        import sys

        from opentelemetry import _logs, metrics, trace

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        before = (
            trace.get_tracer_provider(),
            metrics.get_meter_provider(),
            _logs.get_logger_provider(),
        )
        providers = _runtime.get_providers(Config(auto_configure=True))

        assert providers == before
        assert not any(
            name == "opentelemetry.sdk" or name.startswith("opentelemetry.sdk.")
            for name in sys.modules
        )
        assert not any(
            name.startswith("opentelemetry.exporter") for name in sys.modules
        )
        print("ok")
        """
    )
    assert result.stdout.strip() == "ok"


def test_auto_configure_base_endpoint_sets_all_signal_providers() -> None:
    result = run_json(
        _FAKE_EXPORTERS
        + """
        import json

        from opentelemetry import _logs, metrics, trace
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.trace import TracerProvider

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        _runtime._make_component = make_component

        config = Config(auto_configure=True)
        first = _runtime.get_providers(config)
        second = _runtime.get_providers(config)

        assert first == second
        assert isinstance(first[0], TracerProvider)
        assert isinstance(first[1], MeterProvider)
        assert isinstance(first[2], LoggerProvider)
        assert trace.get_tracer_provider() is first[0]
        assert metrics.get_meter_provider() is first[1]
        assert _logs.get_logger_provider() is first[2]

        _runtime._shutdown()
        _runtime._shutdown()
        print(json.dumps(EVENTS))
        """,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
    )

    inits = [event for event in result if event[0].endswith("-init")]
    shutdowns = [event for event in result if event[0].endswith("-shutdown")]
    assert [event[0] for event in inits] == [
        "span-init",
        "metric-init",
        "log-init",
    ]
    assert [event[2] for event in inits] == [
        "http://collector:4318/v1/traces",
        "http://collector:4318/v1/metrics",
        "http://collector:4318/v1/logs",
    ]
    assert [event[0] for event in shutdowns] == [
        "span-shutdown",
        "metric-shutdown",
        "log-shutdown",
    ]


def test_signal_endpoint_overrides_and_exporter_none_skips_signals() -> None:
    result = run_json(
        _FAKE_EXPORTERS
        + """
        import json

        from opentelemetry import _logs, metrics, trace
        from opentelemetry.sdk.trace import TracerProvider

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        _runtime._make_component = make_component

        before_meter = metrics.get_meter_provider()
        before_logger = _logs.get_logger_provider()
        providers = _runtime.get_providers(Config(auto_configure=True))

        assert isinstance(providers[0], TracerProvider)
        assert providers[0] is trace.get_tracer_provider()
        assert providers[1] is before_meter
        assert providers[2] is before_logger

        _runtime._shutdown()
        print(json.dumps({"events": EVENTS, "meter_same": providers[1] is before_meter,
                          "logger_same": providers[2] is before_logger}))
        """,
        env={
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://base:4318",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://traces.test/custom",
            "OTEL_METRICS_EXPORTER": "none",
            "OTEL_LOGS_EXPORTER": "none",
        },
    )

    assert result["meter_same"] is True
    assert result["logger_same"] is True
    assert [event[0] for event in result["events"]] == ["span-init", "span-shutdown"]
    assert result["events"][0][2] == "https://traces.test/custom"


def test_protocol_validation_is_isolated_per_signal() -> None:
    result = run_script(
        _FAKE_EXPORTERS
        + """
        import logging
        import sys

        logging.basicConfig(level=logging.WARNING, format="%(message)s")

        from opentelemetry import _logs, metrics, trace

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        _runtime._make_component = make_component

        before = (
            trace.get_tracer_provider(),
            metrics.get_meter_provider(),
            _logs.get_logger_provider(),
        )
        providers = _runtime.get_providers(Config(auto_configure=True))

        assert providers[0] is not before[0]
        assert providers[1] is before[1]
        assert providers[2] is before[2]
        _runtime._shutdown()
        print("ok")
        """,
        env={
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
            "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
            "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL": "http/json",
            "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "grpc",
        },
    )

    assert result.stdout.strip() == "ok"
    assert "OpenTelemetry metrics export not configured" in result.stderr
    assert "OpenTelemetry logs export not configured" in result.stderr
    assert "OpenTelemetry traces export not configured" not in result.stderr


def test_invalid_endpoint_warning_does_not_leak_url_or_credentials() -> None:
    result = run_script(
        """
        import logging
        import sys

        logging.basicConfig(level=logging.WARNING, format="%(message)s")

        from opentelemetry import _logs, metrics, trace

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        before = (
            trace.get_tracer_provider(),
            metrics.get_meter_provider(),
            _logs.get_logger_provider(),
        )
        providers = _runtime.get_providers(Config(auto_configure=True))

        assert providers == before
        assert not any(
            name == "opentelemetry.sdk" or name.startswith("opentelemetry.sdk.")
            for name in sys.modules
        )
        print("ok")
        """,
        env={
            "OTEL_EXPORTER_OTLP_ENDPOINT": "ftp://user:super-secret@collector.internal:4318",
        },
    )

    assert result.stdout.strip() == "ok"
    assert "OpenTelemetry traces export not configured" in result.stderr
    assert "OpenTelemetry metrics export not configured" in result.stderr
    assert "OpenTelemetry logs export not configured" in result.stderr
    assert "super-secret" not in result.stderr
    assert "collector.internal" not in result.stderr
    assert "ftp://" not in result.stderr


def test_missing_optional_sdk_dependencies_fail_open() -> None:
    result = run_script(
        """
        import logging
        import sys
        import threading

        logging.basicConfig(level=logging.WARNING, format="%(message)s")

        from opentelemetry import _logs, metrics, trace

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config


        class BlockSdk:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "opentelemetry.sdk" or fullname.startswith(
                    "opentelemetry.sdk."
                ):
                    raise ImportError("blocked SDK")
                if fullname == "opentelemetry.exporter" or fullname.startswith(
                    "opentelemetry.exporter."
                ):
                    raise ImportError("blocked exporter")
                return None


        sys.meta_path.insert(0, BlockSdk())
        before = (
            trace.get_tracer_provider(),
            metrics.get_meter_provider(),
            _logs.get_logger_provider(),
        )
        before_threads = {thread.name for thread in threading.enumerate()}
        providers = _runtime.get_providers(Config(auto_configure=True))

        assert providers == before
        assert {thread.name for thread in threading.enumerate()} == before_threads
        _runtime._shutdown()
        print("ok")
        """,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
    )

    assert result.stdout.strip() == "ok"
    for signal in ("traces", "metrics", "logs"):
        assert f"OpenTelemetry {signal} export not configured" in result.stderr


def test_one_signal_setup_failure_does_not_block_the_others() -> None:
    result = run_script(
        _FAKE_EXPORTERS
        + """
        import json
        import logging

        logging.basicConfig(level=logging.WARNING, format="%(message)s")

        from opentelemetry import _logs, metrics, trace
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk.trace import TracerProvider

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        def broken_component(signal, endpoint):
            if signal.name == "metrics":
                raise RuntimeError("do not print this secret")
            return make_component(signal, endpoint)

        _runtime._make_component = broken_component

        before_meter = metrics.get_meter_provider()
        providers = _runtime.get_providers(Config(auto_configure=True))

        assert isinstance(providers[0], TracerProvider)
        assert isinstance(providers[2], LoggerProvider)
        assert providers[1] is before_meter
        _runtime._shutdown()
        print("ok")
        """,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
    )

    assert result.stdout.strip() == "ok"
    assert "OpenTelemetry metrics export not configured: setup failed" in result.stderr
    assert "do not print this secret" not in result.stderr


def test_failed_global_install_leaves_no_orphan_components() -> None:
    result = run_json(
        _FAKE_EXPORTERS
        + """
        import json

        from opentelemetry import _logs, metrics, trace

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        _runtime._make_component = make_component

        def fail_install(name, provider):
            return False

        _runtime._install_global = fail_install

        before = (
            trace.get_tracer_provider(),
            metrics.get_meter_provider(),
            _logs.get_logger_provider(),
        )
        providers = _runtime.get_providers(Config(auto_configure=True))
        assert providers == before

        after_setup = list(EVENTS)
        _runtime._shutdown()
        print(json.dumps({"after_setup": after_setup, "after_shutdown": EVENTS}))
        """,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
    )

    after_setup = result["after_setup"]
    assert [event[0] for event in after_setup] == [
        "span-init",
        "span-shutdown",
        "metric-init",
        "metric-shutdown",
        "log-init",
        "log-shutdown",
    ]
    assert result["after_shutdown"] == after_setup


def test_unsupported_supplied_providers_are_preserved() -> None:
    result = run_json(
        _FAKE_EXPORTERS
        + """
        import json

        from opentelemetry import _logs, metrics, trace

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        _runtime._make_component = make_component

        class Provider:
            def get_tracer(self, *args, **kwargs):
                return None

            def get_meter(self, *args, **kwargs):
                return None

            def get_logger(self, *args, **kwargs):
                return None

        provider = Provider()
        before = (
            trace.get_tracer_provider(),
            metrics.get_meter_provider(),
            _logs.get_logger_provider(),
        )
        providers = _runtime.get_providers(
            Config(
                auto_configure=True,
                tracer_provider=provider,
                meter_provider=provider,
                logger_provider=provider,
            )
        )

        assert providers == (provider, provider, provider)
        assert (
            trace.get_tracer_provider(),
            metrics.get_meter_provider(),
            _logs.get_logger_provider(),
        ) == before
        _runtime._shutdown()
        print(json.dumps({"events": EVENTS}))
        """,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
    )

    assert result["events"] == []


def test_explicit_noop_global_providers_remain_noop() -> None:
    result = run_json(
        _FAKE_EXPORTERS
        + """
        import json

        from opentelemetry import _logs, metrics, trace

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        _runtime._make_component = make_component

        trace.set_tracer_provider(trace.NoOpTracerProvider())
        metrics.set_meter_provider(metrics.NoOpMeterProvider())
        _logs.set_logger_provider(_logs.NoOpLoggerProvider())

        before = (
            trace.get_tracer_provider(),
            metrics.get_meter_provider(),
            _logs.get_logger_provider(),
        )
        providers = _runtime.get_providers(Config(auto_configure=True))

        assert providers == before
        assert isinstance(providers[0], trace.NoOpTracerProvider)
        assert isinstance(providers[1], metrics.NoOpMeterProvider)
        assert isinstance(providers[2], _logs.NoOpLoggerProvider)
        _runtime._shutdown()
        print(json.dumps({"events": EVENTS}))
        """,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
    )

    assert result["events"] == []


def test_repeated_concurrent_first_use_registers_once() -> None:
    result = run_json(
        _FAKE_EXPORTERS
        + """
        import json
        from concurrent.futures import ThreadPoolExecutor

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        _runtime._make_component = make_component

        def call_once(index):
            return _runtime.get_providers(Config(auto_configure=True))

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(call_once, range(32)))

        assert all(item == results[0] for item in results)
        _runtime._shutdown()
        _runtime._shutdown()
        print(json.dumps(EVENTS))
        """,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
    )

    assert [event[0] for event in result] == [
        "span-init",
        "metric-init",
        "log-init",
        "span-shutdown",
        "metric-shutdown",
        "log-shutdown",
    ]


def test_sdk_disabled_does_not_import_or_configure_sdk() -> None:
    result = run_script(
        """
        import sys

        from opentelemetry import _logs, metrics, trace

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        before = (
            trace.get_tracer_provider(),
            metrics.get_meter_provider(),
            _logs.get_logger_provider(),
        )
        providers = _runtime.get_providers(Config(auto_configure=True))

        assert providers == before
        assert not any(
            name == "opentelemetry.sdk" or name.startswith("opentelemetry.sdk.")
            for name in sys.modules
        )
        assert not any(
            name.startswith("opentelemetry.exporter") for name in sys.modules
        )
        print("ok")
        """,
        env={
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
            "OTEL_SDK_DISABLED": "true",
        },
    )
    assert result.stdout.strip() == "ok"


def test_fork_resets_package_state_and_cleanup_ownership() -> None:
    result = run_json(
        _FAKE_EXPORTERS
        + """
        import json
        import os
        import select

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        _runtime._make_component = make_component

        parent_providers = _runtime.get_providers(Config(auto_configure=True))
        parent_pid = os.getpid()
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()

        if child_pid == 0:
            os.close(read_fd)
            try:
                child_providers = _runtime.get_providers(Config(auto_configure=True))
                payload = {
                    "pid": os.getpid(),
                    "different": (
                        child_providers[0] is not parent_providers[0]
                        and child_providers[1] is not parent_providers[1]
                        and child_providers[2] is not parent_providers[2]
                    ),
                    "before_shutdown": list(EVENTS),
                }
                _runtime._shutdown()
                _runtime._shutdown()
                payload["after_shutdown"] = list(EVENTS)
                os.write(write_fd, json.dumps(payload).encode())
            except BaseException as exc:
                os.write(write_fd, json.dumps({"error": repr(exc)}).encode())
            finally:
                os.close(write_fd)
                os._exit(0)

        os.close(write_fd)
        ready, _, _ = select.select([read_fd], [], [], 30)
        assert ready, "forked child did not report within 30 seconds"
        chunks = []
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(read_fd)
        os.waitpid(child_pid, 0)

        child = json.loads(b"".join(chunks))
        assert "error" not in child, child.get("error")
        assert child["different"] is True

        _runtime._shutdown()
        print(json.dumps({
            "parent_pid": parent_pid,
            "child_pid": child["pid"],
            "parent_events": EVENTS,
            "child": child,
        }))
        """,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
    )

    parent_pid = result["parent_pid"]
    child_pid = result["child_pid"]
    assert parent_pid != child_pid

    parent_inits = [e for e in result["parent_events"] if e[0].endswith("-init")]
    parent_shutdowns = [
        e for e in result["parent_events"] if e[0].endswith("-shutdown")
    ]
    assert {event[1] for event in parent_inits} == {parent_pid}
    assert {event[1] for event in parent_shutdowns} == {parent_pid}
    assert len(parent_shutdowns) == 3

    child_before = result["child"]["before_shutdown"]
    child_after = result["child"]["after_shutdown"]
    child_new_inits = [
        e for e in child_before if e[0].endswith("-init") and e[1] != parent_pid
    ]
    child_shutdowns = [
        e for e in child_after if e[0].endswith("-shutdown") and e[1] != parent_pid
    ]
    assert {event[1] for event in child_new_inits} == {child_pid}
    assert len(child_new_inits) == 3
    assert {event[1] for event in child_shutdowns} == {child_pid}
    assert len(child_shutdowns) == 3


def test_disabled_config_signals_create_no_components() -> None:
    result = run_json(
        _FAKE_EXPORTERS
        + """
        import json

        from opentelemetry import _logs, metrics, trace
        from opentelemetry.sdk._logs import LoggerProvider

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        _runtime._make_component = make_component

        before = (
            trace.get_tracer_provider(),
            metrics.get_meter_provider(),
            _logs.get_logger_provider(),
        )
        providers = _runtime.get_providers(
            Config(auto_configure=True, tracing=False, metrics=False)
        )

        assert providers[0] is before[0]
        assert providers[1] is before[1]
        assert isinstance(providers[2], LoggerProvider)
        _runtime._shutdown()
        print(json.dumps(EVENTS))
        """,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
    )

    assert [event[0] for event in result] == ["log-init", "log-shutdown"]


def test_two_explicit_providers_each_register_once() -> None:
    result = run_json(
        _FAKE_EXPORTERS
        + """
        import json

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        _runtime._make_component = make_component


        class Provider:
            def __init__(self):
                self.components = []
                self.shutdowns = 0

            def get_tracer(self, *args, **kwargs):
                return None

            def get_meter(self, *args, **kwargs):
                return None

            def get_logger(self, *args, **kwargs):
                return None

            def add_span_processor(self, component):
                self.components.append(component)

            def add_metric_reader(self, component):
                self.components.append(component)

            def add_log_record_processor(self, component):
                self.components.append(component)

            def shutdown(self):
                self.shutdowns += 1


        first = Provider()
        second = Provider()
        first_config = Config(
            auto_configure=True,
            tracer_provider=first,
            meter_provider=first,
            logger_provider=first,
        )
        second_config = Config(
            auto_configure=True,
            tracer_provider=second,
            meter_provider=second,
            logger_provider=second,
        )

        for _ in range(3):
            assert _runtime.get_providers(first_config) == (first, first, first)
            assert _runtime.get_providers(second_config) == (second, second, second)

        assert len(first.components) == 3
        assert len(second.components) == 3
        assert first.shutdowns == 0
        assert second.shutdowns == 0
        _runtime._shutdown()
        assert first.shutdowns == 0
        assert second.shutdowns == 0
        print(json.dumps(EVENTS))
        """,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
    )

    assert [event[0] for event in result].count("span-init") == 2
    assert [event[0] for event in result].count("metric-init") == 2
    assert [event[0] for event in result].count("log-init") == 2
    assert [event[0] for event in result].count("span-shutdown") == 2
    assert [event[0] for event in result].count("metric-shutdown") == 2
    assert [event[0] for event in result].count("log-shutdown") == 2


def test_failed_factory_attempts_and_warnings_happen_once_per_signal() -> None:
    result = run_script(
        """
        import logging

        logging.basicConfig(level=logging.WARNING, format="%(message)s")

        from opentelemetry import _logs, metrics, trace

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        CALLS = []


        def broken_component(signal, endpoint):
            CALLS.append(signal.name)
            raise RuntimeError("factory failure")


        _runtime._make_component = broken_component
        before = (
            trace.get_tracer_provider(),
            metrics.get_meter_provider(),
            _logs.get_logger_provider(),
        )
        config = Config(auto_configure=True)
        assert _runtime.get_providers(config) == before
        assert _runtime.get_providers(config) == before
        assert CALLS == ["traces", "metrics", "logs"]
        _runtime._shutdown()
        print("ok")
        """,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
    )

    assert result.stdout.strip() == "ok"
    assert result.stderr.count("OpenTelemetry traces export not configured") == 1
    assert result.stderr.count("OpenTelemetry metrics export not configured") == 1
    assert result.stderr.count("OpenTelemetry logs export not configured") == 1


def test_attempt_without_endpoint_is_not_retried_after_environment_changes() -> None:
    result = run_json(
        """
        import json
        import os

        from opentelemetry import _logs, metrics, trace

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        calls = []


        def component(signal, endpoint):
            calls.append(signal.name)
            raise AssertionError("must not be called")


        _runtime._make_component = component
        before = (
            trace.get_tracer_provider(),
            metrics.get_meter_provider(),
            _logs.get_logger_provider(),
        )
        config = Config(auto_configure=True)
        assert _runtime.get_providers(config) == before
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector:4318"
        assert _runtime.get_providers(config) == before
        assert calls == []
        _runtime._shutdown()
        print(json.dumps(calls))
        """
    )

    assert result == []


def test_new_provider_registration_failure_cleans_component_and_provider() -> None:
    result = run_json(
        _FAKE_EXPORTERS
        + """
        import json
        import logging

        logging.basicConfig(level=logging.WARNING, format="%(message)s")

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        _runtime._make_component = make_component


        class BrokenProvider:
            def __init__(self, name):
                self.name = name

            def add_span_processor(self, component):
                raise RuntimeError("cannot register")

            def add_metric_reader(self, component):
                raise RuntimeError("cannot register")

            def add_log_record_processor(self, component):
                raise RuntimeError("cannot register")

            def shutdown(self):
                EVENTS.append(["provider-shutdown", os.getpid(), self.name])


        def make_broken_provider(signal, component):
            if signal.name == "metrics":
                raise RuntimeError("metric provider factory failure")
            return BrokenProvider(signal.name)


        _runtime._make_provider = make_broken_provider
        providers = _runtime.get_providers(Config(auto_configure=True))
        after_setup = list(EVENTS)
        _runtime._shutdown()
        print(json.dumps({"after_setup": after_setup, "after_shutdown": EVENTS}))
        """,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
    )

    events = result["after_setup"]
    assert [event[0] for event in events].count("metric-shutdown") == 1
    assert [event[0] for event in events].count("span-shutdown") == 1
    assert [event[0] for event in events].count("log-shutdown") == 1
    assert [event[2] for event in events if event[0] == "provider-shutdown"] == [
        "traces",
        "logs",
    ]
    assert result["after_shutdown"] == events


def test_normal_cleanup_returns_promptly_without_timeout_warning() -> None:
    result = run_script(
        """
        import json
        import time

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        EVENTS = []


        class Component:
            def shutdown(self):
                EVENTS.append("shutdown")


        class Provider:
            def get_tracer(self, *args, **kwargs):
                return None

            def add_span_processor(self, component):
                self.component = component


        _runtime._make_component = lambda signal, endpoint: Component()
        provider = Provider()
        _runtime.get_providers(
            Config(
                auto_configure=True,
                metrics=False,
                logs=False,
                tracer_provider=provider,
            )
        )
        started = time.monotonic()
        _runtime._shutdown()
        elapsed = time.monotonic() - started
        print(json.dumps({"elapsed": elapsed, "events": EVENTS}))
        """,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
    )

    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["elapsed"] < 2.0
    assert payload["events"] == ["shutdown"]
    assert "did not finish within" not in result.stderr


def test_slow_atexit_cleanup_is_bounded() -> None:
    import time

    started = time.monotonic()
    result = run_script(
        """
        import time

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config


        class SlowComponent:
            def shutdown(self):
                time.sleep(10)


        class Provider:
            def get_tracer(self, *args, **kwargs):
                return None

            def add_span_processor(self, component):
                self.component = component


        _runtime._make_component = lambda signal, endpoint: SlowComponent()
        _runtime.get_providers(
            Config(
                auto_configure=True,
                metrics=False,
                logs=False,
                tracer_provider=Provider(),
            )
        )
        print("done")
        """,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
        timeout=30,
    )
    elapsed = time.monotonic() - started

    assert result.stdout.strip() == "done"
    assert 4.0 <= elapsed < 8.5
    assert "OpenTelemetry shutdown did not finish within" in result.stderr


def test_normal_interpreter_exit_flushes_metrics_over_http() -> None:
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", 0))
            self.rfile.read(length)
            received.append(self.path)
            self.send_response(200)
            self.send_header("content-type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = run_script(
            """
            from django_ninja_opentelemetry import _runtime
            from django_ninja_opentelemetry._config import Config

            providers = _runtime.get_providers(
                Config(auto_configure=True, tracing=False, logs=False)
            )
            counter = providers[1].get_meter("exporter-probe").create_counter(
                "exporter.probe"
            )
            counter.add(3)
            print("done")
            """,
            env={
                "OTEL_EXPORTER_OTLP_ENDPOINT": (
                    f"http://127.0.0.1:{server.server_address[1]}"
                ),
                "OTEL_TRACES_EXPORTER": "none",
                "OTEL_LOGS_EXPORTER": "none",
                "OTEL_METRIC_EXPORT_INTERVAL": "60000",
                "OTEL_EXPORTER_OTLP_METRICS_TIMEOUT": "2",
            },
            timeout=30,
        )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "/v1/metrics" not in received:
            time.sleep(0.05)
        assert result.stdout.strip() == "done"
        assert "/v1/metrics" in received
        assert "did not finish within" not in result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_public_shutdown_flushes_once_before_closing_owned_resources() -> None:
    result = run_json(
        """
        import asyncio
        import json

        from django_ninja_opentelemetry import shutdown
        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config

        EVENTS = []


        class Resource:
            def force_flush(self):
                EVENTS.append("flush")

            def shutdown(self):
                EVENTS.append("shutdown")


        class Provider:
            def get_tracer(self, *args, **kwargs):
                return None

            def add_span_processor(self, component):
                self.component = component


        _runtime._make_component = lambda signal, endpoint: Resource()
        _runtime.get_providers(
            Config(
                auto_configure=True,
                metrics=False,
                logs=False,
                tracer_provider=Provider(),
            )
        )
        asyncio.run(asyncio.to_thread(shutdown))
        asyncio.run(asyncio.to_thread(shutdown))
        print(json.dumps(EVENTS))
        """,
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
    )

    assert result == ["flush", "shutdown"]


def test_existing_foreign_globals_are_preserved_across_fork() -> None:
    result = run_script(
        """
        import json
        import os
        import select

        from opentelemetry import _logs, metrics, trace

        from django_ninja_opentelemetry import _runtime
        from django_ninja_opentelemetry._config import Config


        class Provider:
            def get_tracer(self, *args, **kwargs):
                return None

            def get_meter(self, *args, **kwargs):
                return None

            def get_logger(self, *args, **kwargs):
                return None


        tracer = Provider()
        meter = Provider()
        logger = Provider()
        trace.set_tracer_provider(tracer)
        metrics.set_meter_provider(meter)
        _logs.set_logger_provider(logger)

        expected = (
            trace.get_tracer_provider(),
            metrics.get_meter_provider(),
            _logs.get_logger_provider(),
        )
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()

        if child_pid == 0:
            os.close(read_fd)
            child = _runtime.get_providers(Config())
            os.write(
                write_fd,
                json.dumps([child[0] is expected[0],
                            child[1] is expected[1],
                            child[2] is expected[2]]).encode(),
            )
            os.close(write_fd)
            os._exit(0)

        os.close(write_fd)
        ready, _, _ = select.select([read_fd], [], [], 30)
        assert ready, "forked child did not report within 30 seconds"
        payload = os.read(read_fd, 65536)
        os.close(read_fd)
        os.waitpid(child_pid, 0)
        assert json.loads(payload) == [True, True, True]
        assert _runtime.get_providers(Config()) == expected
        print("ok")
        """
    )

    assert result.stdout.strip() == "ok"


def test_sdk_interfaces_used_are_available() -> None:
    import inspect

    from opentelemetry.sdk._logs import LoggerProvider as SdkLoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    assert "shutdown_on_exit" in inspect.signature(SdkTracerProvider).parameters
    assert "shutdown_on_exit" in inspect.signature(SdkMeterProvider).parameters
    assert "shutdown_on_exit" in inspect.signature(SdkLoggerProvider).parameters
    assert callable(SdkTracerProvider.add_span_processor)
    assert callable(SdkMeterProvider.add_metric_reader)
    assert callable(SdkLoggerProvider.add_log_record_processor)
    assert callable(Resource.create)
    assert callable(BatchSpanProcessor)
    assert callable(BatchLogRecordProcessor)
    assert callable(PeriodicExportingMetricReader)
