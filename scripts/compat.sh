#!/usr/bin/env bash
# Build an isolated compatibility environment from the committed lockfile.
#
# The dev dependency group supplies pinned test tools; compatibility arguments
# then select the Django/Ninja/DRF/OpenTelemetry versions for this job. The
# project is installed as a normal (non-editable) distribution into /tmp, never
# into the image's default /opt/venv.
#
# Usage: scripts/compat.sh "Django~=5.2.0" "django-ninja==1.7.1" ...
# Environment: COMPAT_EXTRAS=ninja,drf,otlp, COMPAT_VENV=/tmp/compat-venv
set -euo pipefail

project="${COMPAT_PROJECT:-/workspace}"
venv="${COMPAT_VENV:-/tmp/compat-venv}"
dev_requirements="${COMPAT_DEV_REQUIREMENTS:-/tmp/compat-dev-requirements.txt}"
tools_requirements="${COMPAT_TOOLS_REQUIREMENTS:-/tmp/compat-tools-requirements.txt}"
extras="${COMPAT_EXTRAS:-}"

if [[ $# -eq 0 ]]; then
    echo "usage: $0 REQUIREMENT [REQUIREMENT ...]" >&2
    exit 2
fi
if [[ ! -f "${project}/pyproject.toml" ]]; then
    echo "compat: project not found at ${project}" >&2
    exit 1
fi

mkdir -p "$(dirname "${dev_requirements}")"
rm -rf "${venv}"

# Preserve uv.lock: --locked fails rather than updating it.
uv export \
    --quiet \
    --locked \
    --only-group dev \
    --format requirements-txt \
    --no-hashes \
    --no-emit-project \
    --output-file "${dev_requirements}"

# Runtime packages are controlled by the compatibility arguments below. Keep
# the dev-group tool pins but drop these so a 5.2 job does not inherit the lock's
# default Django/DRF/OpenTelemetry selection.
grep -Ev '^(django==|django-ninja==|djangorestframework==|opentelemetry[-.])' \
    "${dev_requirements}" > "${tools_requirements}"

project_spec="${project}"
if [[ -n "${extras}" ]]; then
    project_spec="${project}[${extras}]"
fi

uv venv --python "$(command -v python)" "${venv}"
uv pip install \
    --python "${venv}/bin/python" \
    --requirements "${tools_requirements}" \
    --constraints "${tools_requirements}" \
    "$@" \
    "${project_spec}"

"${venv}/bin/python" - <<'PY'
from __future__ import annotations

import importlib.metadata as metadata
import pathlib

import django_api_opentelemetry

for name in (
    "Django",
    "django-ninja",
    "djangorestframework",
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-http",
    "pytest",
):
    try:
        print(f"{name}=={metadata.version(name)}")
    except metadata.PackageNotFoundError:
        print(f"{name}=<not installed>")

origin = pathlib.Path(django_api_opentelemetry.__file__).resolve()
print(f"project={origin}")
if "/workspace/src" in origin.as_posix():
    raise SystemExit(f"compat project imported from the source tree: {origin}")
PY
