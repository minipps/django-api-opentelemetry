# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

# Immutable slim bases. The named stages let callers keep a single
# PYTHON_VERSION build argument while each supported interpreter stays pinned to
# the manifest-list digest resolved from the official Python image.
ARG PYTHON_VERSION=3.14

# uv is copied from the official digest-pinned image. Dependabot tracks this
# Docker reference; review digest updates rather than restoring an ad-hoc
# package-manager bootstrap.
FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS python-3.12
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS python-3.13
FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS python-3.14

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
