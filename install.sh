#!/usr/bin/env bash
set -euo pipefail

NAVI_SPEC="${NAVI_SPEC:-https://github.com/abxxvrv/navi_agent/archive/refs/heads/gpt.zip}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "Installing Navi from: $NAVI_SPEC"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "python3 not found. Install Python 3.11+ first." >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ is required.")
PY

if ! "$PYTHON_BIN" -m pipx --version >/dev/null 2>&1; then
  echo "pipx not found. Installing pipx..."
  "$PYTHON_BIN" -m pip install --user pipx
  "$PYTHON_BIN" -m pipx ensurepath
fi

export PATH="${PIPX_BIN_DIR:-$HOME/.local/bin}:$PATH"

"$PYTHON_BIN" -m pipx install --force "$NAVI_SPEC"

if command -v navi >/dev/null 2>&1; then
  navi init
else
  "$HOME/.local/bin/navi" init
fi

echo
echo "Navi installed. Run: navi doctor"
