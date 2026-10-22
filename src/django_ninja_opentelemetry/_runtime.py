"""Provider resolution and lazy, package-owned OTLP/HTTP configuration."""

from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, NamedTuple
from urllib.parse import urlsplit, urlunsplit

from opentelemetry import _logs, metrics, trace

from ._config import Config

if TYPE_CHECKING:
    from opentelemetry._logs import LoggerProvider
    from opentelemetry.metrics import MeterProvider
    from opentelemetry.trace import TracerProvider

_logger = logging.getLogger(__name__)

_OTLP_ENDPOINT_ENV: Final = "OTEL_EXPORTER_OTLP_ENDPOINT"
_OTLP_PROTOCOL_ENV: Final = "OTEL_EXPORTER_OTLP_PROTOCOL"
_SDK_DISABLED_ENV: Final = "OTEL_SDK_DISABLED"
_HTTP_PROTOBUF: Final = "http/protobuf"
_SHUTDOWN_TIMEOUT_SECONDS: Final = 5.0


class _Signal(NamedTuple):
    name: str
    enabled_attr: str
    provider_attr: str
    register_method: str


_SIGNALS: Final = (
    _Signal("traces", "tracing", "tracer_provider", "add_span_processor"),
    _Signal("metrics", "metrics", "meter_provider", "add_metric_reader"),
    _Signal("logs", "logs", "logger_provider", "add_log_record_processor"),
)
_TRACES, _METRICS, _LOGS = _SIGNALS


class _Skip(Exception):
    """A bounded, safe-to-report reason for skipping one signal."""


class _ConfigurationError(Exception):
    """An invalid OTLP environment value."""


class _CleanupWorker:
    """A lazily started worker that performs package-owned shutdown off atexit."""

    def __init__(self) -> None:
        self._requests: queue.SimpleQueue[list[Any]] = queue.SimpleQueue()
        self._thread = threading.Thread(
            target=self._run,
            name="django-ninja-opentelemetry-cleanup",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        resources = self._requests.get()
        for resource in resources:
            _flush_quiet(resource)
            try:
                resource.shutdown()
            except Exception as exc:  # noqa: BLE001 - continue cleanup
                _logger.warning(
                    "OpenTelemetry shutdown failed for %s (%s)",
                    type(resource).__name__,
                    type(exc).__name__,
                )

    def shutdown(self, resources: list[Any]) -> None:
        self._requests.put(resources)
        self._thread.join(_SHUTDOWN_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            _logger.warning(
                "OpenTelemetry shutdown did not finish within %s seconds",
                _SHUTDOWN_TIMEOUT_SECONDS,
            )


@dataclass(slots=True)
class _State:
    """Mutable state scoped to one serving process."""

    lock: Any = field(default_factory=threading.Lock)
    pid: int = field(default_factory=os.getpid)
    atexit_registered: bool = False
    shutdown_started: bool = False
    registrations: set[tuple[str, int]] = field(default_factory=set)
    owned_providers: list[Any] = field(default_factory=list)
    owned_components: list[Any] = field(default_factory=list)
    inherited_owned_provider_ids: frozenset[int] = frozenset()
    local_providers: dict[str, Any] = field(default_factory=dict)
    cleanup: _CleanupWorker | None = None


_NOOP_PROVIDERS: Final = {
    "traces": trace.NoOpTracerProvider(),
    "metrics": metrics.NoOpMeterProvider(),
    "logs": _logs.NoOpLoggerProvider(),
}

_state = _State()


def _forked_state(inherited: _State) -> _State:
    return _State(
        pid=os.getpid(),
        atexit_registered=inherited.atexit_registered,
        inherited_owned_provider_ids=frozenset(
            id(provider) for provider in inherited.owned_providers
        ),
    )


def _reset_after_fork() -> None:
    """Reset package state and its lock without touching foreign providers."""

    global _state
    _state = _forked_state(_state)


_register_at_fork = getattr(os, "register_at_fork", None)
if _register_at_fork is not None:
    _register_at_fork(after_in_child=_reset_after_fork)


def _process_state() -> _State:
    """Return process-local state, also handling a missed fork callback."""

    global _state
    if _state.pid != os.getpid():
        _state = _forked_state(_state)
    return _state


def get_providers(
    config: Config,
) -> tuple[TracerProvider, MeterProvider, LoggerProvider]:
    """Return the selected providers, lazily configuring opt-in OTLP export."""

    state = _process_state()
    if config.auto_configure:
        with state.lock:
            if not _sdk_disabled():
                for signal in _SIGNALS:
                    if getattr(config, signal.enabled_attr):
                        _configure_signal(signal, config, state)

    return (
        _select_provider(
            _TRACES, config.tracer_provider, trace.get_tracer_provider(), state
        ),
        _select_provider(
            _METRICS, config.meter_provider, metrics.get_meter_provider(), state
        ),
        _select_provider(
            _LOGS, config.logger_provider, _logs.get_logger_provider(), state
        ),
    )


def _select_provider(
    signal: _Signal, supplied: Any, global_provider: Any, state: _State
) -> Any:
    if supplied is not None:
        return supplied
    local = state.local_providers.get(signal.name)
    if local is not None:
        return local
    if id(global_provider) in state.inherited_owned_provider_ids:
        # The API cannot replace globals after fork; never reuse parent-owned state.
        return _NOOP_PROVIDERS[signal.name]
    return global_provider


def _configure_signal(signal: _Signal, config: Config, state: _State) -> None:
    supplied = getattr(config, signal.provider_attr)
    selected = supplied if supplied is not None else _global_provider(signal)
    key = (signal.name, id(selected))
    if key in state.registrations:
        return
    # An attempt identity is one-shot even if setup fails or no endpoint exists.
    state.registrations.add(key)

    try:
        _setup_signal(signal, state, selected, supplied)
    except _Skip as exc:
        _warn(signal, str(exc))
    except Exception as exc:  # noqa: BLE001 - one signal must not break requests
        _warn(signal, f"setup failed ({type(exc).__name__})")


def _setup_signal(
    signal: _Signal,
    state: _State,
    selected: Any,
    supplied: Any,
) -> None:
    endpoint = _prepare_signal(signal)
    if endpoint is None:
        return

    inherited = supplied is None and id(selected) in state.inherited_owned_provider_ids
    new_provider = supplied is None and (
        inherited or _is_unconfigured(signal, selected)
    )
    if not new_provider and not _supports(selected, signal.register_method):
        raise _Skip("the selected provider does not support registration")

    try:
        component = _make_component(signal, endpoint)
    except ImportError:
        raise _Skip("OTLP HTTP/protobuf dependencies are not installed") from None

    if not new_provider:
        try:
            _register(selected, signal.register_method, component)
        except Exception as exc:
            _shutdown_quiet(component)
            raise _Skip(
                f"provider registration failed ({type(exc).__name__})"
            ) from None
        _record(state, signal, selected, component, owned=False)
        return

    try:
        provider = _make_provider(signal, component)
    except ImportError:
        _shutdown_quiet(component)
        raise _Skip("OpenTelemetry SDK is not installed") from None
    except Exception:
        _shutdown_quiet(component)
        raise

    if signal.name != "metrics":
        try:
            _register(provider, signal.register_method, component)
        except Exception as exc:
            _shutdown_quiet(component)
            _shutdown_quiet(provider)
            raise _Skip(
                f"provider registration failed ({type(exc).__name__})"
            ) from None

    if inherited:
        state.local_providers[signal.name] = provider
    elif not _install_global(signal, provider):
        _shutdown_quiet(provider)
        raise _Skip("another provider was configured concurrently")

    _record(state, signal, provider, component, owned=True)


def _prepare_signal(signal: _Signal) -> str | None:
    """Return a validated endpoint, or ``None`` for an absent/off signal."""

    name = signal.name.upper()
    exporter = os.environ.get(f"OTEL_{name}_EXPORTER", "").strip().lower()
    exporters = {part.strip() for part in exporter.split(",") if part.strip()}
    if "none" in exporters:
        return None
    if exporters and exporters != {"otlp"}:
        raise _Skip("only the OTLP exporter is supported")

    try:
        endpoint = _resolve_endpoint(signal)
        if endpoint is not None:
            _validate_protocol(signal)
    except _ConfigurationError as exc:
        raise _Skip(str(exc)) from None
    return endpoint


def _resolve_endpoint(signal: _Signal) -> str | None:
    name = signal.name.upper()
    signal_endpoint = os.environ.get(f"OTEL_EXPORTER_OTLP_{name}_ENDPOINT", "").strip()
    if signal_endpoint:
        _validate_url(signal_endpoint)
        return signal_endpoint

    base_endpoint = os.environ.get(_OTLP_ENDPOINT_ENV, "").strip()
    if not base_endpoint:
        return None
    _validate_url(base_endpoint)
    return _append_signal_path(base_endpoint, f"v1/{signal.name}")


def _validate_url(value: str) -> None:
    if any(character.isspace() for character in value):
        raise _ConfigurationError("invalid OTLP endpoint URL")
    try:
        parsed = urlsplit(value)
        parsed.port  # noqa: B018 - validates malformed ports
        hostname = parsed.hostname
    except ValueError:
        raise _ConfigurationError("invalid OTLP endpoint URL") from None
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise _ConfigurationError("OTLP endpoint must be an absolute HTTP(S) URL")


def _append_signal_path(base_endpoint: str, path: str) -> str:
    parsed = urlsplit(base_endpoint)
    base_path = parsed.path.rstrip("/")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, f"{base_path}/{path}", parsed.query, "")
    )


def _validate_protocol(signal: _Signal) -> None:
    protocol = (
        os.environ.get(f"OTEL_EXPORTER_OTLP_{signal.name.upper()}_PROTOCOL")
        or os.environ.get(_OTLP_PROTOCOL_ENV)
        or _HTTP_PROTOBUF
    )
    if protocol.strip().lower() != _HTTP_PROTOBUF:
        raise _ConfigurationError("only the HTTP/protobuf OTLP protocol is supported")


def _sdk_disabled() -> bool:
    return os.environ.get(_SDK_DISABLED_ENV, "").strip().lower() == "true"


def _global_provider(signal: _Signal) -> Any:
    if signal.name == "traces":
        return trace.get_tracer_provider()
    if signal.name == "metrics":
        return metrics.get_meter_provider()
    return _logs.get_logger_provider()


def _is_unconfigured(signal: _Signal, provider: Any) -> bool:
    if signal.name == "traces":
        return isinstance(provider, trace.ProxyTracerProvider)
    provider_type = type(provider)
    if signal.name == "metrics":
        return (
            provider_type.__module__ == "opentelemetry.metrics._internal"
            and provider_type.__name__ == "_ProxyMeterProvider"
        )
    return (
        provider_type.__module__ == "opentelemetry._logs._internal"
        and provider_type.__name__ == "ProxyLoggerProvider"
    )


def _supports(provider: object, method_name: str) -> bool:
    return callable(getattr(provider, method_name, None))


def _register(provider: Any, method_name: str, component: Any) -> None:
    register = getattr(provider, method_name, None)
    if not callable(register):
        raise _ConfigurationError("provider does not support registration")
    register(component)


def _install_global(signal: _Signal, provider: Any) -> bool:
    if signal.name == "traces":
        trace.set_tracer_provider(provider)
        return trace.get_tracer_provider() is provider
    if signal.name == "metrics":
        metrics.set_meter_provider(provider)
        return metrics.get_meter_provider() is provider
    _logs.set_logger_provider(provider)
    return _logs.get_logger_provider() is provider


def _record(
    state: _State,
    signal: _Signal,
    provider: object,
    component: object,
    *,
    owned: bool,
) -> None:
    state.registrations.add((signal.name, id(provider)))
    if state.cleanup is None:
        try:
            state.cleanup = _CleanupWorker()
        except Exception:
            if owned:
                _shutdown_quiet(provider)
            else:
                _shutdown_quiet(component)
            raise
    if owned:
        state.owned_providers.append(provider)
    else:
        state.owned_components.append(component)
    if not state.atexit_registered:
        atexit.register(_shutdown)
        state.atexit_registered = True


def _warn(signal: _Signal, reason: str) -> None:
    _logger.warning("OpenTelemetry %s export not configured: %s", signal.name, reason)


def _make_component(signal: _Signal, endpoint: str) -> Any:
    if signal.name == "traces":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as Exporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor as Processor
    elif signal.name == "metrics":
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter as Exporter,
        )
        from opentelemetry.sdk.metrics.export import (
            PeriodicExportingMetricReader as Processor,
        )
    else:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter as Exporter,
        )
        from opentelemetry.sdk._logs.export import (
            BatchLogRecordProcessor as Processor,
        )
    processor_factory: Any = Processor
    exporter_factory: Any = Exporter
    return processor_factory(exporter_factory(endpoint=endpoint))


def _make_provider(signal: _Signal, component: Any) -> Any:
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create()
    if signal.name == "traces":
        from opentelemetry.sdk.trace import TracerProvider as Provider
    elif signal.name == "metrics":
        from opentelemetry.sdk.metrics import MeterProvider as Provider

        # Readers added after construction are not shut down by the provider,
        # so pass the package-owned reader to the constructor.
        return Provider(
            metric_readers=[component],
            resource=resource,
            shutdown_on_exit=False,
        )
    else:
        from opentelemetry.sdk._logs import LoggerProvider as Provider
    return Provider(resource=resource, shutdown_on_exit=False)


def _flush_quiet(resource: object) -> None:
    force_flush = getattr(resource, "force_flush", None)
    if not callable(force_flush):
        return
    try:
        force_flush()
    except Exception:  # noqa: BLE001 - shutdown still proceeds
        _logger.debug("OpenTelemetry component flush failed", exc_info=True)


def _shutdown_quiet(resource: object) -> None:
    shutdown = getattr(resource, "shutdown", None)
    if not callable(shutdown):
        return
    try:
        shutdown()
    except Exception:  # noqa: BLE001 - best-effort cleanup of our own object
        _logger.debug("OpenTelemetry component cleanup failed", exc_info=True)


def shutdown() -> None:
    """Flush and shut down package-owned telemetry resources once.

    This is safe to call from an ASGI lifespan shutdown hook (including through
    ``asyncio.to_thread``). The wait is bounded, and application-owned providers
    are never shut down.
    """

    _shutdown()


def _shutdown() -> None:
    """Shut down package-owned resources once, with a bounded wait."""

    state = _state
    if state.pid != os.getpid():
        return
    with state.lock:
        if state.shutdown_started:
            return
        state.shutdown_started = True
        components = state.owned_components[:]
        providers = state.owned_providers[:]
        state.owned_components.clear()
        state.owned_providers.clear()
        state.registrations.clear()
        cleanup = state.cleanup
        state.cleanup = None

    resources = [*components, *providers]
    if cleanup is None:
        if resources:
            _logger.warning(
                "OpenTelemetry shutdown skipped because no cleanup worker is available"
            )
        return
    cleanup.shutdown(resources)
