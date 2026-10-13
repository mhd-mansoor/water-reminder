#!/usr/bin/env bash
# Water Reminder installer for Linux and macOS.
# Usage:  bash install.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
  BASE="$HOME/Library/Application Support/WaterReminder"
else
  BASE="${XDG_DATA_HOME:-$HOME/.local/share}/water-reminder"
fi
VENV="$BASE/venv"
PY="${PYTHON:-python3}"

echo "💧 Installing Water Reminder..."

# 1. Python 3.9+
if ! command -v "$PY" >/dev/null 2>&1; then
  if [ "$OS" = "Darwin" ]; then
    echo "Python 3 is needed. Install it from https://www.python.org/downloads/ (or run: xcode-select --install), then run this again."
  else
    echo "Python 3 is needed. Install it with your package manager (e.g. sudo apt install python3), then run this again."
  fi
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Python 3.9 or newer is required (found $("$PY" --version 2>&1))."
  exit 1
fi

# 2. Linux system libraries Qt needs
if [ "$OS" = "Linux" ]; then
  if command -v apt-get >/dev/null 2>&1; then
    NEED=""
    dpkg -s libxcb-cursor0 >/dev/null 2>&1 || NEED="$NEED libxcb-cursor0"
    "$PY" -c 'import ensurepip' >/dev/null 2>&1 || NEED="$NEED python3-venv"
    if [ -n "$NEED" ]; then
      echo "→ Installing system packages:$NEED (may ask for your password)"
      sudo apt-get install -y $NEED
    fi
  elif command -v dnf >/dev/null 2>&1; then
    rpm -q xcb-util-cursor >/dev/null 2>&1 || sudo dnf install -y xcb-util-cursor
  elif command -v pacman >/dev/null 2>&1; then
    pacman -Q xcb-util-cursor >/dev/null 2>&1 || sudo pacman -S --needed --noconfirm xcb-util-cursor
  fi
fi

# 3. Private Python environment with PySide6 (Qt)
mkdir -p "$BASE"
if [ ! -x "$VENV/bin/python" ]; then
  echo "→ Creating Python environment"
  "$PY" -m venv "$VENV"
fi
echo "→ Installing PySide6 (Qt) — first time takes a minute"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet --upgrade "PySide6-Essentials>=6.5"

# 4. Set up start-at-login and launch
"$VENV/bin/python" "$HERE/water_reminder.py" --install
