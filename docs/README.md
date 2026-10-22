# Django API OpenTelemetry

This directory began as the implementation specification researched on
2026-10-22 and is now the implementation record for this checkout. The package,
Docker environment, scripts and workflows described here exist in the
repository. Version `v0.2.0` adds DRF support and hardened CI/CD to the initial
`v0.1.0` release. See [task status](tasks.md) for the recorded evidence.

- [DRF support plan](drf-support.md): frozen contract, supported DRF families,
  hook semantics, settings compatibility, privacy limits and task ownership for
  the integration included in `v0.2.0`.
- [Implementation plan](implementation-plan.md): upstream research, agreed
  scope, public configuration, architecture, lifecycle design, behavior and
  acceptance criteria. The FastAPI and Ninja references are retained as design
  provenance.
- [Atomic implementation tasks](tasks.md): task ownership, dependencies,
  completion checks and the current implementation/evidence table.
- [Docker development and CI/CD](development-and-ci.md): the implemented
  container workflow, compatibility matrix, distribution checks and prepared
  release pipeline.

The `v0.1.0` release covers Django HTTP telemetry and automatic Ninja validation
and exception logging. Version `v0.2.0` adds an optional DRF 3.16–3.18
integration through the shared APIView lifecycle without adding required
dependencies or public configuration keys. Docker is the development, test and
package-build environment; users of the published Python package do not need
Docker.

The delivered implementation differs from the original plan in one public API
addition: `instrument_asgi()` and `shutdown()` provide an explicit ASGI lifespan
shutdown path. This was added after discovering that Uvicorn 0.54 re-raises
`SIGTERM` after draining, bypassing `atexit`. The rationale and behavior are
recorded in the [implementation plan](implementation-plan.md#asgi-lifecycle-and-shutdown).
