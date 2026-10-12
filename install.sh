#!/usr/bin/env bash
# Installs the animated Water Reminder for the current user (no root needed,
# except to install GTK Python bindings if they are missing).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/water-reminder/app"
BIN="$HOME/.local/bin/water-reminder"
AUTOSTART="$HOME/.config/autostart/water-reminder.desktop"
LAUNCHER="$HOME/.local/share/applications/water-reminder.desktop"
PY=/usr/bin/python3
[ -x "$PY" ] || PY="$(command -v python3)"

echo "💧 Installing Water Reminder..."

# 1. dependencies: GTK3 + cairo bindings for Python
if ! "$PY" -c "import gi, cairo; gi.require_version('Gtk','3.0'); gi.require_foreign('cairo'); from gi.repository import Gtk" 2>/dev/null; then
  echo "→ Python GTK bindings are missing."
  if command -v apt-get >/dev/null; then
    sudo apt-get install -y python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-3.0
  elif command -v dnf >/dev/null; then
    sudo dnf install -y python3-gobject python3-cairo gtk3
  elif command -v pacman >/dev/null; then
    sudo pacman -S --needed python-gobject python-cairo gtk3
  else
    echo "Please install PyGObject (GTK 3) and pycairo for $PY, then re-run." >&2
    exit 1
  fi
fi

# 2. files
mkdir -p "$APP_DIR" "$(dirname "$BIN")" "$(dirname "$AUTOSTART")" "$(dirname "$LAUNCHER")"
install -m 755 "$HERE/water_reminder.py" "$APP_DIR/water_reminder.py"
cat > "$BIN" <<EOF
#!/bin/sh
exec $PY "$APP_DIR/water_reminder.py" "\$@"
EOF
chmod 755 "$BIN"

# 3. start at login
cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=Water Reminder
Comment=Animated reminder to drink water
Exec=$BIN
Icon=weather-showers
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=20
NoDisplay=false
EOF

# 4. app-menu entry: "Water Reminder" shows the reminder right away
cat > "$LAUNCHER" <<EOF
[Desktop Entry]
Type=Application
Name=Water Reminder
Comment=Show the water reminder now
Exec=$BIN --now
Icon=weather-showers
Categories=Utility;
Keywords=water;drink;hydration;reminder;
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$(dirname "$LAUNCHER")" 2>/dev/null || true

# 5. (re)start the background service now
"$BIN" --stop >/dev/null 2>&1 || true
sleep 0.5
setsid nohup "$BIN" > "$HOME/.local/share/water-reminder/service.log" 2>&1 < /dev/null &
sleep 2
"$BIN" --now || true

echo
echo "✅ Done! A water drop will swim onto your screen every 30 minutes."
echo "   Show it now:   water-reminder --now     (or search 'Water Reminder' in the app menu)"
echo "   Progress:      water-reminder --status"
echo "   Settings:      ~/.config/water-reminder/config.json"
echo "   Uninstall:     bash \"$HERE/uninstall.sh\""
