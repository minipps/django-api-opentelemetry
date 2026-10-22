# Atomic implementation tasks

All tasks are pending. The [implementation plan](implementation-plan.md)
defines behavior and the [Docker contract](development-and-ci.md) defines the
execution environment. Each task produces one reviewable commit with focused
checks run in Docker. A task is complete only when its listed acceptance checks
pass; implementation alone does not complete it.

## Ownership and coordination

Use a coordinator plus runtime, telemetry, HTTP, Ninja and test agents. A
documentation agent may work on the final handoff. These are roles for future
implementation; no implementation agents have been launched as part of writing
these documents.

The coordinator owns package metadata, the lockfile, Docker/Compose files,
workflows, shared contracts and integration conflict resolution. Each coding
agent owns its module and corresponding focused tests. The test agent owns
shared fixtures and conformance tests. Request shared-contract/dependency
changes through the coordinator; do not edit another agent's files concurrently.

Give agents separate branches/worktrees and unique Compose project names.
Parallel tasks start once their declared dependencies are merged. Resolve an
upstream incompatibility before widening versions or adding a fallback. Every
handoff includes changed files, exact container commands run, results and any
remaining limits. Tasks do not authorize publishing a release.

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
- Handle repeated initialization and partial setup failure without accumulating
  resources or closing application-owned providers.
- **Done:** Subprocess tests in Docker cover repeated calls, concurrent first
  use, prefork behavior, partial failures and shutdown ownership.

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
  coordinator adds the collector/smoke services and telemetry profile.
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

Do not dispatch dependent tasks early as independently mergeable work. A failed
acceptance check stays with its module owner; the coordinator resolves changes
that cross module boundaries.
