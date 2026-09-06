#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"
export PYTHONUNBUFFERED=1

if [[ ! -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
  echo "Zeno's macOS environment is not installed yet. Run ./install_macos.sh first."
  exit 1
fi

exec "$SCRIPT_DIR/.venv/bin/python" -u zeno.py "$@"
