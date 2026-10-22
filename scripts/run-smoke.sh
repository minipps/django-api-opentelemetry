#!/usr/bin/env bash
# Install the exact built wheel into a fresh venv and run the boundary smoke test.
set -euo pipefail

wheels=(/artifacts/*.whl)
if [[ ${#wheels[@]} -ne 1 || ! -f ${wheels[0]} ]]; then
    echo "expected exactly one wheel in /artifacts, found ${#wheels[@]}" >&2
    exit 1
fi
wheel=${wheels[0]}
venv=/tmp/smoke-venv
requirements=/tmp/smoke-requirements.txt

unset UV_PROJECT_ENVIRONMENT PYTHONPATH DJANGO_SETTINGS_MODULE
cd /workspace
uv export --locked --no-dev --extra ninja --extra otlp --no-emit-project \
    --format requirements.txt --output-file "$requirements"
uvicorn_version="$(
    /opt/venv/bin/python -c \
        'import importlib.metadata; print(importlib.metadata.version("uvicorn"))'
)"

uv venv --clear --python /opt/venv/bin/python "$venv"
uv pip install --python "$venv/bin/python" -r "$requirements"
uv pip install --python "$venv/bin/python" "uvicorn==${uvicorn_version}"
uv pip install --python "$venv/bin/python" --no-deps "$wheel"

cd /tmp
export SMOKE_TELEMETRY_DIR=/telemetry
export SMOKE_OTLP_ENDPOINT=http://collector:4318
exec "$venv/bin/python" /smoke/smoke.py
