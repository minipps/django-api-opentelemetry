from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from django_api_opentelemetry._config import Config, get_config

if not settings.configured:
    settings.configure()


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DJANGO_NINJA_OTEL_AUTO_CONFIGURE", raising=False)
    if hasattr(settings, "DJANGO_NINJA_OTEL"):
        monkeypatch.delattr(settings, "DJANGO_NINJA_OTEL")


def set_config(value: Any) -> None:
    settings.DJANGO_NINJA_OTEL = value


def test_config_fields_and_defaults() -> None:
    config = get_config()

    assert [field.name for field in dataclasses.fields(Config)] == [
        "tracing",
        "metrics",
        "logs",
        "operation_spans",
        "http_instrumentation",
        "auto_configure",
        "exclude",
        "tracer_provider",
        "meter_provider",
        "logger_provider",
    ]
    assert config == Config()
    assert config.tracing is True
    assert config.metrics is True
    assert config.logs is True
    assert config.operation_spans is True
    assert config.http_instrumentation is True
    assert config.auto_configure is False
    assert config.exclude is None
    assert config.tracer_provider is None
    assert config.meter_provider is None
    assert config.logger_provider is None


def test_explicit_booleans_and_exclude() -> None:
    def exclude(request: Any) -> bool:
        return request.path_info == "/health"

    set_config(
        {
            "tracing": False,
            "metrics": False,
            "logs": False,
            "operation_spans": False,
            "http_instrumentation": False,
            "exclude": exclude,
        }
    )

    config = get_config()

    assert config.tracing is False
    assert config.metrics is False
    assert config.logs is False
    assert config.operation_spans is False
    assert config.http_instrumentation is False
    assert config.exclude is exclude


def test_auto_configure_environment_and_explicit_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DJANGO_NINJA_OTEL_AUTO_CONFIGURE", "true")
    assert get_config().auto_configure is True

    set_config({"auto_configure": False})
    assert get_config().auto_configure is False

    monkeypatch.setenv("DJANGO_NINJA_OTEL_AUTO_CONFIGURE", "false")
    set_config({"auto_configure": True})
    assert get_config().auto_configure is True


def test_explicit_auto_configure_ignores_invalid_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DJANGO_NINJA_OTEL_AUTO_CONFIGURE", "sometimes")
    set_config({"auto_configure": False})

    assert get_config().auto_configure is False


@pytest.mark.parametrize("value", ["yes", "1", "ON", " true "])
def test_auto_configure_accepts_true_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DJANGO_NINJA_OTEL_AUTO_CONFIGURE", value)

    assert get_config().auto_configure is True


@pytest.mark.parametrize("value", ["no", "0", "OFF", "false"])
def test_auto_configure_accepts_false_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DJANGO_NINJA_OTEL_AUTO_CONFIGURE", value)

    assert get_config().auto_configure is False


def test_invalid_auto_configure_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DJANGO_NINJA_OTEL_AUTO_CONFIGURE", "sometimes")

    with pytest.raises(ImproperlyConfigured, match="DJANGO_NINJA_OTEL_AUTO_CONFIGURE"):
        get_config()


def test_non_string_setting_names_are_rejected() -> None:
    set_config({1: True, "unknown": False})
    with pytest.raises(ImproperlyConfigured, match="unsupported setting"):
        get_config()


def test_django_setting_must_be_a_mapping() -> None:
    set_config(["tracing"])

    with pytest.raises(ImproperlyConfigured, match="must be a mapping"):
        get_config()


def test_unknown_setting_is_rejected() -> None:
    set_config({"tracing": True, "unknown": True})

    with pytest.raises(ImproperlyConfigured, match="'unknown'"):
        get_config()


@pytest.mark.parametrize(
    "key",
    [
        "tracing",
        "metrics",
        "logs",
        "operation_spans",
        "http_instrumentation",
        "auto_configure",
    ],
)
def test_boolean_settings_reject_non_booleans(key: str) -> None:
    set_config({key: 1})

    with pytest.raises(ImproperlyConfigured, match=rf"\[{key!r}\] must be a boolean"):
        get_config()


def test_exclude_must_be_callable() -> None:
    set_config({"exclude": "/health"})

    with pytest.raises(
        ImproperlyConfigured, match=r"DJANGO_NINJA_OTEL\['exclude'\] must be a callable"
    ):
        get_config()


def test_api_provider_duck_contract_is_accepted() -> None:
    class Provider:
        def get_tracer(self, *args: Any, **kwargs: Any) -> None:
            pass

        def get_meter(self, *args: Any, **kwargs: Any) -> None:
            pass

        def get_logger(self, *args: Any, **kwargs: Any) -> None:
            pass

    provider = Provider()
    set_config(
        {
            "tracer_provider": provider,
            "meter_provider": provider,
            "logger_provider": provider,
        }
    )

    config = get_config()

    assert config.tracer_provider is provider
    assert config.meter_provider is provider
    assert config.logger_provider is provider


@pytest.mark.parametrize(
    ("key", "method"),
    [
        ("tracer_provider", "get_tracer"),
        ("meter_provider", "get_meter"),
        ("logger_provider", "get_logger"),
    ],
)
def test_provider_requires_its_api_method(key: str, method: str) -> None:
    set_config({key: object()})

    with pytest.raises(
        ImproperlyConfigured, match=rf"must provide a callable {method}"
    ):
        get_config()
