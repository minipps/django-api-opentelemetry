# Atomic implementation tasks

Implementation and local verification are complete. T0-T11 are complete. T12
also passed in the first tagged GitHub Actions run. T13 created the `v0.1.0`
GitHub Release, but the PyPI upload failed because the distribution metadata
used the old name; the metadata now targets `django-api-opentelemetry` for the
retry.
The [implementation plan](implementation-plan.md) defines behavior and the
[Docker contract](development-and-ci.md) defines the execution environment. Each
task is complete only when its listed acceptance checks pass; implementation
alone does not complete a task.

## Ownership and coordination

Implementation used a coordinator plus runtime, telemetry, HTTP, Ninja, delivery,
fixture/smoke and documentation agents. Module implementation was delegated to
`opencode-go/deepseek-v4.1-flash`; the root coordinator reviewed, corrected and
committed the work. The documentation agent owned only `README.md` and `docs/*.md`.

The original plan called for separate branches/worktrees. In practice the agents
worked in the shared checkout with disjoint file ownership, which avoided merge
duplication while feature work was still settling. Parallel Docker runs used
unique Compose project names. Shared-contract or dependency changes went through
the coordinator; agents did not edit another agent's files concurrently. If work
returns to branch-based ownership, each task still needs a unique branch and
Compose project name.

The coordinator owns package metadata, the lockfile, Docker/Compose files,
workflows, shared contracts and integration conflict resolution. Each coding
agent owns its module and focused tests; the test agent owns shared fixtures and
conformance tests. Parallel tasks start once their declared dependencies are
usable. Resolve an upstream incompatibility before widening versions or adding a
fallback. Every handoff includes changed files, exact container commands run,
results and remaining limits. Tasks do not authorize publishing a release.

## Implementation status and evidence

Last reviewed: 2026-10-22. Evidence is limited to files, commits and recorded
container runs in this checkout.

| Task | Status | Evidence and remaining work |
| --- | --- | --- |
| T0 — Package and Docker skeleton | Complete | Commit `b8b0d85`; `Dockerfile`, `compose.yaml`, `pyproject.toml`, `uv.lock`; `dev`/`ci`/`dist` targets and non-root dev image. |
| T1 — Settings and shared contracts | Complete | Commit `b8b0d85`; `_config.py` and `tests/test_config.py`; defaults, environment precedence, invalid keys/types, exclusions and provider duck contracts covered. |
| T2 — Providers and OTLP configuration | Complete | Commit `f47c1e5` plus cleanup fixes `07d9a33` and lifecycle shutdown `b495e24`; focused runtime tests are included in the final 227-test suite. |
| T3 — Telemetry recording core | Complete | Commit `f47c1e5` plus `6d9de1c`; `_telemetry.py` and `tests/test_telemetry.py`; included in the final suite. |
| T4 — Shared Django fixtures | Complete | Commit `7ce7ea9`; baseline settings, demo settings, plain/Ninja URLconfs and WSGI/ASGI fixtures. |
| T5 — Buffered Django middleware | Complete | Commit `e4d5e43`; native sync/async middleware and streaming/file lifecycle; included in the final suite. |
| T6 — Ninja exception hooks | Complete | Commit `b155e29`; `_ninja.py` and `tests/test_ninja.py`; included in the final suite. |
| T7 — Process lifecycle and cleanup | Complete | Commit `b495e24`; public `instrument_asgi`/`shutdown`, bounded owned-resource cleanup and real Uvicorn lifespan test. |
| T8 — Ninja operation spans | Complete | Commit `b155e29`; operation spans for input resolution, endpoint and serialization; included in the final suite. |
| T9 — Streaming and file response lifecycle | Complete | Commit `e4d5e43`; lazy sync/async streaming, early close, cancellation and file-response tests are included in the final suite. |
| T10 — Installed-app activation | Complete | Commit `4caecca`; middleware insertion, Django exception signal registration, Ninja activation and app-laziness regression test. |
| T11 — Conformance and collector smoke test | Complete | Commits `8995e82` and `ae4bcc0`; real contrib coexistence and built-wheel live-collector smoke with three SERVER spans, one WARN, one ERROR and both HTTP metrics. |
| T12 — Container CI and distribution verification | Verified locally and on GitHub | `scripts/check.sh all` passed Ruff, format, ty and 227 tests. The first `v0.1.0` tagged run passed its release gate and reusable CI; its PyPI publishing job rejected the old distribution name. |
| T13 — Release workflow and documentation handoff | GitHub release created; PyPI retry pending | `v0.1.0` is on GitHub. Its initial assets use the old distribution name. Metadata, install instructions, verifier and workflow links now target `django-api-opentelemetry`; rebuilt artifacts and a tagged retry remain. |

Final evidence:

- Default locked environment: `scripts/check.sh all` passed Ruff lint, Ruff
  format, ty and 227 tests, including real OpenTelemetry Django contrib
  coexistence.
- Nine Docker matrix combinations (Python 3.12/3.13/3.14 × Django
  5.2/6.0/6.1): 225 passed, 2 skipped each.
- Ninja 1.7.0 baseline: 225 passed, 2 skipped. OpenTelemetry 1.44 baseline:
  225 passed, 2 skipped.
- Plain Django without Ninja: 185 passed, 42 skipped. API-only without the
  SDK/exporters: 84 passed, 143 skipped.
- Compatibility environments omit the contrib integration; both skips cover
  contrib coexistence, tested in the default locked environment. Missing-extra
  jobs skip tests requiring the absent optional dependencies.
- Built-wheel live-collector smoke: passed with three SERVER spans, one WARN
  validation event, one ERROR exception event and both HTTP metrics.
- Original wheel and wheel rebuilt from the sdist: clean installs outside the
  source tree, `twine check --strict` passed and an actual plain-Django request
  returned HTTP 200.

Committed checkpoints are `528ea2d` (documentation specification), `b8b0d85`
(foundation settings and Docker), `7ce7ea9` (shared fixtures), `f47c1e5`
(runtime/telemetry core), `6d9de1c` (metrics error-type parity), `07d9a33`
(cleanup fix), `b155e29` (Ninja instrumentation), `e4d5e43` (middleware),
`4caecca` (installed-app activation), `b495e24` (ASGI lifespan shutdown),
`8995e82` (contrib coexistence), `ae4bcc0` (built-wheel collector smoke),
`9aaca7e` (optional-dependency test coverage) and `f07ea9b` (CI, distribution
and release workflows).

## T0 — Package and Docker skeleton

- **Owner:** Coordinator. **Dependencies:** None.
- Create the `src/` package skeleton, build metadata, dependency groups, optional
  `ninja`/`otlp` extras and committed uv lockfile.
- Implement the `dev`, `ci` and `dist` Docker targets, base Compose `dev` service,
  non-root source mounting and `.dockerignore`. The demo-server default command
  is added after the shared project exists in T4.
- Include pytest, Ruff, ty, build and twine in container development tooling.
- **Done:** A clean container build can import the package and build a wheel
  without initializing telemetry. Container checks create no root-owned source
  files; no host Python installation is required.

## T1 — Settings and shared contracts

- **Owner:** Coordinator. **Dependencies:** T0.
- Implement `_config.py`, public settings validation and explicit/environment
  precedence. Freeze the request-state fields and helper signatures listed in
  the implementation plan before delegating downstream work.
- Document module boundaries; do not add a generic framework registry or public
  provider-management abstraction.
- **Done:** Container tests cover defaults, overrides, invalid settings,
  exclusions and provider selection rules. Other agents can work against the
  documented contracts without changing shared files.

## T2 — Providers and OTLP configuration

- **Owner:** Runtime agent. **Dependencies:** T1.
- Implement `_runtime.py` provider resolution and opt-in HTTP/protobuf export.
- Cover signal-specific endpoints, exporter `none`, SDK-disabled mode, missing
  optional dependencies, preserved providers and unsupported registration.
- Track package-owned export components without inspecting other exporters'
  private destination state. Configuration failures warn without breaking
  requests.
- **Done:** Focused container tests verify selection, endpoints, ownership and
  failure behavior without contacting a remote service.

## T3 — Telemetry recording core

- **Owner:** Telemetry agent. **Dependencies:** T1.
- Implement `_telemetry.py`: request state, context activation, cached
  instruments, HTTP spans/measurements, operation spans and automatic log events.
- Implement exception-identity deduplication, explicit disabled state and
  idempotent completion. Keep Ninja imports out of the module.
- **Done:** Explicit in-memory provider tests cover sampled/unsampled logs,
  parentage, exact event counts, balanced active requests, duplicate completion,
  exception cleanup and exclusion isolation.

## T4 — Shared Django fixtures

- **Owner:** Test agent. **Dependencies:** T0.
- Create one minimal example/test Django project with ordinary views, Ninja
  routes, custom exception handlers, nested/reused routers and sync/async streams.
- Keep pure-Django settings/URLs separate from optional Ninja routes so absence
  of Ninja is a real tested condition.
- Add explicit in-memory provider fixtures and actual WSGI/ASGI handler helpers.
  Ask the coordinator to wire the development server command in Compose.
- **Done:** Fixture baselines run in Docker before instrumentation, including
  success/failure responses and both request modes.

## T5 — Buffered Django middleware

- **Owner:** HTTP agent. **Dependencies:** T2, T3, T4.
- Implement native sync/async `middleware.py`, propagation, route naming,
  exclusions and buffered request completion.
- Add the Django exception-signal receiver for T10 to register. Detect the
  standard OpenTelemetry Django middleware; honor explicit external HTTP
  ownership for other wrappers.
- **Done:** Container tests cover success, 404, 5xx, middleware short-circuiting,
  nested context, propagation and exclusions without response changes or a
  forced synchronous adapter for async requests.

## T6 — Ninja exception hooks

- **Owner:** Ninja agent. **Dependencies:** T3, T4.
- Implement `_ninja.py` version/signature guards and idempotent exception hooks.
- Observe validation, DEBUG behavior, custom handlers, expected HTTP errors and
  unexpected exceptions converted to 5xx responses.
- Preserve original exception handlers and distinguish input validation from
  output validation/serialization failures.
- **Done:** Container tests assert exact log counts, sensitive-input omission,
  supported-version behavior and a single diagnostic for unsupported hooks.

## T7 — Process lifecycle and cleanup

- **Owner:** Runtime agent. **Dependencies:** T2.
- Complete lazy, per-serving-process initialization and bounded flush/shutdown
  of package-owned resources.
- Provide the public `instrument_asgi(application)` lifespan wrapper and
  synchronous `shutdown()` helper. Native Django ASGI does not implement
  lifespan; the wrapper acknowledges startup without exporter initialization,
  forwards non-lifespan scopes unchanged, and flushes owned resources on
  `lifespan.shutdown`. This was added because Uvicorn 0.54 re-raises `SIGTERM`
  after draining, bypassing `atexit`. Do not intercept signals or claim a flush
  on every termination path; worker-exit hooks can call `shutdown()` after
  requests drain.
- Handle repeated initialization and partial setup failure without accumulating
  resources or closing application-owned providers.
- **Done:** Subprocess tests in Docker cover repeated calls, concurrent first
  use, prefork behavior, partial failures and shutdown ownership. A real Uvicorn
  subprocess confirms lifespan shutdown, and importing the public helpers works
  without Ninja or the SDK/exporters.

## T8 — Ninja operation spans

- **Owner:** Ninja agent. **Dependencies:** T6.
- Instrument fully bound endpoint callables through `PathView.get_view()` and
  add input-resolution/serialization wrappers without copying Ninja logic.
- Preserve signatures, coroutine behavior, existing decorators and generator
  return values. Do not wrap an endpoint again on subsequent URL generation or
  router reuse.
- **Done:** Container tests cover nested/reused routers, multiple APIs, sync and
  async endpoints, generators, decorators, exact span counts and disable flags.

## T9 — Streaming and file response lifecycle

- **Owner:** HTTP agent. **Dependencies:** T5.
- Add lazy sync/async streaming context, exhaustion/failure/close finalization
  and original resource cleanup.
- Preserve `FileResponse` and WSGI file-wrapper behavior. Do not leave context
  active across yields or invent exception logs for cancellation/closure.
- **Done:** Actual handler tests in Docker cover lazy consumption, generator
  failures, early close, cancellation, files, context restoration and exactly
  one completion. Document the Django timing boundary.

## T10 — Installed-app activation

- **Owner:** Coordinator. **Dependencies:** T5, T6.
- Implement `apps.py` middleware insertion, Django signal registration and
  activation of supported Ninja hooks.
- Respect explicitly positioned middleware and repeated `ready()` calls. Keep
  startup free of database access, network calls and exporter threads.
- **Done:** Container tests cover list/tuple settings, repeated initialization,
  explicit positions, one-entry setup and Django without Ninja/SDK extras.

## T11 — Conformance and collector smoke test

- **Owner:** Test agent; coordinator owns Compose integration.
- **Dependencies:** T7, T8, T9, T10.
- Add cross-module regression tests for the FastAPI-derived behavior table,
  concurrent/excluded requests and standard Django instrumentation coexistence.
- Implement a bounded local OTLP smoke test against the built wheel, asserting
  all three signals, trace/log correlation and validation-input omission. The
  coordinator adds the collector/smoke services and telemetry profile. The
  collector output is read-only to the smoke service and must start from a fresh
  volume; stale records fail the check.
- **Done:** Docker tests meet every release acceptance criterion. Smoke tests
  start from empty output, fail on missing signals and retain useful logs.

## T12 — Container CI and distribution verification

- **Owner:** Coordinator. **Dependencies:** T11.
- Implement GitHub Actions container jobs and the compatibility matrix specified
  in the Docker contract. Keep default locked and alternate dependency sets
  separate.
- Build wheel/sdist, run metadata checks, rebuild from sdist and smoke-test both
  wheel origins outside the source tree. Store artifacts and diagnostics.
- **Done:** CI configuration is validated and its underlying Docker commands
  pass locally. Record an actual workflow run when repository hosting is
  available; do not describe an unexecuted remote workflow as verified.

## T13 — Release workflow and documentation handoff

- **Owner:** Coordinator and documentation agent. **Dependencies:** T12.
- Prepare tagged-release workflows using the same Docker checks and exact
  verified distributions, followed by the official PyPA container publishing
  action. Separate TestPyPI prereleases and stable PyPI releases.
- Document one-entry setup, pure Django, Ninja, exclusions, redaction, provider
  ownership, external HTTP instrumentation, version limits and Docker commands.
- Verify package-name availability and list the repository-specific Trusted
  Publisher setup needed from maintainers. Do not create external publisher
  records or publish during implementation without an explicit release request.
- **Done:** Workflow structure and tag/version gates are checked; built-wheel
  examples pass in Docker. External setup and unexecuted publishing are clearly
  identified in the handoff.

## Execution waves

1. **Foundation:** Coordinator completes T0 → T1.
2. **Parallel groundwork:** Runtime T2, telemetry T3 and fixtures T4.
3. **Parallel features:** HTTP T5 → T9, Ninja T6 → T8 and runtime T7.
   Coordinator completes T10 when T5/T6 are ready.
4. **Integration:** T11 after all feature and lifecycle dependencies.
5. **Delivery preparation:** T12 → T13. Documentation drafting may start earlier
   against the frozen specification, but final examples use the built wheel.

The actual run followed this dependency order in the shared checkout described
above. T0-T11 are complete and verified locally. T12 passed on GitHub in the
first tagged run. T13 created the GitHub Release; the PyPI publishing job failed
on the old distribution name and awaits the corrected tagged run.

Do not dispatch dependent tasks early as independently mergeable work. A failed
acceptance check stays with its module owner; the coordinator resolves changes
that cross module boundaries.
