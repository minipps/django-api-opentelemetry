# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.14

FROM python:${PYTHON_VERSION}-slim AS base

ARG UV_VERSION=0.13.5

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH

RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /workspace

FROM base AS deps

COPY pyproject.toml uv.lock README.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --extra ninja --extra otlp --group dev

COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra ninja --extra otlp --group dev

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

COPY tests ./tests

CMD ["python", "-m", "pytest"]

FROM deps AS dist-build

RUN --mount=type=cache,target=/root/.cache/uv \
    uv build --no-build-isolation --out-dir /dist \
    && uv run --no-sync twine check /dist/*

FROM scratch AS dist

COPY --from=dist-build /dist/ /
