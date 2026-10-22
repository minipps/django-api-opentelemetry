"""Read and validate the ``DJANGO_NINJA_OTEL`` Django setting."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest

if TYPE_CHECKING:
    from opentelemetry._logs import LoggerProvider
    from opentelemetry.metrics import MeterProvider
    from opentelemetry.trace import TracerProvider

_ENV_AUTO_CONFIGURE = "DJANGO_NINJA_OTEL_AUTO_CONFIGURE"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

_BOOLEAN_KEYS = frozenset(
    {
        "tracing",
        "metrics",
        "logs",
        "operation_spans",
        "http_instrumentation",
        "auto_configure",
    }
)
_PROVIDER_KEYS = {
    "tracer_provider": "get_tracer",
    "meter_provider": "get_meter",
    "logger_provider": "get_logger",
}
_ALLOWED_KEYS = _BOOLEAN_KEYS | {"exclude"} | set(_PROVIDER_KEYS)


@dataclass(frozen=True, slots=True)
class Config:
    """Normalized package configuration."""

    tracing: bool = True
    metrics: bool = True
    logs: bool = True
    operation_spans: bool = True
    http_instrumentation: bool = True
    auto_configure: bool = False
    exclude: Callable[[HttpRequest], bool] | None = None
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    logger_provider: LoggerProvider | None = None


def get_config() -> Config:
    """Return the validated configuration from Django settings and environment."""

    raw = getattr(settings, "DJANGO_NINJA_OTEL", None)
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ImproperlyConfigured(
            "DJANGO_NINJA_OTEL must be a mapping of setting names to values."
        )

    unknown = sorted(repr(key) for key in raw if key not in _ALLOWED_KEYS)
    if unknown:
        raise ImproperlyConfigured(
            "DJANGO_NINJA_OTEL contains unsupported setting(s): "
            + ", ".join(unknown)
            + "."
        )

    values: dict[str, Any] = {}
    for key in _BOOLEAN_KEYS:
        if key in raw:
            values[key] = _validate_bool(key, raw[key])

    auto_configure = (
        values["auto_configure"]
        if "auto_configure" in values
        else _environment_bool(_ENV_AUTO_CONFIGURE)
    )

    exclude = raw.get("exclude")
    if exclude is not None and not callable(exclude):
        raise ImproperlyConfigured(
            "DJANGO_NINJA_OTEL['exclude'] must be a callable receiving HttpRequest."
        )

    providers: dict[str, Any] = {}
    for key, method in _PROVIDER_KEYS.items():
        provider = raw.get(key)
        if provider is not None and not callable(getattr(provider, method, None)):
            raise ImproperlyConfigured(
                f"DJANGO_NINJA_OTEL[{key!r}] must provide a callable {method}()."
            )
        providers[key] = provider

    return Config(
        tracing=values.get("tracing", True),
        metrics=values.get("metrics", True),
        logs=values.get("logs", True),
        operation_spans=values.get("operation_spans", True),
        http_instrumentation=values.get("http_instrumentation", True),
        auto_configure=auto_configure,
        exclude=exclude,
        tracer_provider=providers["tracer_provider"],
        meter_provider=providers["meter_provider"],
        logger_provider=providers["logger_provider"],
    )


def _validate_bool(key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ImproperlyConfigured(f"DJANGO_NINJA_OTEL[{key!r}] must be a boolean.")
    return value


def _environment_bool(name: str) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return False
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ImproperlyConfigured(
        f"{name} must be one of: true, false (or 1/0, yes/no, on/off)."
    )
