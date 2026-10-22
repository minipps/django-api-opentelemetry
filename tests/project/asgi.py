"""ASGI entry point for the shared example project.

The coordinator can point the development server at
``tests.project.asgi:application``.  This module intentionally uses plain
Django only, so it also works without the Ninja or SDK extras installed.
"""

from __future__ import annotations

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.project.settings")

from django.core.asgi import get_asgi_application  # noqa: E402

from django_api_opentelemetry import instrument_asgi  # noqa: E402

application = instrument_asgi(get_asgi_application())
