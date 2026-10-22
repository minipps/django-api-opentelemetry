"""Installed-app activation for Django telemetry."""

from __future__ import annotations

from collections.abc import Sequence

from django.apps import AppConfig

MIDDLEWARE_PATH = "django_api_opentelemetry.middleware.TelemetryMiddleware"
_EXCEPTION_DISPATCH_UID = "django_api_opentelemetry.observe_django_exception"


class TelemetryConfig(AppConfig):
    """Validate configuration and activate HTTP, signal and Ninja hooks."""

    name = "django_api_opentelemetry"

    def ready(self) -> None:
        # Keep application import time free of Django settings, middleware,
        # telemetry and optional framework modules. Everything optional is
        # loaded here.
        from django.conf import settings
        from django.core.signals import got_request_exception

        from ._config import get_config
        from ._drf import install as install_drf
        from ._ninja import install
        from .middleware import observe_django_exception

        get_config()

        current = getattr(settings, "MIDDLEWARE", None)
        updated = _insert_middleware(current)
        if updated is not current:
            settings.MIDDLEWARE = updated

        got_request_exception.connect(
            observe_django_exception,
            dispatch_uid=_EXCEPTION_DISPATCH_UID,
        )
        install()
        install_drf()


def _insert_middleware(current: Sequence[str] | None) -> Sequence[str]:
    entries = list(current or ())
    if MIDDLEWARE_PATH in entries:
        return current if current is not None else entries
    entries.insert(0, MIDDLEWARE_PATH)
    return tuple(entries) if isinstance(current, tuple) else entries
