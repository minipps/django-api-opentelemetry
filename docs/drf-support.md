# DRF support plan and tasks

## Scope and contracts

Add optional Django REST framework instrumentation to the existing installed
app. Reuse Django HTTP spans, metrics, exclusions, provider ownership, exception
identity deduplication and the existing `DJANGO_NINJA_OTEL` settings. No new
public exports, configuration keys, adapter registry or required dependency.

Support DRF `>=3.16,<3.19`, with default locked development on 3.18.3. Verify
3.16.1 and 3.17.2 on Django 5.2; the existing Python/Django matrix uses 3.18.3.
Missing DRF is silent. Unsupported versions or incompatible hook signatures
produce one diagnostic and leave all DRF hooks untouched. Installation is
idempotent and lazy in `TelemetryConfig.ready()`.

Research baseline (2026-10-22): DRF's [release notes][releases] identify 3.18.3
as current and 3.18 as adding Django 6.1 support. The tagged [APIView source][view]
exposes synchronous `dispatch`, `initial`, and `handle_exception`. DRF responses
render after dispatch through Django's template-response lifecycle. Request
wrappers delegate attributes to the underlying Django request.

Hook those three APIView methods and DRF `Response.render` (without patching
Django's shared base class). Preserve native arguments, return values,
exception handlers, authentication, permissions, throttling, streaming and
rendering behavior. APIViews, generic views, ViewSets/router actions and
`@api_view` are covered through their shared APIView lifecycle. Native DRF
views remain synchronous under WSGI and Django ASGI.

Operation spans are `django_drf.dispatch`, `django_drf.initial`, and
`django_drf.render`. Dispatch includes handler execution and serializer work;
initial includes negotiation, authentication, permissions and throttling;
render times actual response rendering, not serializer `.data`. Initial is a
child of dispatch; deferred rendering is a sibling under the HTTP span.
Already-rendered responses do not create another render span. Streaming stays
owned by the existing HTTP middleware.

`ValidationError` reaching `handle_exception` emits one WARN event named
`django_drf.validation.failed`, with `django_drf.validation.error_count` (the
number of scalar leaves in nested dict/list details) and optional `http.route`.
Never serialize validation details, input, field names or codes into telemetry.
Non-raising `is_valid()` followed by an application-created 400 is not detected.
Expected DRF/Django 4xx errors do not emit exception logs or mark operation spans
as errors. Unexpected escaping exceptions, handler failures and exceptions
converted to 5xx emit the existing `http.server.request.exception` once by
identity. Custom handlers converting unexpected errors to successful/4xx
responses do not emit error logs. APIException with a 5xx status is an error.
HTTP spans still reflect all response status codes.

Shared helper changes must preserve all Ninja names and existing calls:
`operation` accepts the DRF namespace; `record_validation` gains only an
optional keyword-only `framework="django_ninja"`. Shared modules import neither
framework. Hooks use only explicit request state, including explicit `None` for
excluded requests; never infer state from ambient spans.

## Tasks and ownership

| Task | Owner | Files | Acceptance |
| --- | --- | --- | --- |
| D0: Plan and checkpoints | Coordinator | This plan, task status | Plan committed/pushed before coding; reviewed checkpoints pushed as work lands. |
| D1: Shared contracts and hooks | DeepSeek core agent | `_drf.py`, `_telemetry.py`, `apps.py`, `test_telemetry.py`, `test_apps.py` | Guarded hooks, original behavior, shared-helper regressions, type/lint checks. |
| D2: DRF conformance tests | DeepSeek tests agent | `tests/test_drf.py`, `tests/project/drf_urls.py` | Real middleware and explicit in-memory providers; WSGI/ASGI, view families, privacy/counts, custom handlers, render failure, exclusions, disabled signals, expected errors, streaming, install guards. |
| D3: Dependencies and delivery | DeepSeek delivery agent | Metadata/lock, Dockerfile, CI, compatibility/smoke scripts and tests, README and existing docs | Optional `drf` extra, locked default and compatibility jobs, missing extras stay real, built-wheel DRF smoke and current setup docs. |
| D4: Review and verification | Coordinator; corrections delegated | All diffs | Full container checks, baseline DRF versions, absent-extra checks, build/distribution and collector smoke; record actual evidence and any unavailable checks. |

Agents use only `opencode-go/deepseek-v4.1-flash` with disjoint file ownership.
They do not commit, push, change release versions, publish or edit another
agent's files. Coordinator reviews, commits and pushes `feature/drf-support`.
Tests may start against this frozen contract while D1/D3 are in progress; final
execution waits for their dependencies. Failures return to their owning agent.

No serializer monkey-patching, per-field spans, asynchronous third-party DRF
extensions, background tasks, Channels, new settings aliases or release work.

## Execution evidence

- D0: Plan recorded before delegation; implementation and verification pending.

[releases]: https://www.django-rest-framework.org/community/release-notes/
[view]: https://github.com/encode/django-rest-framework/blob/3.18.3/rest_framework/views.py
