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

- D0: Plan committed and pushed as `7581c9e` before coding delegation.
- D1: Implemented by the DeepSeek core agent and committed as `6c5b833`.
  Shared-helper/startup checks passed 60 tests, lint, formatting and type checks.
- D2: Implemented by the DeepSeek conformance agent and committed as `1811017`.
  All 43 DRF conformance tests pass through the real middleware.
- D3: Implemented by the DeepSeek delivery agent. Optional dependency and Docker
  setup are in `ea89ad3`; CI, compatibility and collector smoke are in `4f38d00`.
  Documentation records the integration as unreleased; no version was changed.
- D4: Coordinator reviewed the diffs and ran the default container checks:
  Ruff lint/format, ty on `src`, and all 288 tests passed. Compose configuration,
  shell syntax and Actionlint checks passed.
- All nine combinations of Python 3.12/3.13/3.14 and Django 5.2/6.0/6.1 passed
  with DRF 3.18.3: 286 passed, two contrib-integration skips in each environment.
- DRF 3.16.1, DRF 3.17.2 and OpenTelemetry 1.44 compatibility environments each
  passed 286 tests with the same two skips. Ninja 1.7.0 without DRF passed
  243 tests (45 skips); plain Django without either framework passed 203
  (85 skips); API-only without SDK/exporters passed 106 (182 skips); DRF without
  Ninja passed 246 (42 skips). Missing-extra skips are expected. The locked
  default environment includes and tests the real contrib integration.
- Docker built a wheel and sdist in `/tmp/drf-support-dist`. Distribution checks
  passed for the original wheel and a wheel rebuilt from the sdist, including
  clean imports and a plain-Django HTTP 200 outside the checkout.
- The isolated `drf-support-verification` collector smoke passed against that
  wheel: six SERVER spans, two WARN validation events, two correlated ERROR
  events, three dispatch spans, three initial spans, two render spans and both
  HTTP metrics. The temporary collector, network and volume were removed.
- Commits are pushed to `feature/drf-support`. Two checkpoint pushes initially
  hit temporary DNS failures; the successful retry pushed both commits. No
  remote CI run or release publication is claimed.

[releases]: https://www.django-rest-framework.org/community/release-notes/
[view]: https://github.com/encode/django-rest-framework/blob/3.18.3/rest_framework/views.py
