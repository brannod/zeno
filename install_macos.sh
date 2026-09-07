#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3.11+ is required. Install it from https://www.python.org/downloads/macos/ and run this script again."
  exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python executable not found: $PYTHON_BIN"
  exit 1
fi

if [[ ! -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
  echo "Creating Zeno's private Python environment..."
  "$PYTHON_BIN" -m venv "$SCRIPT_DIR/.venv"
fi

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
echo "Installing Zeno 3.6.19 dependencies..."
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r requirements.txt
echo "Installing Chromium for Live Browser..."
"$VENV_PYTHON" -m playwright install chromium
echo
echo "Zeno 3.6.19 is installed. Run ./start_macos.sh to launch it."
