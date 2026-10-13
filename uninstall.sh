#!/usr/bin/env bash
# Removes Water Reminder (Linux / macOS). Add --purge to also delete your history and settings.
if [ "$(uname -s)" = "Darwin" ]; then
  BASE="$HOME/Library/Application Support/WaterReminder"
else
  BASE="${XDG_DATA_HOME:-$HOME/.local/share}/water-reminder"
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -x "$BASE/venv/bin/python" ]; then
  "$BASE/venv/bin/python" "$HERE/water_reminder.py" --uninstall "$@"
fi
rm -rf "$BASE/venv" "$BASE/app"
echo "Done."
