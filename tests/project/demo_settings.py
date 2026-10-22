"""Django settings for the instrumented Compose development server.

The baseline ``tests.project.settings`` stays uninstrumented so ordinary tests
can assert plain-Django behavior. This module opts into the package app and the
Ninja URLconf for manual and Compose demos.
"""

from __future__ import annotations

from .settings import *  # noqa: F403

DEBUG = False
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
SECRET_KEY = "demo-test-only-not-a-secret"

INSTALLED_APPS = ["django_ninja_opentelemetry"]
ROOT_URLCONF = "tests.project.ninja_urls"
