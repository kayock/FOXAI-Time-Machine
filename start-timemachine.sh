#!/usr/bin/env bash
# FOXAI Time Machine — Linux starter

set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

echo ""
echo " FOXAI Time Machine"
echo " ------------------"
echo ""

if ! command -v python3 >/dev/null 2>&1; then
  echo " Python 3 was not found."
  echo ""
  echo " Install Python 3 with your distribution's package manager, e.g.:"
  echo "   sudo apt install python3"
  echo " then try again. (sudo is only for installing Python — not for Time Machine.)"
  echo ""
  echo " Troubleshooting fallback:"
  echo "   cd \"$ROOT\""
  echo "   python3 serve.py"
  echo " then open http://127.0.0.1:8420"
  echo ""
  exit 1
fi

echo " Using: $(command -v python3)"
echo ""
exec python3 "$ROOT/launch.py"
