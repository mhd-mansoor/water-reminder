#!/usr/bin/env bash
# Water Reminder — install or update (macOS and Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/mhd-mansoor/water-reminder/main/install.sh | bash
#
# Run the same command again any time to update to the latest version.
# It downloads the newest release from GitHub, checks its SHA-256 checksum,
# installs it for your user only (no admin rights), turns on start-at-login
# and starts it. Your history and settings are kept.
#
# Apple Silicon Macs and x86-64 Linux get the ready-made app. Other machines
# (Intel Macs, ARM Linux) — or WR_FROM_SOURCE=1 — get a Python-based install.
set -euo pipefail

REPO="mhd-mansoor/water-reminder"
BASE_URL="${WR_BASE_URL:-https://github.com/$REPO/releases/latest/download}"
OS="$(uname -s)"
ARCH="$(uname -m)"
if [ "$OS" = "Darwin" ]; then
  DATA="$HOME/Library/Application Support/WaterReminder"
else
  DATA="${XDG_DATA_HOME:-$HOME/.local/share}/water-reminder"
fi
HERE=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say()  { printf '\033[1;34m💧 %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

download() {  # download <asset> -> $TMP/<asset>, verified against SHA256SUMS.txt
  local asset="$1"
  [ -f "$TMP/SHA256SUMS.txt" ] || curl -fsSL -o "$TMP/SHA256SUMS.txt" "$BASE_URL/SHA256SUMS.txt" \
    || fail "Couldn't reach GitHub. Check your internet connection."
  say "Downloading ${asset}…"
  curl -fL --progress-bar -o "$TMP/$asset" "$BASE_URL/$asset" || fail "Download of $asset failed."
  local want got
  want="$(awk -v f="$asset" '$2 == f || $2 == "*"f {print $1}' "$TMP/SHA256SUMS.txt")"
  got="$(sha256 "$TMP/$asset")"
  [ -n "$want" ] && [ "$want" = "$got" ] || fail "Checksum check failed for $asset — not installing it."
}

cleanup_source_install() {  # switching from a Python install to the app: remove the old bits
  rm -rf "$DATA/venv" "$DATA/app/water_reminder.py" "$DATA/app/water_reminder.py.bak" 2>/dev/null || true
}

install_linux_app() {
  local asset="WaterReminder-Linux-x86_64"
  download "$asset"
  chmod +x "$TMP/$asset"
  # The app copies itself into ~/.local/share/water-reminder, sets up start-at-login and
  # menu entries, stops any older copy, then starts the installed copy.
  "$TMP/$asset" || fail "The app couldn't start. Run it from a terminal to see why."
  cleanup_source_install
}

install_mac_app() {
  local asset="WaterReminder-macOS.dmg" app="Water Reminder.app" dest mnt="$TMP/mnt" was=""
  download "$asset"
  if [ -w /Applications ]; then dest="/Applications"; else dest="$HOME/Applications"; mkdir -p "$dest"; fi
  [ -d "$dest/$app" ] && was="--after-update"
  # stop a running copy first
  for old in "/Applications/$app" "$HOME/Applications/$app"; do
    [ -x "$old/Contents/MacOS/WaterReminder" ] && "$old/Contents/MacOS/WaterReminder" --stop >/dev/null 2>&1 || true
  done
  mkdir -p "$mnt"
  hdiutil attach -nobrowse -readonly -noautoopen -quiet -mountpoint "$mnt" "$TMP/$asset" || fail "Couldn't open the .dmg."
  rm -rf "$dest/$app"
  ditto "$mnt/$app" "$dest/$app"
  hdiutil detach "$mnt" -quiet || true
  xattr -dr com.apple.quarantine "$dest/$app" 2>/dev/null || true
  for other in "/Applications/$app" "$HOME/Applications/$app"; do  # don't leave a second copy behind
    [ "$other" != "$dest/$app" ] && [ -d "$other" ] && rm -rf "$other" 2>/dev/null || true
  done
  cleanup_source_install
  say "Installed to $dest/$app"
  if [ -n "$was" ]; then open "$dest/$app" --args --after-update; else open "$dest/$app"; fi
}

install_from_source() {
  local py="${PYTHON:-python3}" src
  command -v "$py" >/dev/null 2>&1 || fail "Python 3.9+ is needed: https://www.python.org/downloads/"
  "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || fail "Python 3.9 or newer is needed."
  if [ -n "$HERE" ] && [ -f "$HERE/water_reminder.py" ]; then
    src="$HERE/water_reminder.py"
  else
    download "water_reminder.py"; src="$TMP/water_reminder.py"
  fi
  if [ "$OS" = "Linux" ]; then
    if command -v apt-get >/dev/null 2>&1; then
      local need=""
      dpkg -s libxcb-cursor0 >/dev/null 2>&1 || need="$need libxcb-cursor0"
      "$py" -c 'import ensurepip' >/dev/null 2>&1 || need="$need python3-venv"
      if [ -n "$need" ]; then say "Installing system packages:$need (may ask for your password)"; sudo apt-get install -y $need; fi
    elif command -v dnf >/dev/null 2>&1; then
      rpm -q xcb-util-cursor >/dev/null 2>&1 || sudo dnf install -y xcb-util-cursor
    elif command -v pacman >/dev/null 2>&1; then
      pacman -Q xcb-util-cursor >/dev/null 2>&1 || sudo pacman -S --needed --noconfirm xcb-util-cursor
    fi
  fi
  mkdir -p "$DATA"
  [ -x "$DATA/venv/bin/python" ] || { say "Creating Python environment…"; "$py" -m venv "$DATA/venv"; }
  say "Installing PySide6 (Qt) — the first time takes a minute…"
  "$DATA/venv/bin/python" -m pip install --quiet --upgrade pip
  "$DATA/venv/bin/python" -m pip install --quiet --upgrade "PySide6-Essentials>=6.5"
  "$DATA/venv/bin/python" "$src" --install
}

say "Water Reminder installer"
if [ "${WR_FROM_SOURCE:-0}" = "1" ]; then
  install_from_source
elif [ "$OS" = "Linux" ] && [ "$ARCH" = "x86_64" ]; then
  install_linux_app
elif [ "$OS" = "Darwin" ] && [ "$ARCH" = "arm64" ]; then
  install_mac_app
else
  say "No ready-made app for $OS/$ARCH — installing with Python instead."
  install_from_source
fi
say "Done! Run this same command any time to update."
