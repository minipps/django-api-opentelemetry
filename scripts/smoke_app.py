"""Temporary Django + Ninja + DRF application used by the built-wheel smoke test.

This module is deliberately outside ``tests.project``: the smoke service runs it
from a clean wheel-installed environment.  Django settings use package defaults
for tracing, metrics and logs; only exporter configuration comes from the
environment supplied by ``scripts/smoke.py``.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

if not settings.configured:
    settings.configure(
        SECRET_KEY="smoke-test-not-a-secret",
        DEBUG=False,
        ALLOWED_HOSTS=["*"],
        INSTALLED_APPS=["django_api_opentelemetry", "rest_framework"],
        MIDDLEWARE=[],
        ROOT_URLCONF=__name__,
        ASGI_APPLICATION=f"{__name__}.application",
        DATABASES={},
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        REST_FRAMEWORK={"UNAUTHENTICATED_USER": None},
    )

import django  # noqa: E402

django.setup()

from django.http import HttpRequest, JsonResponse  # noqa: E402
from django.urls import path  # noqa: E402
from ninja import NinjaAPI  # noqa: E402
from rest_framework import serializers  # noqa: E402
from rest_framework.response import Response  # noqa: E402
from rest_framework.views import APIView  # noqa: E402

api = NinjaAPI(urls_namespace="smoke")


def ok(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


def error(request: HttpRequest) -> JsonResponse:
    raise RuntimeError("smoke unhandled error")


@api.get("/items/{item_id}")
def item(request: HttpRequest, item_id: int) -> dict[str, int]:
    return {"item_id": item_id}


class SmokeItemSerializer(serializers.Serializer):
    item_id = serializers.IntegerField()


class DrfOkView(APIView):
    def get(self, request: Any) -> Response:
        return Response({"status": "ok"})


class DrfItemView(APIView):
    def post(self, request: Any) -> Response:
        serializer = SmokeItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response({"item_id": serializer.validated_data["item_id"]})


class DrfErrorView(APIView):
    def get(self, request: Any) -> Response:
        raise RuntimeError("smoke unhandled DRF error")


urlpatterns = [
    path("ok", ok, name="smoke-ok"),
    path("error", error, name="smoke-error"),
    path("drf/ok", DrfOkView.as_view(), name="smoke-drf-ok"),
    path("drf/items", DrfItemView.as_view(), name="smoke-drf-items"),
    path("drf/error", DrfErrorView.as_view(), name="smoke-drf-error"),
    path("", api.urls),
]

from django.core.asgi import get_asgi_application  # noqa: E402

from django_api_opentelemetry import instrument_asgi  # noqa: E402

application = instrument_asgi(get_asgi_application())
