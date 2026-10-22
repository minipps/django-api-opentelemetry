# django-api-opentelemetry

OpenTelemetry traces, metrics and exception logs for Django, with deeper
instrumentation for Django Ninja and Django REST framework APIs.

> Tagged releases are validated in CI, then published to PyPI by the project's
> GitHub Actions workflow.

## Features

- One installed-app entry adds HTTP server spans and the
  `http.server.request.duration` and `http.server.active_requests` metrics for
  Django requests, using resolved route templates rather than raw paths.
- Django Ninja and Django REST framework input-validation failures produce one
  warning event with the validation count and route. Unexpected exceptions
  produce one correlated error event with type, message and stack trace.
- Ninja operation spans cover input resolution, the bound endpoint and response
  serialization. DRF operation spans cover dispatch, initial request handling
  and response rendering.
- OTLP/HTTP export is opt-in and separate from recording. Existing tracer, meter
  and logger providers remain application-owned.
- The standard OpenTelemetry Django instrumentation is detected so the package
  does not create a second HTTP span or HTTP metric.

The package does **not** forward arbitrary Python application logs and does not
create an access log for every successful request. Its automatic log events are
limited to validation warnings and unexpected request exceptions.

## Installation

The base package supports plain Django without Ninja, DRF or the SDK/exporters:

```bash
pip install django-api-opentelemetry
```

Add the optional extras when the application uses Django Ninja, Django REST
framework or the bundled OTLP export setup:

```bash
pip install "django-api-opentelemetry[ninja,drf,otlp]"
```

Supported versions:

- Python 3.12–3.14
- Django 5.2–6.1 (`>=5.2,<6.2`)
- Django Ninja 1.7 (`>=1.7,<1.8`, optional)
- Django REST framework 3.16–3.18 (`>=3.16,<3.19`, optional)
- OpenTelemetry API/SDK 1.44–1.x (`>=1.44,<2`)

The development image installs both framework extras from the lockfile and
uses DRF 3.18.3 by default. Compatibility jobs exercise DRF 3.16.1 and 3.17.2
on Django 5.2.

> DRF support is currently unreleased on the `feature/drf-support` branch. It is
> not included in the published `v0.1.0` package; the `drf` extra and examples
> apply after a release that contains this change.

## Quick start

Add the app. The installed app prepends its middleware when it is not already
present, registers Django's exception signal and activates supported Ninja and
DRF hooks:

```python
INSTALLED_APPS = [
    # Existing applications...
    "django_api_opentelemetry",
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

from django_api_opentelemetry import instrument_asgi

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
from django_api_opentelemetry import shutdown

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
| `operation_spans` | `True` | Add Ninja and DRF operation spans when tracing is enabled. |
| `http_instrumentation` | `True` | Allow package-owned HTTP server spans and metrics. Set `False` when another wrapper owns them. |
| `auto_configure` | Environment value, otherwise `False` | Configure OTLP/HTTP exporters. |
| `exclude` | `None` | Callable receiving `HttpRequest`; true suppresses all package telemetry for that request. |
| `tracer_provider` | `None` | Explicit tracer provider, otherwise the selected global provider. |
| `meter_provider` | `None` | Explicit meter provider, otherwise the selected global provider. |
| `logger_provider` | `None` | Explicit logger provider, otherwise the selected global provider. |

Ninja and DRF use this same setting; there are no framework-specific keys,
adapters or required framework dependencies. `tracing`, `metrics`, `logs`,
`operation_spans`, `http_instrumentation`, `exclude` and the three provider
options apply to both integrations. Missing DRF or Ninja is a silent no-op.
Unsupported framework versions or hook signatures produce one diagnostic and
disable only that framework's hooks; ordinary Django HTTP telemetry continues.

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
and DRF operation spans, validation warnings and exception events. For other HTTP
wrappers, set `http_instrumentation=False`; do not assume detection of every
vendor wrapper.

### Django REST framework

Install the optional extra and keep the existing installed-app entry:

```bash
pip install "django-api-opentelemetry[drf]"
```

No DRF settings, view decorators or replacement base classes are required.
Instrumentation activates lazily in `TelemetryConfig.ready()` and is idempotent.
Supported releases are DRF 3.16, 3.17 and 3.18; the locked development default
is 3.18.3. DRF support is not part of the published `v0.1.0` distribution. The
hooks preserve native dispatch arguments, return values, exception handlers,
authentication, permissions, throttling, streaming and rendering behavior.

## Recording boundaries and privacy

With the default prepended middleware position, a buffered response is finished
after response middleware completes. For streaming responses, it is finished at
iterator exhaustion, failure or close. These are Django-observable boundaries,
not FastAPI's ASGI `send` boundary.

DRF operation spans are `django_drf.dispatch`, `django_drf.initial` and
`django_drf.render`. Dispatch includes the handler and serializer work; initial
includes content negotiation, authentication, permissions and throttling;
render times actual response rendering, not serializer `.data`. Initial is a
child of dispatch. Deferred rendering is a sibling under the HTTP span.
Already-rendered responses do not add another render span. Streaming responses
remain owned by the existing HTTP middleware. APIViews, generic views,
ViewSets/router actions and `@api_view` share the same APIView lifecycle.

A DRF `ValidationError` reaching `handle_exception` emits one WARN
`django_drf.validation.failed` with `django_drf.validation.error_count` (the
number of scalar leaves in the nested details) and `http.route` when resolved.
Validation details, input values, field names and error codes are never
serialized into telemetry. A non-raising `is_valid()` followed by an
application-created 400 is not detected. Expected DRF/Django 4xx errors do not
emit exception logs or mark operation spans as errors. Unexpected escaping
exceptions, handler failures and `APIException` responses with a 5xx status
emit one `http.server.request.exception`, deduplicated by exception identity.
HTTP spans still record every response status.

Validation warnings contain only the framework-specific validation count and
the resolved `http.route`; invalid input is not serialized into the event.
Unexpected exception events can contain the exception message and stack trace.
Redact
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
is built with a locked `uv sync` for the `ninja`/`drf`/`otlp` extras and
development group. The locked default is DRF 3.18.3. Rebuild after changing
`pyproject.toml` or `uv.lock`; normal test commands do not resync dependencies.
Compatibility jobs build isolated environments with `scripts/compat.sh` rather
than modifying the committed lockfile. The published `v0.1.0` release evidence
recorded Ruff, format, ty and 227 tests in the default environment, and nine
Python/Django matrix jobs plus four compatibility jobs. DRF delivery evidence is
recorded separately in [task status](docs/tasks.md).

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
virtual environment, starts Uvicorn and issues three baseline Django/Ninja
requests (a plain Django success, a Ninja validation failure and a plain Django
uncaught error) plus three DRF requests (success, validation failure, uncaught
error). It asserts six SERVER spans, one Ninja and one DRF validation warning,
two correlated exception events, the DRF operation spans, validation-input and
field-name omission, and exactly one HTTP duration measurement per route.

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

The package does not instrument Channels/WebSockets, background jobs,
per-chunk spans, third-party asynchronous DRF extensions or a dashboard stack.
It does not patch serializers, create per-field spans, or record validation
details. Ninja hooks require Ninja 1.7 and DRF hooks require DRF 3.16–3.18; each
framework's hooks are disabled with one diagnostic on an unsupported version
while ordinary Django telemetry and the other framework continue.

DRF coverage is the shared APIView lifecycle. A non-raising `is_valid()`
followed by an application-created 400 is not detected. The package does not
promise support for arbitrary asynchronous DRF extensions outside that
lifecycle.

See the [DRF support plan](docs/drf-support.md) for the frozen contract and
[task status](docs/tasks.md) for current evidence.
