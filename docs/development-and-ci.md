# Docker development and CI/CD

This document describes the Docker, compatibility and release tooling that is
implemented in this checkout. The files are the source of truth: `Dockerfile`,
`compose.yaml`, `scripts/`, `ops/otel-collector.yaml` and `.github/workflows/`.
The [implementation plan](implementation-plan.md) records the design, the
[DRF support plan](drf-support.md) records the frozen framework contract, and
[task status](tasks.md) records what has actually been verified. Repository-level
controls are listed in [CI/CD supply-chain controls](supply-chain-security.md).

## Development environment

Require Docker Engine/Desktop with Compose v2 and BuildKit. Host Python, package
managers and virtual environments are not required.

The single `Dockerfile` has `dev`, `ci` and `dist` targets. The supported Python
images are pinned by digest for 3.12, 3.13 and 3.14; `PYTHON_VERSION` defaults to
`3.14`. uv is copied from the digest-pinned official
`ghcr.io/astral-sh/uv:0.13.5` image instead of being installed with pip, and the
committed `uv.lock` is installed with `uv sync --locked`. Runtime dependencies
and development tools live under `/opt/venv`, so mounting the checkout at
`/workspace` does not hide the environment. The development image installs the
`ninja`, `drf` and `otlp` extras plus the `dev` dependency group and installs the
project editable from `/workspace`. The lockfile resolves the optional DRF range
to 3.18.3 for this default environment.

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
Its lint mode runs Ruff, Ruff format check, `ty check --python /opt/venv src`
and `zizmor --offline --persona auditor .github`. The explicit `--python /opt/venv`
matters because a host-created `.venv` under the mounted checkout can otherwise
be detected by ty and produce a `pyvenv.cfg` warning. zizmor is part of the
locked `dev` dependency group, so workflow and Dependabot security checks use the same
reproducible environment as the rest of CI. The `ci` image includes `.github` so
the checked-in workflows and Dependabot configuration can be audited inside the
image.

The image build performs the dependency sync; normal test commands must not
resync or rewrite the lockfile. Rebuild the image after changing `pyproject.toml`
or `uv.lock`. uv is used for locking, syncing, exporting requirement sets and
building distributions; it is a development tool, not a runtime dependency. The
relevant container commands are:

```bash
# During the image build (project installed editable from /workspace):
uv sync --locked --no-install-project --extra ninja --extra drf --extra otlp --group dev
uv sync --locked --extra ninja --extra drf --extra otlp --group dev

# Distribution target:
uv build --no-build-isolation --out-dir /dist
uv run --no-sync twine check /dist/*
```

For a dependency set that differs from the committed lock, use the isolated
compatibility helper rather than changing `/opt/venv`:

```bash
docker compose run --rm dev bash -c '
  COMPAT_EXTRAS=ninja,drf,otlp \
  scripts/compat.sh "Django~=5.2.0" "django-ninja==1.7.1" \
    "djangorestframework==3.18.3" &&
  /tmp/compat-venv/bin/python -m pytest
'
```

`scripts/compat.sh` exports the locked dev requirements, removes the locked
Django/Ninja/DRF/OpenTelemetry entries, installs the requested constraints into a
fresh `/tmp/compat-venv`, and verifies that the package imports from the installed
distribution rather than `/workspace/src`. It does not modify `uv.lock`. The
`COMPAT_EXTRAS` value controls which optional extras are installed; omitting
`drf` or `ninja` keeps those absence conditions real.

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
development image. It starts the smoke ASGI application and issues three
baseline Django/Ninja requests (a plain Django success, a Ninja validation
failure and a plain Django uncaught error) plus three DRF requests (success,
validation failure, uncaught error), then checks all three signals.
Assertions cover exactly six SERVER spans, one Ninja and one DRF validation
warning, two correlated exception errors, the expected DRF operation spans,
validation-input and field-name omission, and exactly one HTTP duration
measurement per route.

The `v0.1.0` built-wheel live-collector run (before DRF support) passed with
three SERVER spans, one WARN validation event, one ERROR exception event and
both `http.server.request.duration` and `http.server.active_requests` metrics
present. DRF smoke evidence is recorded separately in [task status](tasks.md).

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
reusable from the release workflow. It runs on `ubuntu-24.04` with explicit job
timeouts, read-only repository permissions and `persist-credentials: false` on
every checkout. All third-party actions are pinned to full commit SHAs:
`actions/checkout` v7.0.1, `docker/setup-buildx-action` v4.4.1,
`docker/build-push-action` v7.4.0, `actions/upload-artifact` v7.0.2,
`actions/download-artifact` v8.0.2 and `pypa/gh-action-pypi-publish` v1.14.3.
Docker base images, the uv bootstrap image and the BuildKit image are pinned by
digest, and the Dockerfile frontend directive is digest-pinned as well. BuildKit
is passed inline through `driver-opts` on every Buildx setup, so updating that
digest is an explicit manual review rather than a tag-following update. Docker
documents the [GitHub Actions integration](https://docs.docker.com/build/ci/github-actions/)
and [multi-stage build targets](https://docs.docker.com/build/building/multi-stage/).

The Python/Django matrix and compatibility jobs share GitHub Actions Buildx
caches under `ci-tests-py<version>`, one scope per Python version. Django versions
and compatibility cases reuse the same base CI image. `mode=max` preserves
intermediate dependency layers. GitHub's
[cache access rules](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache)
also scope access by branch, tag or PR merge ref; PR caches cannot overwrite the
default branch's cache.

Matrix and compatibility jobs use these caches during both ordinary and release
CI. Lint, release-gate, distribution and collector jobs do not use external
caches. Release artifacts therefore remain isolated from the shared test
caches. Local Docker/BuildKit layer reuse within a fresh runner and the
Dockerfile's uv cache mounts remain.

The jobs are:

1. **Lint and default environment** — validate `docker compose config`, build the
   `ci` image, run the `check-dist.py` and `check-release.py` self-tests, then run
   `scripts/check.sh all` against the committed locked environment. The same
   locked image runs `zizmor --offline --persona auditor .github` and fails on
   any finding, including low-severity auditor findings with no suppressions.
2. **Python/Django matrix** — nine jobs for Python 3.12/3.13/3.14 and Django
   5.2/6.0/6.1, using `Django~=X.0`, Ninja 1.7.1, DRF 3.18.3 and the
   `ninja,drf,otlp` extras through `scripts/compat.sh`.
3. **Compatibility jobs** — seven additional environments: Ninja 1.7.0, DRF
   3.16.1, DRF 3.17.2, plain Django without Ninja or DRF, API-only without the
   SDK/exporters, DRF without Ninja, and the minimum OpenTelemetry 1.44
   dependency set. These use Python 3.12 and Django 5.2.
4. **Artifacts** — build wheel/sdist from the `dist` target on a fresh runner
   without external cache, verify both wheel origins with `scripts/check-dist.py`
   outside the source tree, record SHA-256 hashes and upload the checked
   distributions.
5. **Collector smoke** — download the checked distribution artifact, verify its
   hashes, start the collector under a unique Compose project name, run the
   smoke service, capture collector logs, and tear down the profile with `down -v`.

Compatibility jobs install into `/tmp/compat-venv` and invoke that interpreter
directly. This avoids an implicit resync to the default dependency set and keeps
the committed lockfile unchanged. JUnit reports are uploaded when present.

Historical `v0.1.0` verification completed on 2026-10-22:

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
distribution/release self-checks pass. The corrected `v0.1.0` run on commit
`80d30d1` passed the release gate, all CI jobs and PyPI publishing. An initial
attempt failed because the package metadata used the old distribution name;
the corrected distributions are now published as `django-api-opentelemetry`.

DRF implementation verification for the integration included in `v0.2.0`
(recorded before CI hardening added six supply-chain regression tests):

- The updated dev image installs the locked `ninja`, `drf` and `otlp` extras and
  resolves DRF 3.18.3.
- `docker compose run --rm dev bash scripts/check.sh all` passed Ruff lint, Ruff
  format, ty on `src` and 288 tests in the default locked environment. The DRF
  conformance module contributes 43 of those tests.
- The Docker `dist` target built the wheel and sdist, and
  `scripts/check-dist.py` verified the original wheel and the wheel rebuilt from
  the sdist through clean installs and plain-Django requests outside the source
  tree.
- The isolated built-wheel collector smoke passed with six SERVER spans, two WARN
  validation events, two ERROR exception events, three `django_drf.dispatch`
  spans, three `django_drf.initial` spans, two `django_drf.render` spans and both
  HTTP metrics.
- All nine Python/Django matrix jobs (Python 3.12/3.13/3.14 × Django
  5.2/6.0/6.1) passed with 286 tests and 2 expected contrib-coexistence skips
  each.
- All seven compatibility jobs passed. DRF 3.16.1, DRF 3.17.2 and the
  OpenTelemetry 1.44 baseline each passed 286 with 2 skips; Ninja 1.7.0 without
  DRF passed 243 with 45 skips; plain Django without Ninja or DRF passed 203
  with 85 skips; API-only without the SDK/exporters passed 106 with 182 skips;
  DRF without Ninja passed 246 with 42 skips.
- Local isolated runs on Python 3.14 and Django 5.2.18 reproduced the DRF
  3.16.1, DRF 3.17.2, DRF-without-Ninja and plain-Django results with the
  project installed outside `/workspace`.

These are local Docker results from the feature branch. A branch push does not
trigger this workflow, which runs on pull requests and pushes to `main`, and a
temporary DNS error delayed the feature-branch pushes, so no remote CI run or
release is claimed.

Supply-chain hardening verification in this checkout on 2026-10-22:

- The rebuilt `ci` image ran `scripts/check.sh all`: Ruff lint, Ruff format, ty,
  `zizmor --offline --persona auditor .github` and 294 tests passed. zizmor
  audited the Dependabot configuration and both workflows in auditor mode, and
  reported no findings and no suppressions.
- The `ci` and `dist` targets built through BuildKit `v0.33.1` pinned by digest,
  with the Dockerfile frontend digest and the digest-pinned uv bootstrap image.
  `docker compose config` accepted the updated build arguments.
- The exported wheel and sdist passed `scripts/check-dist.py` in the same
  environment: `twine check --strict`, clean wheel import, rebuild from the
  sdist, and the plain-Django request all passed.
- `scripts/check-release.py --self-test` passed inside the image; the real
  `v0.1.0` check returned `version=0.1.0` and `prerelease=false`, while a
  malformed `v0.2` tag was rejected.
- Both workflow files parse, every `actions/checkout` sets
  `persist-credentials: false`, and shared Buildx caches are restricted to the
  matrix and compatibility jobs.
  No remote GitHub Actions run is claimed for this branch.

The latest published Actionlint (v1.7.12) predates GitHub's July 2026 `$/.`
self-repository syntax, so it cannot validate that reference directly. The
release workflow keeps `$/.github/workflows/ci.yml`; zizmor and GitHub's
documented syntax validate it, and Actionlint v1.7.12 passes both workflows when
only that token is normalized in a temporary copy for the older parser.

## Continuous delivery

`.github/workflows/release.yml` is prepared for version tags `vX.Y.Z`:

1. `release-gate` is gated on a genuine `push` of a `refs/tags/v*` tag. It checks
   out full history with credentials not persisted, requires the tagged commit to
   be an ancestor of `origin/main`, and only then builds the existing `ci` image.
   The `check-release.py` self-test and tag check run inside that image's
   uv-locked environment, so no `pip install` step fetches an unpinned or
   unhashed helper. The check verifies a normalized PEP 440 version matching
   `project.version`; prereleases are identified from the tag.
2. The reusable CI workflow runs the same container checks on the tagged commit.
   Release CI uses the fixed `ci-*` namespace while the release orchestrator uses
   `release-*`, so caller and callee cannot cancel one another; tag-triggered CI
   also never cancels in progress.
3. Prereleases publish to TestPyPI and stable versions publish to PyPI in fresh
   jobs that only download the `dist` artifact, verify it against `SHA256SUMS`,
   and invoke the pinned official PyPA action. They do not check out source or
   build anything under `id-token: write`.
4. Only the publishing jobs receive `id-token: write`; no `attestations: write`
   permission is granted. PyPA's action exchanges that OIDC identity for a
   short-lived PyPI token and signs PEP 740 attestations with the same identity,
   as documented in its
   [Trusted Publishing usage](https://github.com/pypa/gh-action-pypi-publish#trusted-publishing).

The wheel and sdist are built once by CI on a fresh runner with no external
cache. Their SHA-256 hashes are recorded in `SHA256SUMS`, and the `dist` artifact
is the only handoff. Every later job verifies those exact hashes before use, so
publishing consumes the same immutable bytes that CI verified rather than a
rebuild. Pull-request and ordinary CI jobs retain read-only repository tokens,
with no secrets or OIDC permission. Only matrix and compatibility jobs
share Actions build caches.

The publishing flow follows PyPI's documented
[Trusted Publishing setup](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
rather than forwarding OIDC tokens into project containers.

The `v0.1.0` GitHub Release and PyPI project are published. The release assets
are the corrected, CI-verified wheel, source distribution and checksum file.
The workflow uses the `testpypi` and `pypi` GitHub environments and binds the
Trusted Publisher identity to the repository, `release.yml`, and matching
environment.

The distribution name is `django-api-opentelemetry`, and its Python import
package is `django_api_opentelemetry`.

Docker Hub/GHCR publishing, deployment platforms, Kubernetes and dashboards are
not part of this release.
