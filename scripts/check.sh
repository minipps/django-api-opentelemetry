#!/usr/bin/env bash
# Run the shared container checks. Usage: scripts/check.sh [lint|test|all] [pytest args...]
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

mode="${1:-all}"
case "${mode}" in
    lint|test|all) ;;
    *)
        echo "usage: $0 [lint|test|all] [pytest args...]" >&2
        exit 2
        ;;
esac
shift || true

if [[ "${mode}" == "lint" || "${mode}" == "all" ]]; then
    python -m ruff check .
    python -m ruff format --check .
    ty check --python /opt/venv src
fi

if [[ "${mode}" == "test" || "${mode}" == "all" ]]; then
    python -m pytest "$@"
fi
