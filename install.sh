#!/usr/bin/env bash
set -euo pipefail

NAVI_SPEC="${NAVI_SPEC:-navi_agent @ https://github.com/abxxvrv/navi_agent/archive/refs/heads/gpt.zip}"
NAVI_PYTHON="${NAVI_PYTHON:->=3.11}"

echo "Installing Navi from: $NAVI_SPEC"

# Ensure uv is available. The official installer drops a standalone binary into
# ~/.local/bin and never touches the system Python, so it sidesteps PEP 668.
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# uv installs into ~/.local/bin; make it visible to this shell.
export PATH="$HOME/.local/bin:$PATH"
if command -v uv >/dev/null 2>&1; then
  UV_BIN="uv"
else
  UV_BIN="$HOME/.local/bin/uv"
fi

# Install Navi. uv provisions a matching interpreter, downloading a managed
# CPython when nothing on the system satisfies $NAVI_PYTHON.
"$UV_BIN" tool install --force --python "$NAVI_PYTHON" "$NAVI_SPEC"

# Persist the tool bin directory on PATH for future shells.
"$UV_BIN" tool update-shell || true

export PATH="${UV_TOOL_BIN_DIR:-$HOME/.local/bin}:$PATH"
if command -v navi >/dev/null 2>&1; then
  NAVI_CMD="navi"
else
  NAVI_CMD="${UV_TOOL_BIN_DIR:-$HOME/.local/bin}/navi"
fi

# Under `curl | bash` the script's stdin is the pipe (already at EOF), so the
# interactive prompts in `navi init` must read from the controlling terminal.
if [ -e /dev/tty ] && (exec </dev/tty) 2>/dev/null; then
  "$NAVI_CMD" init </dev/tty
else
  echo
  echo "No interactive terminal detected; skipping model setup."
  echo "Finish setup later with: $NAVI_CMD init"
fi

echo
echo "Navi installed. Run: navi doctor"
