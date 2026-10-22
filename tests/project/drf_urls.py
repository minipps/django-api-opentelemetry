"""Optional Django REST framework URLconf used by the DRF conformance tests.

The module intentionally imports DRF at import time and is only selected by
tests after ``pytest.importorskip("rest_framework")``.  The shared plain
Django URLconf never imports it, so a missing optional dependency stays a real
missing dependency.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

from django.http import StreamingHttpResponse
from django.urls import include, path
from opentelemetry import trace
from rest_framework import serializers, status, viewsets
from rest_framework.authentication import BaseAuthentication
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.exceptions import (
    APIException,
    AuthenticationFailed,
    NotFound,
    ValidationError,
)
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import BasePermission
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.response import Response
from rest_framework.routers import SimpleRouter
from rest_framework.throttling import BaseThrottle
from rest_framework.views import APIView, exception_handler

#: ``(label, span name, span id)`` observed from application code.  Tests clear
#: this list before each request so they can verify which operation span was
#: current while authentication, validation, handlers and streams ran.
PROBES: list[tuple[str, str, int | None]] = []


def _probe(label: str) -> None:
    span = trace.get_current_span()
    context = span.get_span_context()
    PROBES.append(
        (
            label,
            getattr(span, "name", ""),
            context.span_id if context.is_valid else None,
        )
    )


class ItemSerializer(serializers.Serializer):
    item_id = serializers.IntegerField()
    name = serializers.CharField()


class NestedItemSerializer(serializers.Serializer):
    name = serializers.CharField()
    private_code = serializers.IntegerField(min_value=1)


class NestedRequestSerializer(serializers.Serializer):
    item = NestedItemSerializer()
    tags = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        _probe("serializer")
        return attrs


class ApiView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def get(self, request: Any) -> Response:
        _probe("handler")
        return Response({"family": "api-view"})


class ItemsView(ListAPIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []
    serializer_class = ItemSerializer

    def get_queryset(self) -> list[dict[str, Any]]:
        return [
            {"item_id": 1, "name": "one"},
            {"item_id": 2, "name": "two"},
        ]


class ItemView(RetrieveAPIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []
    serializer_class = ItemSerializer

    def get_object(self) -> dict[str, Any]:
        item_id = int(self.kwargs["pk"])
        return {"item_id": item_id, "name": f"item-{item_id}"}


class ThingViewSet(viewsets.ViewSet):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def list(self, request: Any) -> Response:
        return Response({"things": [1, 2]})

    def create(self, request: Any) -> Response:
        return Response(
            {"created": request.data.get("name")},
            status=status.HTTP_201_CREATED,
        )


@api_view(["GET"])
@authentication_classes([])
@permission_classes([])
def function_view(request: Any) -> Response:
    return Response({"family": "api-view-function"})


class NestedValidationView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def post(self, request: Any) -> Response:
        serializer = NestedRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=status.HTTP_201_CREATED)


class ManualValidationView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def post(self, request: Any) -> Response:
        raise ValidationError(
            {
                "outer": {
                    "private_field": ["secret-value-one"],
                    "other_field": ["secret-value-two"],
                },
                "items": ["secret-value-three"],
            }
        )


class NonRaisingValidationView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def post(self, request: Any) -> Response:
        serializer = NestedRequestSerializer(data=request.data)
        serializer.is_valid()
        return Response(
            {"detail": "invalid", "errors": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )


class NotFoundView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def get(self, request: Any) -> Response:
        raise NotFound("missing private item")


class DenyPermission(BasePermission):
    def has_permission(self, request: Any, view: Any) -> bool:
        _probe("permission")
        return False


class PermissionView(APIView):
    authentication_classes: list[Any] = []
    permission_classes = [DenyPermission]

    def get(self, request: Any) -> Response:
        return Response({"unreachable": True})


class RejectAuthentication(BaseAuthentication):
    def authenticate(self, request: Any) -> None:
        _probe("authenticate")
        raise AuthenticationFailed("bad private credentials")

    def authenticate_header(self, request: Any) -> str:
        # Without a challenge header DRF deliberately converts a failed
        # authentication into 403 instead of 401.
        return "Bearer"


class AuthenticationView(APIView):
    authentication_classes = [RejectAuthentication]
    permission_classes: list[Any] = []

    def get(self, request: Any) -> Response:
        return Response({"unreachable": True})


class DenyThrottle(BaseThrottle):
    def allow_request(self, request: Any, view: Any) -> bool:
        _probe("throttle")
        return False

    def wait(self) -> float:
        return 1.0


class ThrottleView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []
    throttle_classes = [DenyThrottle]

    def get(self, request: Any) -> Response:
        return Response({"unreachable": True})


class CustomAPIException(APIException):
    status_code = 409
    default_detail = "custom conflict"
    default_code = "custom_conflict"


class CustomServerAPIException(APIException):
    status_code = 503
    default_detail = "custom unavailable"
    default_code = "custom_unavailable"


class ApiException409View(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def get(self, request: Any) -> Response:
        raise CustomAPIException()


class ApiException503View(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def get(self, request: Any) -> Response:
        raise CustomServerAPIException()


class UnexpectedView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def get(self, request: Any) -> Response:
        raise RuntimeError("unexpected private failure")


class ConvertedClientError(Exception):
    pass


class ConvertedServerError(Exception):
    pass


class HandlerFailure(Exception):
    pass


def test_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """A DRF ``EXCEPTION_HANDLER`` used to exercise custom handler outcomes."""

    if isinstance(exc, ConvertedClientError):
        return Response({"detail": "converted client"}, status=409)
    if isinstance(exc, ConvertedServerError):
        return Response({"detail": "converted server"}, status=503)
    if isinstance(exc, HandlerFailure):
        raise ValueError("handler replaced the private failure")
    return exception_handler(exc, context)


class Custom409View(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def get(self, request: Any) -> Response:
        raise ConvertedClientError("private client failure")


class Custom503View(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def get(self, request: Any) -> Response:
        raise ConvertedServerError("private server failure")


class CustomRaiseView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def get(self, request: Any) -> Response:
        raise HandlerFailure("private handler input")


class ExplodingRenderer(BaseRenderer):
    media_type = "application/json"
    format = "json"
    charset = None

    def render(
        self,
        data: Any,
        accepted_media_type: str | None = None,
        renderer_context: dict[str, Any] | None = None,
    ) -> bytes:
        _probe("render-failure")
        raise RuntimeError("renderer private failure")


class RenderFailureView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []
    renderer_classes = [ExplodingRenderer]

    def get(self, request: Any) -> Response:
        return Response({"will": "fail"})


class AlreadyRenderedView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def get(self, request: Any) -> Response:
        response = Response({"already": True})
        response.accepted_renderer = JSONRenderer()
        response.accepted_media_type = "application/json"
        response.renderer_context = {
            "request": request,
            "response": response,
            "view": self,
            "format": "json",
            "args": (),
            "kwargs": {},
        }
        response.render()
        return response


def _sync_chunks() -> Iterator[bytes]:
    _probe("stream-1")
    yield b"drf-sync-1\n"
    _probe("stream-2")
    yield b"drf-sync-2\n"


def _failing_sync_chunks() -> Iterator[bytes]:
    _probe("stream-error")
    yield b"drf-sync-error-1\n"
    raise RuntimeError("stream private failure")


async def _async_chunks() -> AsyncIterator[bytes]:
    _probe("stream-async-1")
    yield b"drf-async-1\n"
    _probe("stream-async-2")
    yield b"drf-async-2\n"


class StreamSyncView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def get(self, request: Any) -> StreamingHttpResponse:
        return StreamingHttpResponse(_sync_chunks(), content_type="text/plain")


class StreamFailureView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def get(self, request: Any) -> StreamingHttpResponse:
        return StreamingHttpResponse(_failing_sync_chunks(), content_type="text/plain")


class StreamAsyncView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    def get(self, request: Any) -> StreamingHttpResponse:
        return StreamingHttpResponse(_async_chunks(), content_type="text/plain")


router = SimpleRouter()
router.register("things", ThingViewSet, basename="drf-thing")

urlpatterns = [
    path("drf/api-view/", ApiView.as_view(), name="drf-api-view"),
    path("drf/generic/list/", ItemsView.as_view(), name="drf-generic-list"),
    path(
        "drf/generic/detail/<int:pk>/",
        ItemView.as_view(),
        name="drf-generic-detail",
    ),
    path("drf/function/", function_view, name="drf-function"),
    path(
        "drf/validation/nested/",
        NestedValidationView.as_view(),
        name="drf-validation-nested",
    ),
    path(
        "drf/validation/manual/",
        ManualValidationView.as_view(),
        name="drf-validation-manual",
    ),
    path(
        "drf/validation/non-raising/",
        NonRaisingValidationView.as_view(),
        name="drf-validation-non-raising",
    ),
    path("drf/expected/not-found/", NotFoundView.as_view(), name="drf-not-found"),
    path(
        "drf/expected/permission/",
        PermissionView.as_view(),
        name="drf-permission",
    ),
    path(
        "drf/expected/auth/",
        AuthenticationView.as_view(),
        name="drf-authentication",
    ),
    path(
        "drf/expected/throttled/",
        ThrottleView.as_view(),
        name="drf-throttled",
    ),
    path(
        "drf/expected/api-409/",
        ApiException409View.as_view(),
        name="drf-api-409",
    ),
    path(
        "drf/expected/api-503/",
        ApiException503View.as_view(),
        name="drf-api-503",
    ),
    path("drf/unexpected/", UnexpectedView.as_view(), name="drf-unexpected"),
    path("drf/custom/409/", Custom409View.as_view(), name="drf-custom-409"),
    path("drf/custom/503/", Custom503View.as_view(), name="drf-custom-503"),
    path("drf/custom/raise/", CustomRaiseView.as_view(), name="drf-custom-raise"),
    path(
        "drf/render/failure/",
        RenderFailureView.as_view(),
        name="drf-render-failure",
    ),
    path(
        "drf/render/already/",
        AlreadyRenderedView.as_view(),
        name="drf-render-already",
    ),
    path("drf/stream/sync/", StreamSyncView.as_view(), name="drf-stream-sync"),
    path(
        "drf/stream/failure/",
        StreamFailureView.as_view(),
        name="drf-stream-failure",
    ),
    path("drf/stream/async/", StreamAsyncView.as_view(), name="drf-stream-async"),
    path("drf/viewset/", include(router.urls)),
]
