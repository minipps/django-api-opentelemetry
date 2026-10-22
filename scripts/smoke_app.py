"""Temporary Django + Ninja application used by the built-wheel OTLP smoke test.

This module is deliberately outside ``tests.project``: the smoke service runs it
from a clean wheel-installed environment.  Django settings use package defaults
for tracing, metrics and logs; only exporter configuration comes from the
environment supplied by ``scripts/smoke.py``.
"""

from __future__ import annotations

from django.conf import settings

if not settings.configured:
    settings.configure(
        SECRET_KEY="smoke-test-not-a-secret",
        DEBUG=False,
        ALLOWED_HOSTS=["*"],
        INSTALLED_APPS=["django_api_opentelemetry"],
        MIDDLEWARE=[],
        ROOT_URLCONF=__name__,
        ASGI_APPLICATION=f"{__name__}.application",
        DATABASES={},
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
    )

import django  # noqa: E402

django.setup()

from django.http import HttpRequest, JsonResponse  # noqa: E402
from django.urls import path  # noqa: E402
from ninja import NinjaAPI  # noqa: E402

api = NinjaAPI(urls_namespace="smoke")


def ok(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


def error(request: HttpRequest) -> JsonResponse:
    raise RuntimeError("smoke unhandled error")


@api.get("/items/{item_id}")
def item(request: HttpRequest, item_id: int) -> dict[str, int]:
    return {"item_id": item_id}


urlpatterns = [
    path("ok", ok, name="smoke-ok"),
    path("error", error, name="smoke-error"),
    path("", api.urls),
]

from django.core.asgi import get_asgi_application  # noqa: E402

from django_api_opentelemetry import instrument_asgi  # noqa: E402

application = instrument_asgi(get_asgi_application())
