# Docker development and CI/CD

This is the implementation contract for the future Docker files and workflows,
not a description of tooling that already exists. It extends the
[implementation plan](implementation-plan.md) and is delivered by the
[atomic tasks](tasks.md).

## Development environment

Require Docker Engine/Desktop with Compose v2 and BuildKit. Host Python,
package managers and virtual environments are not required. Use one Dockerfile
for local development, CI tests and distribution builds; parameterize the
Python version with `PYTHON_VERSION`, defaulting to 3.14.

Use official Python slim images, pin resolved image digests during
implementation, and pin the uv tool version. Store Python dependencies in
`pyproject.toml` and commit `uv.lock`. uv supplies one lockfile for the supported
Python versions; it is a container development tool, not a runtime dependency.

The Dockerfile has these targets:

| Target | Purpose |
| --- | --- |
| `dev` | Locked development dependencies, editable source install and tools. |
| `ci` | Same dependency/tool baseline with source copied into the image and no host bind mount. |
| `dist` | Export only the wheel and sdist built inside the container. |

Put the environment under `/opt/venv` so mounting source at `/workspace` cannot
hide it. Add `.dockerignore` entries for Git internals, local environments,
caches, credentials and existing build output. Do not bake credentials into
layers. Use a non-root development user with configurable `LOCAL_UID` and
`LOCAL_GID` so tests and builds do not create root-owned files on the host.

Compose defines `dev`, `collector` and `smoke` services. `dev` mounts the current
worktree, installs the `ninja`/`otlp` extras and dev dependency group at image
build time, and runs the shared example/test Django ASGI project by default.
Publish the development port only on `127.0.0.1`. Rebuild after dependency
changes. Keep dependency installation out of normal test invocations.

The collector runs only under the `telemetry` profile, accepts OTLP HTTP on
4318 and writes all three signals to a file export in a shared volume. Use a
pinned official collector image with the required receiver/exporter components.
The smoke service waits for collector readiness with a bounded retry, sends
requests and checks exported records. A timeout must fail the smoke check and
print diagnostic logs. Ordinary unit tests use in-memory providers and do not
depend on the collector.

Planned command interface, available after T0/T4/T9:

```bash
docker compose build --build-arg LOCAL_UID="$(id -u)" --build-arg LOCAL_GID="$(id -g)" dev
docker compose run --rm dev python -m pytest
docker compose run --rm dev python -m ruff check .
docker compose run --rm dev python -m ruff format --check .
docker compose run --rm dev ty check src
docker compose run --rm dev python -m build --no-isolation
docker compose up dev
docker compose --profile telemetry up -d collector
docker compose --profile telemetry run --rm smoke
docker compose --profile telemetry down -v
```

The last command deletes the disposable collector output volume. Run smoke
checks with a fresh collector output so stale records cannot satisfy assertions.
Use a distinct Compose project name for each agent worktree to isolate
containers and volumes; do not start multiple development servers on the same
published port. Never mount the Docker socket inside project containers.

## Continuous integration

Use GitHub Actions on Linux. The runner orchestrates Docker; Python linting,
typing, tests, package builds and wheel checks execute inside project
containers. Use Docker Buildx and build caching, with immutable action commit
references selected at implementation time. Docker documents the supported
[GitHub Actions integration](https://docs.docker.com/build/ci/github-actions/)
and [multi-stage build targets](https://docs.docker.com/build/building/multi-stage/).

Run on pull requests and pushes to `main`:

1. Validate Compose configuration and build the `ci` image from a clean checkout.
2. Run Ruff lint/format checks and ty once on the default locked environment.
3. Run pytest across Python 3.12/3.13/3.14 and Django 5.2/6.0/6.1, selecting the
   newest patch within each Django series. Test Ninja 1.7.1 in these nine jobs.
4. Add Python 3.12 + Django 5.2 jobs for Ninja 1.7.0, a plain-Django install
   without Ninja, an API-only install without the SDK/exporters, and the minimum
   supported OpenTelemetry 1.44 dependency set.
5. Run one separate collector smoke job using the default locked dependency set.
6. Build wheel and sdist once, run `twine check`, rebuild a wheel from the sdist
   and test both wheel origins in clean containers outside the source tree.
7. Upload distribution artifacts, test reports, coverage and collector logs as
   appropriate. Failures must preserve useful diagnostics and clean up services.

The default environment uses the committed lockfile without modifying it.
Compatibility jobs resolve their explicit version constraints in an isolated
environment inside the same container tooling, without rewriting `uv.lock`.
Invoke that environment's Python directly, avoiding an implicit resync to the
default dependency set. Do not pass incompatible version constraints silently.

Wheel tests must not import from the source checkout or an editable install.
Use a temporary Django project outside `/workspace`, install the wheel normally,
and assert package imports resolve to the installed distribution. Include a
pure-Django run without optional extras. Feed the built wheel, rather than
editable source, into the collector smoke test.

Tests changing global providers, hook installation or prefork state use
subprocesses inside the container. Use explicit providers for ordinary tests.
Do not depend on resetting OpenTelemetry's process-global singleton state.

## Continuous delivery

The released product is a Python wheel and sdist. Docker is the build and
verification environment; a deployed service image or container registry is not
required for this library.

On a version tag `vX.Y.Z`:

1. Check that the tag matches package metadata and the commit belongs to `main`.
2. Run the same container validation, compatibility matrix and artifact checks
   used for CI against the tagged commit.
3. Build distributions once in Docker, then smoke-test and retain their hashes.
   All publishing steps consume those exact artifacts; do not rebuild them.
4. Publish prerelease versions to TestPyPI and stable versions to PyPI through
   separate GitHub environments. Use PyPA's official container-based publish
   action with OIDC Trusted Publishing and attestations.
5. Keep `id-token: write` confined to the publishing job. Ordinary PR/test jobs
   have read-only repository permissions and no publishing credentials.

The PyPA publishing action is a Docker action; artifact building and testing
remain in this project's Dockerfile. Use the documented
[PyPI Trusted Publishing flow](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
instead of custom forwarding of OIDC tokens into project containers.

Repository owner, package-name availability and PyPI/TestPyPI publisher records
must be verified when connecting release workflows. They are not available in
this empty repository yet. Preparing workflow files is part of implementation;
creating external publisher records or performing a release is a separate
maintainer action. Do not add Docker Hub/GHCR publishing, deployment platforms,
Kubernetes or a dashboard stack to this release.
