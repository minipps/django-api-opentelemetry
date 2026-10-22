"""Minimal, database-free settings for the shared test project.

The instrumentation app is intentionally absent: T4 fixtures must work before
the installed-app integration exists.  Plain-Django tests use this settings
module, while Ninja-specific tests select ``tests.project.ninja_urls``.
"""

from __future__ import annotations

SECRET_KEY = "test-only-not-a-secret"
DEBUG = False
ALLOWED_HOSTS = ["*"]

# Keep instrumentation activation out of the baseline fixture project.
INSTALLED_APPS: list[str] = []
MIDDLEWARE: list[str] = []

ROOT_URLCONF = "tests.project.urls"
ASGI_APPLICATION = "tests.project.asgi.application"

# No models or migrations are needed.  SQLite in memory is present only so
# Django's test support can initialize a database engine if a future test asks
# pytest-django for one.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
