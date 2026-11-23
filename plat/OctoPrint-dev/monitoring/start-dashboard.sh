#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/monitoring/dashboard.py" "$@"
