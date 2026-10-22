# Docker development and CI/CD

This document describes the Docker, compatibility and release tooling that is
implemented in this checkout. The files are the source of truth: `Dockerfile`,
`compose.yaml`, `scripts/`, `ops/otel-collector.yaml` and `.github/workflows/`.
The [implementation plan](implementation-plan.md) records the design, and
[task status](tasks.md) records what has actually been verified.

## Development environment

Require Docker Engine/Desktop with Compose v2 and BuildKit. Host Python, package
managers and virtual environments are not required.

The single `Dockerfile` has `dev`, `ci` and `dist` targets. The supported Python
images are pinned by digest for 3.12, 3.13 and 3.14; `PYTHON_VERSION` defaults to
`3.14`. uv is installed at a pinned `UV_VERSION` (currently `0.13.5`), and the
committed `uv.lock` is installed with `uv sync --locked`. Runtime dependencies
and development tools live under `/opt/venv`, so mounting the checkout at
`/workspace` does not hide the environment. The development image installs the
`ninja` and `otlp` extras plus the `dev` dependency group and installs the
project editable from `/workspace`.

| Target | Purpose |
| --- | --- |
| `dev` | Locked extras/dev group, editable source install, non-root user and tooling. |
| `ci` | Same dependency/tool baseline with the checkout copied into the image. |
| `dist` | Export only the wheel and sdist built inside the container. |

The `dev` and `ci` images run as a non-root user. `LOCAL_UID` and `LOCAL_GID`
default to `1000` and can be set at build time so mounted files are not created
as root on the host. `.dockerignore` excludes Git internals, local environments,
caches, credentials and build output.

Build the development image and start the demo:

```bash
docker compose build \
  --build-arg LOCAL_UID="$(id -u)" \
  --build-arg LOCAL_GID="$(id -g)" dev
docker compose up dev
```

The demo is published only on `127.0.0.1:8000`. The `dev` command selects
`tests.project.demo_settings` and runs:

```text
uvicorn tests.project.asgi:application --host 0.0.0.0 --port 8000 --reload
```

That settings module opts into the installed app and the Ninja URLconf. It is set
inside the service command only. Ordinary pytest runs use
`tests.project.settings` from `pyproject.toml`, which intentionally keeps the
package out of `INSTALLED_APPS` and `MIDDLEWARE`; this preserves plain-Django
tests and prevents demo configuration from leaking into the suite.

Run the checks inside the mounted development container:

```bash
docker compose run --rm dev python -m pytest
docker compose run --rm dev bash scripts/check.sh all
docker compose run --rm dev python -m ruff check .
docker compose run --rm dev python -m ruff format --check .
docker compose run --rm dev ty check --python /opt/venv src
```

`scripts/check.sh` accepts `lint`, `test` or `all` and forwards pytest arguments.
Its lint mode runs Ruff, Ruff format check and `ty check --python /opt/venv src`.
The explicit `--python /opt/venv` matters because a host-created `.venv` under the
mounted checkout can otherwise be detected by ty and produce a `pyvenv.cfg`
warning.

The image build performs the dependency sync; normal test commands must not
resync or rewrite the lockfile. Rebuild the image after changing `pyproject.toml`
or `uv.lock`. uv is used for locking, syncing, exporting requirement sets and
building distributions; it is a development tool, not a runtime dependency. The
relevant container commands are:

```bash
# During the image build (project installed editable from /workspace):
uv sync --locked --no-install-project --extra ninja --extra otlp --group dev
uv sync --locked --extra ninja --extra otlp --group dev

# Distribution target:
uv build --no-build-isolation --out-dir /dist
uv run --no-sync twine check /dist/*
```

For a dependency set that differs from the committed lock, use the isolated
compatibility helper rather than changing `/opt/venv`:

```bash
docker compose run --rm dev bash -c '
  COMPAT_EXTRAS=ninja,otlp \
  scripts/compat.sh "Django~=5.2.0" "django-ninja==1.7.1" &&
  /tmp/compat-venv/bin/python -m pytest
'
```

`scripts/compat.sh` exports the locked dev requirements, removes the locked
Django/Ninja/OpenTelemetry entries, installs the requested constraints into a
fresh `/tmp/compat-venv`, and verifies that the package imports from the
installed distribution rather than `/workspace/src`. It does not modify
`uv.lock`. The `COMPAT_EXTRAS` value controls which optional extras are installed.

## Distribution checks

Build the wheel and sdist once in Docker and export them:

```bash
docker build --target dist --output dist .
```

Buildx creates a relative local-export destination as mode `0700`. CI sets the
exported directory to `0755` and the wheel and sdist to `0644` so the verifier
can read them under a different UID. The verifier remains non-root and receives
the artifacts through a read-only mount.

The `dist` stage uses the locked `uv_build` backend with
`uv build --no-build-isolation` and runs `twine check`. BuildKit cache mounts
keep uv downloads out of the exported result. Verify the exported artifacts from
a clean environment outside the source tree:

```bash
docker compose run --rm dev python scripts/check-dist.py --self-test
docker compose run --rm dev python scripts/check-dist.py dist
```

`scripts/check-dist.py` requires exactly one wheel and one sdist. It runs
`twine check --strict`, creates a temporary venv, installs the wheel with uv,
imports it from outside `/workspace`, confirms the import belongs to the
installed distribution, rebuilds a wheel from the sdist and repeats the import
check. Remove stale files from `dist` before building if more than one version is
present.

Final verification installed both the original wheel and the wheel rebuilt from
the sdist into clean environments outside the source tree. Both passed
`twine check --strict`, and an actual plain-Django request returned HTTP 200.

## Local collector smoke test

The `telemetry` Compose profile provides:

- `collector-init`: a one-shot root-owned initializer that prepares the shared
  volume for the collector user;
- `collector`: the pinned official OpenTelemetry Collector contrib image, running
  non-root and writing traces, metrics and logs as JSONL;
- `smoke`: a one-shot non-root service with the built wheel and collector output
  mounted read-only.

The smoke service installs the wheel from `./dist` into a fresh temporary venv,
along with the locked runtime requirements and the same Uvicorn version as the
development image. It starts the smoke ASGI application, requests a success
route, a Ninja validation failure and an uncaught error, then checks all three
signals. Assertions cover one SERVER span per route, one validation warning, one
correlated exception error, validation-input omission and both HTTP metrics.

The final built-wheel live-collector run passed with three SERVER spans, one WARN
validation event, one ERROR exception event and both `http.server.request.duration`
and `http.server.active_requests` metrics present.

Run it with a fresh collector volume:

```bash
docker compose --profile telemetry down -v
docker compose --profile telemetry up -d --wait collector
docker compose --profile telemetry run --rm smoke
docker compose --profile telemetry down -v
```

The smoke probe rejects non-empty existing signal files rather than letting old
records satisfy assertions. A repeated run without `down -v` is expected to fail
with a stale-output diagnostic. The collector output is read-only to the smoke
container; do not modify or truncate those files while the collector is running.
On failure, `scripts/smoke.py` prints signal-file sizes and a bounded Uvicorn
output tail. Collector logs are available with:

```bash
docker compose --profile telemetry logs collector
```

Ordinary unit tests use explicit in-memory providers and do not require the
collector. Tests that exercise global providers, hook installation, fork
behavior or interpreter shutdown use subprocesses.

## Continuous integration

`.github/workflows/ci.yml` runs on pull requests and pushes to `main`, and is
reusable from the release workflow. It has read-only repository permissions and
uses Buildx with GitHub Actions cache. All third-party actions are pinned to
commit SHAs: `actions/checkout` v7.0.1, `docker/setup-buildx-action` v4.4.1,
`docker/build-push-action` v7.4.0, `actions/upload-artifact` v7.0.2,
`actions/download-artifact` v8.0.2 and `pypa/gh-action-pypi-publish` v1.14.3.
Docker documents the [GitHub Actions integration](https://docs.docker.com/build/ci/github-actions/)
and [multi-stage build targets](https://docs.docker.com/build/building/multi-stage/).

The jobs are:

1. **Lint and default environment** — validate `docker compose config`, build the
   `ci` image, run the `check-dist.py` and `check-release.py` self-tests, then run
   `scripts/check.sh all` against the committed locked environment.
2. **Python/Django matrix** — nine jobs for Python 3.12/3.13/3.14 and Django
   5.2/6.0/6.1, using `Django~=X.0` and Ninja 1.7.1 through `scripts/compat.sh`.
3. **Compatibility jobs** — four additional environments: Ninja 1.7.0, plain
   Django without Ninja, API-only without the SDK/exporters, and the minimum
   OpenTelemetry 1.44 dependency set. These use Python 3.12 and Django 5.2.
4. **Artifacts** — build wheel/sdist from the `dist` target, verify both wheel
   origins with `scripts/check-dist.py` outside the source tree, record SHA-256
   hashes and upload the checked distributions.
5. **Collector smoke** — download the checked distribution artifact, verify its
   hashes, start the collector under a unique Compose project name, run the
   smoke service, capture collector logs, and tear down the profile with `down -v`.

Compatibility jobs install into `/tmp/compat-venv` and invoke that interpreter
directly. This avoids an implicit resync to the default dependency set and keeps
the committed lockfile unchanged. JUnit reports are uploaded when present.

Local verification completed on 2026-10-22:

- `scripts/check.sh all` passed Ruff lint, Ruff format, ty and 227 tests in the
  default locked environment. That suite includes the real OpenTelemetry Django
  instrumentation coexistence coverage.
- The nine Python/Django Docker jobs (Python 3.12/3.13/3.14 and Django
  5.2/6.0/6.1) each passed with 225 tests and 2 skips.
- The Ninja 1.7.0 and OpenTelemetry 1.44 baseline jobs each passed with 225 tests
  and 2 skips.
- The plain-Django-without-Ninja job passed with 185 tests and 42 skips; the
  API-only-without-SDK job passed with 84 tests and 143 skips.

The CI scripts and workflows are committed in `f07ea9b`; actionlint and the
distribution/release self-checks pass. The `v0.1.0` GitHub Actions run passed
the release gate and CI, then failed at PyPI publishing because the package
metadata used the old distribution name while the intended PyPI project is
`django-api-opentelemetry`. The package metadata and release links now use the
intended name; the corrected distributions still need a tagged publish run.

## Continuous delivery

`.github/workflows/release.yml` is prepared for version tags `vX.Y.Z`:

1. `release-gate` checks out full history, requires the tagged commit to be an
   ancestor of `origin/main`, and runs `scripts/check-release.py` to verify a
   normalized PEP 440 version matching `project.version`. Prereleases are
   identified from the tag.
2. The reusable CI workflow runs the same container checks on the tagged commit.
3. Prereleases publish to TestPyPI and stable versions publish to PyPI using the
   official PyPA container action with OIDC Trusted Publishing and attestations.
   The downloaded `dist` artifact is checked against `SHA256SUMS`; publishing
   consumes the exact artifacts produced and verified by CI.
4. Only the publishing jobs receive `id-token: write` and
   `attestations: write`. Ordinary test jobs remain read-only.

The publishing flow follows PyPI's documented
[Trusted Publishing setup](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
rather than forwarding OIDC tokens into project containers.

The `v0.1.0` GitHub Release exists. Its first attached artifacts were built
under the old distribution name and should be replaced by the corrected,
verified distributions. The first PyPI upload did not complete. Confirm the
Trusted Publisher record targets `django-api-opentelemetry`. The workflow uses
the `testpypi` and `pypi` GitHub environments and binds the publisher identity
to the repository, `release.yml`, and the matching environment.

The distribution name is `django-api-opentelemetry`, and its Python import
package is `django_api_opentelemetry`.

Docker Hub/GHCR publishing, deployment platforms, Kubernetes and dashboards are
not part of this release.
