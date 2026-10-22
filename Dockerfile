# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

# Immutable slim bases. The named stages let callers keep a single
# PYTHON_VERSION build argument while each supported interpreter stays pinned to
# the manifest-list digest resolved from the official Python image.
ARG PYTHON_VERSION=3.14

# uv is copied from the official digest-pinned image. Dependabot tracks this
# Docker reference; review digest updates rather than restoring an ad-hoc
# package-manager bootstrap.
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2 AS python-3.12
FROM python:3.13-slim@sha256:27b7c9d4f13ca20bb1170148f02095d50a5dfce1f7f2dabf5ca273727e43b7ea AS python-3.13
FROM python:3.14-slim@sha256:0514bc76712d64f8bb48adcb95b1ab0d88c69fee95d62fe4c18ee948bee9b1e1 AS python-3.14

FROM python-${PYTHON_VERSION} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH

COPY --from=uv /uv /uvx /bin/

WORKDIR /workspace

FROM base AS deps

COPY pyproject.toml uv.lock README.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --extra ninja --extra drf --extra otlp --group dev

COPY src ./src

# uv sync installs the source tree editable at /workspace, so a host bind mount
# cannot hide /opt/venv and local edits remain visible.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra ninja --extra drf --extra otlp --group dev

FROM deps AS dev

ARG LOCAL_UID=1000
ARG LOCAL_GID=1000

RUN groupadd --gid "${LOCAL_GID}" app \
    && useradd --uid "${LOCAL_UID}" --gid "${LOCAL_GID}" --create-home --shell /bin/bash app \
    && chown -R app:app /workspace

ENV HOME=/home/app

USER app

CMD ["sleep", "infinity"]

FROM dev AS ci

# Copy the whole checkout (minus .dockerignore entries) so tests, scripts and
# shared example projects can evolve without changing this Dockerfile. The copy
# is owned by the unprivileged runtime user.
COPY --chown=app:app . .

CMD ["python", "-m", "pytest"]

FROM deps AS dist-build

# Build once from the checked-out source. uv_build is part of the locked dev
# group, so --no-build-isolation uses the same pinned backend that built the
# environment above.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv run --no-sync python -c "import uv_build" \
    && uv build --no-build-isolation --out-dir /dist \
    && uv run --no-sync twine check /dist/*

FROM scratch AS dist

COPY --from=dist-build /dist/ /
