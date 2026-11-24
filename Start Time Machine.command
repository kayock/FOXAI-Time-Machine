#!/bin/bash
# FOXAI Time Machine — macOS double-click starter (.command)
# Right-click → Open the first time if Gatekeeper blocks it.

set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"

echo ""
echo " FOXAI Time Machine"
echo " ------------------"
echo ""

if ! command -v python3 >/dev/null 2>&1; then
  echo " Python 3 was not found."
  echo ""
  echo " Install Python 3 from https://www.python.org/downloads/"
  echo " or via your preferred package manager, then try again."
  echo ""
  echo " Troubleshooting fallback:"
  echo "   cd \"$ROOT\""
  echo "   python3 serve.py"
  echo " then open http://127.0.0.1:8420"
  echo ""
  read -r -p "Press Enter to close… " _
  exit 1
fi

echo " Using: $(command -v python3)"
echo ""
exec python3 "$ROOT/launch.py"
