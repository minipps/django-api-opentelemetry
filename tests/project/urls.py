"""Plain-Django URLconf with no optional dependencies."""

from __future__ import annotations

from django.urls import path

from . import views

urlpatterns = [
    path("sync/", views.sync_success, name="sync-success"),
    path("async/", views.async_success, name="async-success"),
    path("error/", views.sync_error, name="sync-error"),
    path("async-error/", views.async_error, name="async-error"),
    path("status/404/", views.status_404, name="status-404"),
    path("status/500/", views.status_500, name="status-500"),
    path("stream/sync/", views.sync_stream, name="sync-stream"),
    path("stream/async/", views.async_stream, name="async-stream"),
    path("file/", views.file_response, name="file-response"),
]
