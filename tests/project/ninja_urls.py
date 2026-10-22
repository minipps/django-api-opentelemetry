"""Optional URLconf that adds the Django Ninja API.

Tests select this module explicitly so the plain URLconf stays free of Ninja.
"""

from __future__ import annotations

from django.urls import path

from .ninja_api import api

urlpatterns = [
    path("ninja/", api.urls),
]
