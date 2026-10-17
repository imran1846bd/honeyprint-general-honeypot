#!/bin/sh
# Bootstrap the local core dependencies, then supervise the default custom lab.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_BIN=${HONEYPRINT_PYTHON:-python3}
VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    "$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10 or newer is required"'
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
if ! "$VENV_DIR/bin/python" -c 'import importlib.metadata as m; assert m.version("PyYAML") == "6.0.3"; assert m.version("Paramiko") == "4.0.0"' >/dev/null 2>&1; then
    "$VENV_DIR/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt"
fi
exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/quickstart.py" "$@"
