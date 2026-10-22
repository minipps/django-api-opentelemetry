# django-ninja-opentelemetry

OpenTelemetry traces, metrics and exception logs for Django, with deeper
instrumentation for Django Ninja APIs.

> **Status:** foundation work in progress. Package configuration and the Docker
> development environment exist; request instrumentation is not implemented yet.

## Installation

The base package supports plain Django:

```bash
pip install django-ninja-opentelemetry
```

Add the optional extras when the application uses Django Ninja or the bundled
OTLP exporter setup:

```bash
pip install "django-ninja-opentelemetry[ninja,otlp]"
```

Supported versions:

- Python 3.12–3.14
- Django 5.2–6.1 (`>=5.2,<6.2`)
- Django Ninja 1.7 (`>=1.7,<1.8`, optional)
- OpenTelemetry API/SDK 1.44–1.x (`>=1.44,<2`)

## Configuration

Add the installed app:

```python
INSTALLED_APPS = [
    # Existing applications...
    "django_ninja_opentelemetry",
]
```

Configure the package with one optional Django setting:

| Key | Default | Meaning |
| --- | --- | --- |
| `tracing` | `True` | Enable package-created spans, including operation spans. |
| `metrics` | `True` | Enable HTTP measurements. |
| `logs` | `True` | Emit automatic validation and exception logs. |
| `operation_spans` | `True` | Add Ninja operation spans when tracing is enabled. |
| `http_instrumentation` | `True` | Allow package-owned HTTP server spans and metrics. |
| `auto_configure` | Environment value, otherwise `False` | Configure OTLP exporters. |
| `exclude` | `None` | Callable receiving `HttpRequest`; true suppresses package telemetry. |
| `tracer_provider` | `None` | Explicit tracer provider, otherwise the global provider. |
| `meter_provider` | `None` | Explicit meter provider, otherwise the global provider. |
| `logger_provider` | `None` | Explicit logger provider, otherwise the global provider. |

```python
DJANGO_NINJA_OTEL = {
    "exclude": lambda request: request.path_info == "/health",
}
```

`DJANGO_NINJA_OTEL_AUTO_CONFIGURE=true` enables OTLP exporter setup unless the
Django setting sets `auto_configure` explicitly. Standard OpenTelemetry
environment variables such as `OTEL_SERVICE_NAME`,
`OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_SDK_DISABLED` remain authoritative at
runtime. Invalid `DJANGO_NINJA_OTEL` keys or value types raise
`django.core.exceptions.ImproperlyConfigured`.

## Development in Docker

Docker Engine/Desktop with Compose v2 and BuildKit is the only host requirement.
Dependencies live in `/opt/venv`; the source tree is mounted at `/workspace`.

```bash
docker compose build --build-arg LOCAL_UID="$(id -u)" --build-arg LOCAL_GID="$(id -g)" dev
docker compose run --rm dev python -m pytest
docker compose run --rm dev python -m ruff check .
docker compose run --rm dev python -m ruff format --check .
docker compose run --rm dev ty check src
docker compose run --rm dev python -m build --no-isolation
```

The `dev` service sleeps by default until shared test/demo fixtures are added.
The Dockerfile also provides `ci` (source copied into the image, pytest entry
point) and `dist` (exports the wheel and sdist):

```bash
docker build --target dist --output dist .
```

The Python base version is selected with the `PYTHON_VERSION` build argument
(default `3.14`); uv is pinned with `UV_VERSION`. Rebuild the image after
changing dependencies.
