#!/usr/bin/env bash
# Removes Water Reminder (macOS / Linux). Add --purge to also delete your history and settings.
if [ "$(uname -s)" = "Darwin" ]; then
  DATA="$HOME/Library/Application Support/WaterReminder"
  for app in "/Applications/Water Reminder.app" "$HOME/Applications/Water Reminder.app"; do
    if [ -x "$app/Contents/MacOS/WaterReminder" ]; then
      "$app/Contents/MacOS/WaterReminder" --uninstall "$@"; rm -rf "$app"; fi
  done
else
  DATA="${XDG_DATA_HOME:-$HOME/.local/share}/water-reminder"
  [ -x "$DATA/app/WaterReminder" ] && "$DATA/app/WaterReminder" --uninstall "$@"
fi
if [ -x "$DATA/venv/bin/python" ] && [ -f "$DATA/app/water_reminder.py" ]; then
  "$DATA/venv/bin/python" "$DATA/app/water_reminder.py" --uninstall "$@"
fi
rm -rf "$DATA/venv" "$DATA/app"
echo "Water Reminder removed."
