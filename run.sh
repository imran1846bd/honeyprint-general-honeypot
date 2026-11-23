#!/bin/sh
# Bootstrap the selected honeypot with a supported Python interpreter.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -e "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    chmod 600 "$SCRIPT_DIR/.env"
fi
VENV_DIR="$SCRIPT_DIR/.venv"
supported_python() {
    "$1" -c 'import sys; raise SystemExit(not ((3, 11) <= sys.version_info[:2] <= (3, 13)))' >/dev/null 2>&1
}

if [ -n "${HONEYPRINT_PYTHON:-}" ]; then
    PYTHON_BIN=$HONEYPRINT_PYTHON
    if ! supported_python "$PYTHON_BIN"; then
        echo "HONEYPRINT_PYTHON must point to Python 3.11, 3.12 or 3.13." >&2
        exit 1
    fi
elif [ -x "$VENV_DIR/bin/python" ] && supported_python "$VENV_DIR/bin/python"; then
    PYTHON_BIN="$VENV_DIR/bin/python"
else
    PYTHON_BIN=
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && supported_python "$candidate"; then
            PYTHON_BIN=$candidate
            break
        fi
    done
    if [ -z "$PYTHON_BIN" ]; then
        echo "HoneyPrint requires Python 3.11 through 3.13. Install Python 3.13 or set HONEYPRINT_PYTHON to its path." >&2
        exit 1
    fi
fi

if [ -e "$VENV_DIR" ]; then
    rebuild=false
    if [ ! -x "$VENV_DIR/bin/python" ] || ! supported_python "$VENV_DIR/bin/python"; then
        rebuild=true
    elif [ -n "${HONEYPRINT_PYTHON:-}" ]; then
        current_minor=$("$VENV_DIR/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
        selected_minor=$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
        if [ "$current_minor" != "$selected_minor" ]; then rebuild=true; fi
    fi
    if [ "$rebuild" = true ]; then
        mkdir -p "$SCRIPT_DIR/.honeyprint"
        backup="$SCRIPT_DIR/.honeyprint/venv-incompatible-$(date +%Y%m%d%H%M%S)"
        while [ -e "$backup" ]; do backup="${backup}-1"; done
        mv "$VENV_DIR" "$backup"
        echo "Saved incompatible Python environment at $backup" >&2
    fi
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "Preparing HoneyPrint with $("$PYTHON_BIN" --version)" >&2
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
if ! "$VENV_DIR/bin/python" -c 'import importlib.metadata as m; assert m.version("PyYAML") == "6.0.3"; assert m.version("Paramiko") == "4.0.0"' >/dev/null 2>&1; then
    "$VENV_DIR/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt"
fi
exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/quickstart.py" "$@"
