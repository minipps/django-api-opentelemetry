# Django and Django Ninja OpenTelemetry

Implementation specification, based on upstream research performed on 2026-10-22.
These documents describe planned work; the package, Docker configuration and
workflows have not been implemented yet.

- [Implementation plan](implementation-plan.md): upstream findings, public
  configuration, architecture, behavior and acceptance criteria.
- [Atomic implementation tasks](tasks.md): dependencies, ownership, parallel
  execution order and completion checks for coding agents.
- [Docker development and CI/CD](development-and-ci.md): the required container
  workflow, compatibility matrix, artifact checks and release pipeline.

The first release covers Django HTTP telemetry and automatic Ninja validation
and exception logging. Keep framework-specific hooks separate so DRF support can
be added later. Docker is the development, test and package-build environment;
users of the published Python package do not need Docker.
