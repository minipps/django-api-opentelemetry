# django-ninja-opentelemetry

OpenTelemetry traces, metrics and exception logs for Django, with deeper
instrumentation for Django Ninja APIs.

> Tagged releases are validated in CI, then published to PyPI by the project's
> GitHub Actions workflow.

## Features

- One installed-app entry adds HTTP server spans and the
  `http.server.request.duration` and `http.server.active_requests` metrics for
  Django requests, using resolved route templates rather than raw paths.
- Django Ninja input-validation failures produce one warning event with the
  validation count and route. Unexpected exceptions produce one correlated
  error event with type, message and stack trace.
- Ninja operation spans cover input resolution, the bound endpoint and response
  serialization.
- OTLP/HTTP export is opt-in and separate from recording. Existing tracer, meter
  and logger providers remain application-owned.
- The standard OpenTelemetry Django instrumentation is detected so the package
  does not create a second HTTP span or HTTP metric.

The package does **not** forward arbitrary Python application logs and does not
create an access log for every successful request. Its automatic log events are
limited to validation warnings and unexpected request exceptions.

## Installation

The base package supports plain Django without Ninja or the SDK/exporters:

```bash
pip install django-ninja-opentelemetry
```

Add the optional extras when the application uses Django Ninja or the bundled
OTLP export setup:

```bash
pip install "django-ninja-opentelemetry[ninja,otlp]"
```

Supported versions:

- Python 3.12–3.14
- Django 5.2–6.1 (`>=5.2,<6.2`)
- Django Ninja 1.7 (`>=1.7,<1.8`, optional)
- OpenTelemetry API/SDK 1.44–1.x (`>=1.44,<2`)

## Quick start

Add the app. The installed app prepends its middleware when it is not already
present, registers Django's exception signal and activates supported Ninja
hooks:

```python
INSTALLED_APPS = [
    # Existing applications...
    "django_ninja_opentelemetry",
]
```

Configuration is optional:

```python
DJANGO_NINJA_OTEL = {
    "exclude": lambda request: request.path_info == "/health",
}
```

For reliable export flushing on ASGI servers, wrap the Django ASGI application:

```python
# asgi.py
from django.core.asgi import get_asgi_application

from django_ninja_opentelemetry import instrument_asgi

application = instrument_asgi(get_asgi_application())
```

`instrument_asgi()` adds a minimal lifespan wrapper. Native Django ASGI
applications do not implement lifespan, so the wrapper acknowledges startup
without initializing exporters, forwards HTTP and WebSocket scopes unchanged,
and flushes package-owned telemetry when the server sends lifespan shutdown.

The installed app still activates HTTP telemetry and Ninja hooks on its own; the
wrapper only adds a reliable shutdown path. This is recommended because Uvicorn
0.54 re-raises `SIGTERM` after draining the server, which bypasses `atexit`.
For other worker types, call the synchronous `shutdown()` from a worker-exit hook
after requests have drained:

```python
from django_ninja_opentelemetry import shutdown

shutdown()
```

`shutdown()` waits for a bounded time, is safe to call more than once, and only
shuts down resources owned by this package. It does not intercept signals, and
the package cannot guarantee a flush on every possible server-termination path.
Application-owned providers are never closed.

## Configuration

All keys are optional. Invalid key names or value types raise
`django.core.exceptions.ImproperlyConfigured`; an invalid
`DJANGO_NINJA_OTEL_AUTO_CONFIGURE` value raises the same exception.

| Key | Default | Meaning |
| --- | --- | --- |
| `tracing` | `True` | Enable package-created spans, including operation spans. |
| `metrics` | `True` | Enable HTTP measurements. |
| `logs` | `True` | Emit automatic validation and exception log events. |
| `operation_spans` | `True` | Add Ninja operation spans when tracing is enabled. |
| `http_instrumentation` | `True` | Allow package-owned HTTP server spans and metrics. Set `False` when another wrapper owns them. |
| `auto_configure` | Environment value, otherwise `False` | Configure OTLP/HTTP exporters. |
| `exclude` | `None` | Callable receiving `HttpRequest`; true suppresses all package telemetry for that request. |
| `tracer_provider` | `None` | Explicit tracer provider, otherwise the selected global provider. |
| `meter_provider` | `None` | Explicit meter provider, otherwise the selected global provider. |
| `logger_provider` | `None` | Explicit logger provider, otherwise the selected global provider. |

`DJANGO_NINJA_OTEL_AUTO_CONFIGURE=true` enables OTLP exporter setup unless the
Django setting sets `auto_configure` explicitly. Export setup is deliberately
opt-in: recording telemetry and deciding where it is sent are separate concerns,
matching the upstream FastAPI 0.143 change.

```bash
export DJANGO_NINJA_OTEL_AUTO_CONFIGURE=true
export OTEL_SERVICE_NAME=my-api
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

Inside Compose, use the collector service address
`http://collector:4318` instead of `localhost`.

Exporter behavior:

- With no endpoint configured, the package installs no OTLP exporter and does
  not guess a default endpoint.
- A base endpoint gains the signal path (`/v1/traces`, `/v1/metrics`,
  `/v1/logs`); a signal-specific endpoint is used as written.
- Only the OTLP HTTP/protobuf protocol is supported.
- `OTEL_TRACES_EXPORTER`, `OTEL_METRICS_EXPORTER` or `OTEL_LOGS_EXPORTER` set to
  `none` disables that signal; other non-OTLP values are skipped with a warning.
- `OTEL_SDK_DISABLED=true` disables package telemetry.
- Standard endpoint, header, timeout and resource environment variables are
  delegated to the official OpenTelemetry exporters.

### Providers and coexistence

Explicit providers in `DJANGO_NINJA_OTEL` take precedence. Otherwise the package
uses the global providers. With `auto_configure` enabled, it adds processors,
readers or exporters to an existing provider when that provider supports
registration, or installs an SDK provider when the global provider is still
unconfigured. Only package-owned components and providers are flushed and shut
down.

If the standard OpenTelemetry Django middleware is installed, the package detects
it and leaves HTTP spans and metrics to that integration while retaining Ninja
operation spans, validation warnings and exception events. For other HTTP
wrappers, set `http_instrumentation=False`; do not assume detection of every
vendor wrapper.

## Recording boundaries and privacy

With the default prepended middleware position, a buffered response is finished
after response middleware completes. For streaming responses, it is finished at
iterator exhaustion, failure or close. These are Django-observable boundaries,
not FastAPI's ASGI `send` boundary.

Validation warnings contain only `django_ninja.validation.error_count` and the
resolved `http.route`; invalid input is not serialized into the event. Unexpected
exception events can contain the exception message and stack trace. Redact
sensitive data at the OpenTelemetry processor/exporter layer or set
`logs=False` if those values must not leave the process. Error logs are emitted
independently of trace sampling.

## Development in Docker

Docker Engine/Desktop with Compose v2 and BuildKit is the only host requirement.
No host Python, `uv`, Ruff or ty installation is needed. Dependencies live in
`/opt/venv`; the checkout is mounted at `/workspace`.

Build the development image with host-matching ownership:

```bash
docker compose build \
  --build-arg LOCAL_UID="$(id -u)" \
  --build-arg LOCAL_GID="$(id -g)" dev
```

Start the demo server on `127.0.0.1:8000` only:

```bash
docker compose up dev
```

The demo command selects `tests.project.demo_settings` and runs the shared ASGI
project with reload enabled. `tests.project.asgi` already wraps the Django
application with `instrument_asgi(get_asgi_application())`, so the demo exercises
the lifespan shutdown path. Ordinary pytest runs use the baseline
`tests.project.settings`, which deliberately keeps the package out of
`INSTALLED_APPS`, so plain-Django tests remain a real condition. The demo
settings are not applied to pytest.

Run tests and checks:

```bash
docker compose run --rm dev python -m pytest
docker compose run --rm dev bash scripts/check.sh all
docker compose run --rm dev python -m ruff check .
docker compose run --rm dev python -m ruff format --check .
docker compose run --rm dev ty check --python /opt/venv src
```

`scripts/check.sh all` runs Ruff lint, Ruff format check, ty and pytest. The image
is built with a locked `uv sync` for the `ninja`/`otlp` extras and development
group. Rebuild after changing `pyproject.toml` or `uv.lock`; normal test commands
do not resync dependencies. Compatibility jobs build isolated environments with
`scripts/compat.sh` rather than modifying the committed lockfile. Final local
verification passed Ruff, format, ty and 227 tests in the default environment,
and the nine Python/Django matrix jobs plus four compatibility jobs. See
[task status](docs/tasks.md) for the evidence.

Build and verify distributions:

```bash
docker build --target dist --output dist .
docker compose run --rm dev python scripts/check-dist.py --self-test
docker compose run --rm dev python scripts/check-dist.py dist
```

The `dist` target exports exactly the wheel and sdist built inside the image.
`check-dist.py` runs `twine check`, installs the wheel into a clean temporary
environment outside `/workspace`, verifies the import resolves to the installed
distribution, rebuilds a wheel from the sdist and repeats the import check. The
directory must contain exactly one wheel and one sdist; remove stale artifacts
first.

Run the collector smoke test against the built wheel:

```bash
docker compose --profile telemetry down -v
docker compose --profile telemetry up -d --wait collector
docker compose --profile telemetry run --rm smoke
docker compose --profile telemetry down -v
```

The `telemetry` profile runs a one-shot volume initializer, then a non-root
collector writing all three signals to a shared volume. The smoke service sees
that output read-only, installs the wheel from `./dist` into a fresh temporary
virtual environment, starts Uvicorn and requests a success route, a Ninja
validation failure and an uncaught error. It asserts all three signals, trace/log
correlation, validation-input omission and both HTTP metrics.

The smoke check requires a fresh collector volume. If it reports stale output,
run `docker compose --profile telemetry down -v` before starting the collector
again. A repeated smoke run without that reset is expected to fail rather than
reuse old records. Collector diagnostics are available with:

```bash
docker compose --profile telemetry logs collector
```

See [development and CI](docs/development-and-ci.md) for the full container,
compatibility-matrix and release workflow, and [task status](docs/tasks.md) for
current verification evidence.

## Deliberate limits

The first release does not include DRF, Channels/WebSockets, background-job
instrumentation, automatic forwarding of application logs, per-chunk spans or a
dashboard stack. Ninja hooks require Ninja 1.7 and are disabled with one
diagnostic on an unsupported version while ordinary Django telemetry continues.
