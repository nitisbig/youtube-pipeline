#!/usr/bin/env bash
set -e

# Resolve directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Launch YouTube Video Pipeline GUI
exec /usr/bin/python3 main.py "$@"
