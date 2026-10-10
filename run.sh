#!/usr/bin/env bash
# Convenience launcher for the YouTube Playlist -> MP3 ZIP app.
set -euo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

# Prefer a project environment when one has been created, while preserving the
# existing system Python setup for checkouts that do not use a virtualenv.
if [[ -x "$APP_DIR/.venv/bin/python" ]]; then
  PYTHON="$APP_DIR/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

exec "$PYTHON" "$APP_DIR/app.py"
