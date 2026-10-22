"""Installed-app activation, idempotence and plain-Django startup checks."""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured

import django_api_opentelemetry
from django_api_opentelemetry.apps import MIDDLEWARE_PATH, TelemetryConfig


@pytest.fixture
def app_config() -> TelemetryConfig:
    return TelemetryConfig("django_api_opentelemetry", django_api_opentelemetry)


def _receiver_count() -> int:
    from django.core.signals import got_request_exception

    from django_api_opentelemetry.middleware import observe_django_exception

    live = got_request_exception._live_receivers(None)
    receivers = [*live[0], *live[1]] if isinstance(live, tuple) else live
    return sum(receiver is observe_django_exception for receiver in receivers)


def _run_python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_app_config_is_discovered() -> None:
    config = TelemetryConfig.create("django_api_opentelemetry")

    assert type(config) is TelemetryConfig
    assert config.name == "django_api_opentelemetry"


def test_apps_import_does_not_load_activation_modules() -> None:
    result = _run_python(
        textwrap.dedent(
            """
            import sys

            import django_api_opentelemetry.apps

            loaded = (
                "django_api_opentelemetry._drf",
                "django_api_opentelemetry._ninja",
                "django_api_opentelemetry._telemetry",
                "django_api_opentelemetry.middleware",
            )
            assert not any(name in sys.modules for name in loaded), loaded
            print("import-ok")
            """
        )
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "import-ok"


def test_ready_prepends_middleware_for_list(
    app_config: TelemetryConfig, settings: Any
) -> None:
    settings.MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]

    app_config.ready()

    assert settings.MIDDLEWARE == [
        MIDDLEWARE_PATH,
        "django.middleware.common.CommonMiddleware",
    ]
    assert isinstance(settings.MIDDLEWARE, list)


def test_ready_preserves_tuple_type(app_config: TelemetryConfig, settings: Any) -> None:
    settings.MIDDLEWARE = ("django.middleware.common.CommonMiddleware",)

    app_config.ready()

    assert settings.MIDDLEWARE == (
        MIDDLEWARE_PATH,
        "django.middleware.common.CommonMiddleware",
    )
    assert isinstance(settings.MIDDLEWARE, tuple)


def test_ready_preserves_explicit_middleware_position(
    app_config: TelemetryConfig, settings: Any
) -> None:
    current = ["first.Middleware", MIDDLEWARE_PATH, "last.Middleware"]
    settings.MIDDLEWARE = list(current)

    app_config.ready()

    assert settings.MIDDLEWARE == current


def test_repeated_ready_is_safe_and_deduplicates_signal(
    app_config: TelemetryConfig, settings: Any
) -> None:
    settings.MIDDLEWARE = []

    app_config.ready()
    app_config.ready()

    assert settings.MIDDLEWARE == [MIDDLEWARE_PATH]
    assert _receiver_count() == 1

    # Simulate a test settings reset between ready() calls.
    settings.MIDDLEWARE = []
    app_config.ready()

    assert settings.MIDDLEWARE == [MIDDLEWARE_PATH]
    assert _receiver_count() == 1


def test_ready_rejects_invalid_settings_before_side_effects(
    app_config: TelemetryConfig, settings: Any
) -> None:
    settings.MIDDLEWARE = ["existing.Middleware"]
    settings.DJANGO_NINJA_OTEL = {"metrics": "yes"}

    with pytest.raises(ImproperlyConfigured):
        app_config.ready()

    assert settings.MIDDLEWARE == ["existing.Middleware"]


def test_ready_installs_actual_ninja_hooks_once(
    app_config: TelemetryConfig, settings: Any
) -> None:
    pytest.importorskip("ninja")

    from ninja.main import NinjaAPI

    settings.MIDDLEWARE = []
    app_config.ready()
    patched = NinjaAPI.on_exception

    app_config.ready()

    assert NinjaAPI.on_exception is patched
    assert getattr(patched, "_django_ninja_otel_patched", False)


def test_ready_installs_actual_drf_hooks_once(
    app_config: TelemetryConfig, settings: Any
) -> None:
    pytest.importorskip("rest_framework")

    from rest_framework.response import Response
    from rest_framework.views import APIView

    settings.MIDDLEWARE = []
    app_config.ready()
    patched = (
        APIView.dispatch,
        APIView.initial,
        APIView.handle_exception,
        Response.render,
    )

    app_config.ready()

    assert (
        APIView.dispatch,
        APIView.initial,
        APIView.handle_exception,
        Response.render,
    ) == patched
    assert all(
        getattr(function, "_django_drf_otel_patched", False) for function in patched
    )


def _reset_drf_install(
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    from django_api_opentelemetry import _drf

    monkeypatch.setattr(_drf, "_install_attempted", False)
    monkeypatch.setattr(_drf, "_warned", set())
    monkeypatch.setattr(_drf, "_warning_lock", threading.Lock())
    return _drf


def test_drf_install_is_silent_without_distribution(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    drf = _reset_drf_install(monkeypatch)
    monkeypatch.setattr(drf, "_distribution_version", lambda: None)

    with caplog.at_level(logging.WARNING, logger=drf.__name__):
        drf.install()

    assert caplog.records == []
    assert drf._install_attempted is True


def test_drf_install_warns_once_for_unsupported_version(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    drf = _reset_drf_install(monkeypatch)
    monkeypatch.setattr(drf, "_distribution_version", lambda: "3.19.0")

    with caplog.at_level(logging.WARNING, logger=drf.__name__):
        drf.install()
        drf.install()

    assert len(caplog.records) == 1
    assert "unsupported version" in caplog.records[0].message


@pytest.mark.parametrize(
    ("installed", "expected"),
    [
        ("3.16.1", True),
        ("3.17.2", True),
        ("3.18.3", True),
        ("3.15.9", False),
        ("3.19.0", False),
        ("4.0.0", False),
        ("unknown", False),
    ],
)
def test_drf_version_support(installed: str, expected: bool) -> None:
    from django_api_opentelemetry import _drf

    assert _drf._version_supported(installed) is expected


def test_drf_install_leaves_hooks_untouched_on_signature_mismatch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pytest.importorskip("rest_framework")

    from rest_framework.response import Response
    from rest_framework.views import APIView

    drf = _reset_drf_install(monkeypatch)
    monkeypatch.setattr(drf, "_distribution_version", lambda: "3.18.3")
    monkeypatch.setattr(drf, "_signature_matches", lambda *args: False)
    before = (
        APIView.dispatch,
        APIView.initial,
        APIView.handle_exception,
        Response.render,
    )

    with caplog.at_level(logging.WARNING, logger=drf.__name__):
        drf.install()

    assert (
        APIView.dispatch,
        APIView.initial,
        APIView.handle_exception,
        Response.render,
    ) == before
    assert len(caplog.records) == 1
    assert "unexpected hook signature" in caplog.records[0].message


def test_drf_validation_leaf_count_uses_nested_scalar_leaves() -> None:
    from django_api_opentelemetry import _drf

    class DetailError(Exception):
        def __init__(self, detail: Any) -> None:
            self.detail = detail

    assert (
        _drf._validation_leaf_count(
            DetailError(
                {
                    "name": ["required"],
                    "nested": {"value": [1, {"deep": True}]},
                    "empty": [],
                }
            )
        )
        == 3
    )
    assert _drf._validation_leaf_count(DetailError([])) == 0
    assert _drf._validation_leaf_count(DetailError(None)) == 1
    assert _drf._validation_leaf_count(RuntimeError("no detail")) == 0

    deeply_nested: Any = "leaf"
    for _ in range(256):
        deeply_nested = {"value": deeply_nested}
    assert _drf._validation_leaf_count(DetailError(deeply_nested)) == 1


def test_plain_django_startup_without_optional_imports() -> None:
    result = _run_python(
        textwrap.dedent(
            """
            import os
            import sys
            import threading

            os.environ.pop("DJANGO_SETTINGS_MODULE", None)


            class BlockOptionalImports:
                def find_spec(self, fullname, path=None, target=None):
                    blocked = (
                        fullname == "ninja"
                        or fullname.startswith("ninja.")
                        or fullname == "rest_framework"
                        or fullname.startswith("rest_framework.")
                        or fullname == "opentelemetry.sdk"
                        or fullname.startswith("opentelemetry.sdk.")
                        or fullname == "opentelemetry.exporter"
                        or fullname.startswith("opentelemetry.exporter.")
                    )
                    if blocked:
                        raise ModuleNotFoundError(fullname)
                    return None


            sys.meta_path.insert(0, BlockOptionalImports())

            from django.conf import settings

            settings.configure(
                SECRET_KEY="plain-django-test-only",
                DEBUG=False,
                ALLOWED_HOSTS=["testserver"],
                INSTALLED_APPS=["django_api_opentelemetry"],
                MIDDLEWARE=[],
                DATABASES={},
                USE_TZ=True,
            )

            before = threading.active_count()
            import django

            django.setup()
            after = threading.active_count()

            from django.apps import apps

            from django_api_opentelemetry._config import get_config
            from django_api_opentelemetry.apps import TelemetryConfig

            middleware = (
                "django_api_opentelemetry.middleware.TelemetryMiddleware"
            )
            assert settings.MIDDLEWARE == [middleware], settings.MIDDLEWARE
            assert isinstance(
                apps.get_app_config("django_api_opentelemetry"), TelemetryConfig
            )
            assert get_config().tracing is True
            assert after == before, (before, after)
            assert "django_api_opentelemetry.middleware" in sys.modules

            forbidden = (
                "ninja",
                "rest_framework",
                "opentelemetry.sdk",
                "opentelemetry.exporter",
            )
            loaded = [
                name
                for name in sys.modules
                if any(
                    name == prefix or name.startswith(prefix + ".")
                    for prefix in forbidden
                )
            ]
            assert not loaded, loaded
            print("plain-django-ok")
            """
        )
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "plain-django-ok"


def test_demo_settings_are_instrumented() -> None:
    from tests.project import demo_settings

    assert demo_settings.INSTALLED_APPS == ["django_api_opentelemetry"]
    assert demo_settings.ROOT_URLCONF == "tests.project.ninja_urls"
    assert demo_settings.DEBUG is False
    assert set(demo_settings.ALLOWED_HOSTS) >= {"localhost", "127.0.0.1", "testserver"}


def test_baseline_settings_remain_uninstrumented() -> None:
    from tests.project import settings as baseline

    assert "django_api_opentelemetry" not in baseline.INSTALLED_APPS
    assert baseline.MIDDLEWARE == []
