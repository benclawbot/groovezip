#!/usr/bin/env bash
# Convenience launcher for the YouTube Playlist -> MP3 ZIP app.
set -e
cd "$(dirname "$0")"
exec python3 app.py
