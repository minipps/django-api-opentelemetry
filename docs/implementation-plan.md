# Implementation plan

## Goal and agreed scope

Build `django-api-opentelemetry`, imported as
`django_api_opentelemetry`, as a reusable installed Django app. One
`INSTALLED_APPS` entry enables Django-wide HTTP traces, metrics and exception
logs, with deeper instrumentation for ordinary Django Ninja APIs. Endpoint
decorators and a replacement `NinjaAPI` class are not required.

The initial release targets Python 3.12–3.14, Django 5.2/6.0/6.1, Ninja
1.7.0–1.7.1, and OpenTelemetry API/SDK 1.44+. Constrain the optional Ninja
dependency to `>=1.7,<1.8` until the next minor version is verified. Use a
`src/` package layout. Ninja and SDK/exporter dependencies are optional extras,
so the base package also works in plain Django projects.

Development, tests, compatibility checks and distribution builds run in Docker.
The [Docker workflow](development-and-ci.md) and [task breakdown](tasks.md) are
part of this specification.

DRF is a future integration. Keep Ninja imports out of the shared request and
telemetry code; do not implement DRF hooks or a generic adapter registry now.

## Implementation status

The plan below has been implemented in this checkout as the modules
`_config.py`, `_runtime.py`, `_telemetry.py`, `middleware.py`, `_ninja.py`,
`apps.py` and `_asgi.py`. One installed-app entry remains the normal setup; the
public lifecycle helpers are an optional addition described below. The
[task status](tasks.md) table records the final local evidence, external GitHub
verification and release setup. This is a design record, not a claim that the
release has been published.

## FastAPI research baseline

FastAPI's native integration originated in
[0.142.0](https://github.com/fastapi/fastapi/releases/tag/0.142.0), through
[PR #16403](https://github.com/fastapi/fastapi/pull/16403).

| Version | Relevant change | Requirement for this package |
| --- | --- | --- |
| 0.142.0 | Native telemetry | Combine HTTP instrumentation with framework hooks. |
| 0.142.1 | Fixed repeated endpoint wrapping | Wrap each callable once, including reused routers. |
| 0.142.2 | Allowed startup after automatic configuration fails | Optional exporter failures must not stop application handling. |
| 0.142.3 | Cached tracers to preserve warning deduplication | Cache instruments by provider identity. |
| 0.142.4 | Isolated excluded requests | Exclusions suppress internal hooks and inherited package request state. |
| 0.143.0 | Made exporter setup opt-in | Recording telemetry and configuring its destination are separate concerns. |

These changes are recorded in
[FastAPI's release notes](https://fastapi.tiangolo.com/release-notes/#01430).
The contents of the three telemetry core files, `_api.py`, `_asgi.py` and
`_runtime.py`, were compared at tags 0.143.0 and 0.159.1 and were identical.
This establishes the later-version baseline without assuming every FastAPI
subsystem stayed unchanged.

FastAPI automatically records validation warnings and unhandled-exception logs,
alongside request spans, operation spans and HTTP metrics. Automatic logging is
not an access log for every successful request or forwarding of arbitrary
Python application logs. Error logs are independent of trace sampling. See the
[FastAPI telemetry documentation](https://fastapi.tiangolo.com/advanced/opentelemetry/)
and the tagged implementation:

- [Telemetry API and operation spans](https://github.com/fastapi/fastapi/blob/0.159.1/fastapi/telemetry/_api.py).
- [HTTP lifecycle and exception logs](https://github.com/fastapi/fastapi/blob/0.159.1/fastapi/telemetry/_asgi.py).
- [Exporter setup and resource ownership](https://github.com/fastapi/fastapi/blob/0.159.1/fastapi/telemetry/_runtime.py).

Django middleware alone cannot observe all Ninja failures. Ninja catches input
validation and operation exceptions before delegating to `NinjaAPI.on_exception()`.
The package therefore needs guarded Ninja hooks as well as HTTP middleware.
The extension points were inspected in Ninja's
[operation runner](https://github.com/vitalik/django-ninja/blob/v1.7.1/ninja/operation.py),
[exception dispatch](https://github.com/vitalik/django-ninja/blob/v1.7.1/ninja/main.py)
and [router binding](https://github.com/vitalik/django-ninja/blob/v1.7.1/ninja/router.py).

## Installation and public configuration

```bash
pip install "django-api-opentelemetry[ninja,otlp]"
```

```python
INSTALLED_APPS = [
    # Existing applications...
    "django_api_opentelemetry",
]
```

Automatic exporter setup is explicitly enabled:

```bash
export DJANGO_NINJA_OTEL_AUTO_CONFIGURE=true
export OTEL_SERVICE_NAME=my-api
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

When the application runs inside Compose, use the collector service address
`http://collector:4318` instead of `localhost`.

### ASGI lifecycle and shutdown

Native Django ASGI applications do not implement ASGI lifespan. The
implementation therefore adds two public helpers without changing the
installed-app setup:

```python
# asgi.py
from django.core.asgi import get_asgi_application

from django_api_opentelemetry import instrument_asgi

application = instrument_asgi(get_asgi_application())
```

`instrument_asgi(application)` returns a wrapper that:

- forwards every non-lifespan scope to the original application unchanged;
- acknowledges `lifespan.startup` without initializing exporters or opening
  network connections;
- on `lifespan.shutdown`, calls the synchronous package shutdown in a worker
  thread and then sends `lifespan.shutdown.complete`.

The synchronous `shutdown()` is exported for WSGI or custom worker-exit hooks
after requests have drained. It is bounded, idempotent and only flushes/shuts
down package-owned resources.

This addition was driven by an actual runtime finding: Uvicorn 0.54 re-raises
`SIGTERM` after its drain sequence, so the normal `atexit` hook is bypassed.
Relying on `atexit` alone therefore does not guarantee an exporter flush on that
termination path. The lifespan wrapper provides an explicit shutdown signal.
The package does not intercept signals and does not claim a flush on every
possible server-termination path; deployments using another worker should call
`shutdown()` from the worker's documented exit hook. The installed app alone
still activates HTTP telemetry and Ninja hooks; the wrapper is recommended for
reliable ASGI shutdown rather than required for recording.

Use one optional Django setting, `DJANGO_NINJA_OTEL`:

| Key | Default | Meaning |
| --- | --- | --- |
| `tracing` | `True` | Enable package-created spans, including operation spans. |
| `metrics` | `True` | Enable HTTP measurements. |
| `logs` | `True` | Emit automatic validation and exception logs. |
| `operation_spans` | `True` | Add Ninja operation spans when tracing is enabled. |
| `http_instrumentation` | `True` | Allow package-owned HTTP server spans and metrics; false leaves HTTP ownership to another integration. |
| `auto_configure` | Environment value, otherwise `False` | Configure OTLP exporters; an explicit setting overrides the environment. |
| `exclude` | `None` | Callable receiving `HttpRequest`; true suppresses all package telemetry for that request. |
| `tracer_provider` | `None` | Explicit provider, otherwise the global provider. |
| `meter_provider` | `None` | Same selection rule for metrics. |
| `logger_provider` | `None` | Same selection rule for logs. |

```python
DJANGO_NINJA_OTEL = {
    "exclude": lambda request: request.path_info == "/health",
}
```

Validate setting names and types. Invalid Django settings raise
`ImproperlyConfigured`; optional exporter setup failures instead warn and skip
the affected signal. Without configured providers, nothing is exported. With no
OTLP endpoint configured, no exporter is installed; the package does not assume
a default localhost destination.

The installed app inserts its middleware first unless it is explicitly present
in `MIDDLEWARE` already. Respect an explicit position and handle list or tuple
settings. Ninja-specific hooks activate only when a supported Ninja version is
installed. SDK and exporter imports remain optional.

## Architecture and internal contracts

Use a small, flat package. Configuration, runtime, telemetry, middleware, Ninja
hooks and app initialization have separate ownership.

| Module | Responsibility |
| --- | --- |
| `_config.py` | Typed settings and normalized configuration. |
| `_runtime.py` | Provider resolution, optional exporter setup and owned-resource cleanup. |
| `_telemetry.py` | Request state, context activation, instruments, log emission and deduplication. |
| `middleware.py` | Synchronous/asynchronous Django request and response lifecycle. |
| `_ninja.py` | Guarded Ninja hooks and operation instrumentation. |
| `apps.py` | Middleware insertion, signal registration and hook activation. |
| `_asgi.py` | Optional ASGI lifespan wrapper and public shutdown path. |

Freeze these internal contracts so modules can be implemented independently.
They are internal helpers, not additional public APIs; the only public lifecycle
addition is `instrument_asgi()`/`shutdown()` documented above:

- `_runtime.get_providers(config)` returns the selected tracer, meter and logger
  providers in that order, initializing optional exporters once per process.
- `_telemetry.begin_request(request, config)` returns request telemetry state,
  or `None` for excluded/disabled requests, and stores that result explicitly on
  the request.
- `_telemetry.activate(state)` is a context manager that attaches and restores
  request context in the same execution context.
- `_telemetry.operation(state, name, function=None)` is a context manager for
  an optional internal span.
- `_telemetry.record_validation(state, error_count)` emits the validation event.
- `_telemetry.record_exception(state, exc)` records an unexpected exception once.
- `_telemetry.finish_request(state, status_code, error=None)` completes owned
  measurements and the server span exactly once.

Helpers accept `None` state as a no-op. Request state holds the selected
configuration/providers, saved OpenTelemetry context, owned server span,
monotonic start time, resolved route, response status, seen exceptions and
completion flag. Clear retained exception references at completion. State lives
on the request rather than in an independent process-global current-request
variable. Hooks never infer their request state from another request's ambient
span. An excluded request must retain an explicit disabled state.

## Required behavior

### Django HTTP lifecycle

- Implement native sync and async middleware without forcing async views through
  a synchronous middleware adapter.
- Extract incoming context through the configured OpenTelemetry propagator.
- Create one owned `SERVER` span named from method and resolved Django route.
  Preserve an already-active local parent; extract inbound remote context when
  there is no local parent. Use method-only names when no route is resolved.
- Use route templates, including mount prefixes, for names and metric labels.
  Never put raw paths or identifiers into metric dimensions.
- Record `http.server.request.duration` in seconds and
  `http.server.active_requests`. Derive counts from histogram measurements.
  Increment/decrement active requests with identical attributes.
- Mark 5xx responses and unexpected exceptions as errors; expected 4xx responses
  do not automatically mark server spans as errors.
- Observe ordinary Django unhandled exceptions through `got_request_exception`,
  including errors Django converts into responses. Preserve application
  responses, exception propagation and existing Django logging.
- Capture no bodies, validation inputs, query strings, cookies or headers by
  default. Use stable OpenTelemetry HTTP conventions.

The middleware finishes buffered requests after response middleware completes.
Streaming requests finish at exhaustion, failure or closure. These are
Django-observable boundaries, not FastAPI's ASGI `send` boundary. The distinction
must appear in user documentation and metric tests.

### Streaming responses

Wrap existing streaming iterators lazily, preserving their sync/async type,
original bytes and cleanup. Restore request context only while advancing the
iterator, then detach before yielding to its consumer. Finalization must be
idempotent across iterator exhaustion, errors, early close and cancellation.

Preserve `FileResponse` and WSGI file-wrapper behavior, including completion when
the wrapper bypasses Python iteration. Keep original response/file close calls.
Cancellation and generator closure must not become fabricated unhandled-error
logs. Actual generator failures remain observable and are re-raised.

See Django's documentation on
[streaming middleware](https://docs.djangoproject.com/en/5.2/topics/http/middleware/#dealing-with-streaming-responses)
and [exception signals](https://docs.djangoproject.com/en/5.2/ref/signals/#got-request-exception).

### Ninja hooks and automatic events

Keep patches in `_ninja.py`, check version and expected signatures before
installation, and wrap original implementations rather than copying them.
Unsupported Ninja versions produce one clear diagnostic and disable
Ninja-specific hooks while ordinary Django telemetry continues.

Use `PathView.get_view()` to wrap fully bound endpoint callables once, after
router decorators have been applied. Wrap `Operation._get_values()` for input
resolution, `Operation._result_to_response()` for response serialization, and
`NinjaAPI.on_exception()` for validation and exception inspection.

Internal span names are `django_ninja.input_resolution`,
`django_ninja.endpoint` and `django_ninja.serialization`. Preserve endpoint
signatures, coroutine detection and generator return values. Generator creation
may have an endpoint span; iteration runs under the HTTP request context,
without an endpoint span left active across yields. Do not add per-chunk spans
in the first release.

| Situation | Automatic event |
| --- | --- |
| `ninja.errors.ValidationError` | One WARN `django_ninja.validation.failed`, with `http.route` when known and `django_ninja.validation.error_count`. |
| Unexpected exception escaping Ninja | One ERROR `http.server.request.exception`. |
| Unexpected exception converted into a 5xx response | One error event containing the original exception. |
| Expected HTTP/authentication/authorization/throttling error | No unhandled-exception log. |
| Custom handler converts an unexpected exception into a non-5xx response | No unhandled-exception log. |
| Response validation/serialization failure | Error, distinct from input validation. |

Exception logs include type, message and stack trace, correlated with current
trace context. Emit independently of span sampling. Deduplicate by exception
identity across Ninja, Django signals and streaming observation. A plain 5xx
response without an exception marks the span as failed but does not invent an
exception log. Never serialize validation errors into the warning event.

### Provider ownership and coexistence

The base dependencies are Django and `opentelemetry-api`. The `ninja` extra adds
Ninja; `otlp` adds the SDK and HTTP/protobuf exporters. Docker development tools
are not runtime dependencies of the published package.

Automatic setup supports standard OTLP signal-specific endpoints, exporter
`none` and `OTEL_SDK_DISABLED`. Delegate header authentication, certificates and
timeouts to official exporters. Explicit `auto_configure` overrides
`DJANGO_NINJA_OTEL_AUTO_CONFIGURE`.

Initialize export components lazily on first request, once per serving process.
`AppConfig.ready()` does not create exporter threads, access the database or
perform network requests. Preserve configured providers. With explicit
auto-configuration, add package-owned components where registration is supported;
warn and skip an unsupported signal. Shut down only resources created by this
package, once, through the ASGI lifespan wrapper or an explicit `shutdown()`
call. Do not replace or shut down application-owned providers. A best-effort
`atexit` cleanup remains registered, but it is not the documented guarantee for
server termination paths such as Uvicorn 0.54's post-drain `SIGTERM` re-raise.

Detect standard OpenTelemetry Django middleware and defer HTTP spans and
metrics to it while retaining Ninja enrichment. Other wrappers use
`http_instrumentation=False`. Keep tracing enabled to add operation spans to
the existing request context. Do not claim automatic detection of every vendor
wrapper or duplicate exporter destination.

## Acceptance and limits

Release only when all of these hold:

- An ordinary `NinjaAPI()` works with one installed-app entry.
- Plain Django works without Ninja or SDK/exporter extras installed.
- Validation yields one warning without invalid input; unexpected errors yield
  one correlated error even with unsampled traces.
- Expected HTTP failures and custom handlers retain response behavior.
- Excluded, nested and concurrent requests cannot inherit another request's
  package telemetry state.
- Streams stay lazy; all completion paths leave no context leak or unbalanced
  active-request count.
- Reused routers and repeated initialization do not multiply wrappers or
  exporter components.
- Existing Django instrumentation produces no second package-owned HTTP span
  or metric measurement.
- The optional ASGI wrapper forwards non-lifespan scopes unchanged, acknowledges
  startup, and flushes owned resources once on lifespan shutdown; a real Uvicorn
  subprocess verifies the shutdown path.
- Docker-based tests pass across the compatibility matrix, and a local OTLP
  receiver confirms traces, metrics and logs from the built wheel.

Use actual Django clients and WSGI/ASGI handlers for middleware tests. Ninja's
standalone test client cannot establish middleware correctness. Isolate global
provider configuration and process-lifecycle tests in subprocesses.

Defer DRF, Channels/WebSockets, background jobs, automatic application-log
forwarding, per-chunk spans and observability dashboards. Ninja hooks require
version maintenance. Exception messages and stack traces can contain sensitive
data; document processor-level redaction and `logs=False`.
