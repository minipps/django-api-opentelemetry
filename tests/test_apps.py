"""Installed-app activation, idempotence and plain-Django startup checks."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured

import django_ninja_opentelemetry
from django_ninja_opentelemetry.apps import MIDDLEWARE_PATH, TelemetryConfig


@pytest.fixture
def app_config() -> TelemetryConfig:
    return TelemetryConfig("django_ninja_opentelemetry", django_ninja_opentelemetry)


def _receiver_count() -> int:
    from django.core.signals import got_request_exception

    from django_ninja_opentelemetry.middleware import observe_django_exception

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
    config = TelemetryConfig.create("django_ninja_opentelemetry")

    assert type(config) is TelemetryConfig
    assert config.name == "django_ninja_opentelemetry"


def test_apps_import_does_not_load_activation_modules() -> None:
    result = _run_python(
        textwrap.dedent(
            """
            import sys

            import django_ninja_opentelemetry.apps

            loaded = (
                "django_ninja_opentelemetry._ninja",
                "django_ninja_opentelemetry._telemetry",
                "django_ninja_opentelemetry.middleware",
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
                INSTALLED_APPS=["django_ninja_opentelemetry"],
                MIDDLEWARE=[],
                DATABASES={},
                USE_TZ=True,
            )

            before = threading.active_count()
            import django

            django.setup()
            after = threading.active_count()

            from django.apps import apps

            from django_ninja_opentelemetry._config import get_config
            from django_ninja_opentelemetry.apps import TelemetryConfig

            middleware = (
                "django_ninja_opentelemetry.middleware.TelemetryMiddleware"
            )
            assert settings.MIDDLEWARE == [middleware], settings.MIDDLEWARE
            assert isinstance(
                apps.get_app_config("django_ninja_opentelemetry"), TelemetryConfig
            )
            assert get_config().tracing is True
            assert after == before, (before, after)
            assert "django_ninja_opentelemetry.middleware" in sys.modules

            forbidden = (
                "ninja",
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

    assert demo_settings.INSTALLED_APPS == ["django_ninja_opentelemetry"]
    assert demo_settings.ROOT_URLCONF == "tests.project.ninja_urls"
    assert demo_settings.DEBUG is False
    assert set(demo_settings.ALLOWED_HOSTS) >= {"localhost", "127.0.0.1", "testserver"}


def test_baseline_settings_remain_uninstrumented() -> None:
    from tests.project import settings as baseline

    assert "django_ninja_opentelemetry" not in baseline.INSTALLED_APPS
    assert baseline.MIDDLEWARE == []
