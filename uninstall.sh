#!/usr/bin/env bash
# Removes Water Reminder (keeps your history unless you pass --purge).
"$HOME/.local/bin/water-reminder" --stop 2>/dev/null || true
rm -f "$HOME/.local/bin/water-reminder" \
      "$HOME/.config/autostart/water-reminder.desktop" \
      "$HOME/.local/share/applications/water-reminder.desktop"
rm -rf "$HOME/.local/share/water-reminder/app"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$HOME/.local/share/water-reminder" "$HOME/.config/water-reminder"
fi
echo "Water Reminder removed."
