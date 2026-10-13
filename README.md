# 💧 Water Reminder

An animated hydration buddy for **Windows, macOS and Linux**. Every 30 minutes, a water drop swims onto your screen holding a glass of water, and a wave rises along the bottom of the screen. Click **I drank a glass** to log it, or snooze it for 10 minutes.

![preview](preview.gif)

## Features

- 🫧 An animated mascot that moves around your screen. Its eyes follow your mouse, and it pauses when you hover over it.
- 🥤 A daily goal tracker (8 glasses by default), with a celebration each time you log a glass
- 🔔 Greets you by name, shows hydration tips and plays a small "bloop" sound
- 🌙 Quiet hours (22:00 to 08:00), snooze, and automatic hiding if you ignore it
- 🖱️ Only the mascot and its speech bubble catch clicks. The rest of your screen keeps working normally.
- 🧭 A system tray / menu bar icon with **Remind me now**, **Log a glass**, **Pause for 1 hour** and **Settings**
- 🚀 Starts automatically when you log in

## Install

First download the project, either with **Code → Download ZIP** (then unzip it) or with:

```bash
git clone https://github.com/mhd-mansoor/water-reminder.git
```

### Windows
Double-click **`install.bat`**.
If Python isn't installed, the installer tries to install it with winget. If that doesn't work, install Python from [python.org](https://www.python.org/downloads/) (tick **"Add python.exe to PATH"**) and run `install.bat` again.

### macOS
Open **Terminal** and run:
```bash
bash ~/Downloads/water-reminder/install.sh
```
Adjust the path to wherever you put the folder. You need Python 3.9 or newer. If you don't have it, install it from [python.org](https://www.python.org/downloads/) or run `xcode-select --install`.

### Linux
```bash
bash water-reminder/install.sh
```
On Ubuntu/Debian, Fedora and Arch, the installer adds any missing system packages, which may ask for your password.

The installer sets up a private Python environment with PySide6 (Qt), turns on start-at-login and shows the reminder once so you can see it.

## Usage

- Use the **water-drop icon** in the system tray (Windows/Linux) or menu bar (macOS) for Remind me now, Log a glass, Pause and Settings.
- On Windows, you can also open **Start → Water Reminder** to show it right away.
- On Linux, use the terminal commands:

| Command | What it does |
|---|---|
| `water-reminder --now` | Show the reminder right now |
| `water-reminder --status` | Today's progress and the last few days |
| `water-reminder --stop` | Stop until next login |

## Settings

Choose **Settings…** from the tray menu, or edit `config.json` directly:

| OS | Location |
|---|---|
| Windows | `%APPDATA%\WaterReminder\config.json` |
| macOS | `~/Library/Application Support/WaterReminder/config.json` |
| Linux | `~/.config/water-reminder/config.json` |

| Key | Default | Meaning |
|---|---|---|
| `interval_minutes` | 30 | How often it appears |
| `snooze_minutes` | 10 | Snooze length |
| `daily_goal_glasses` | 8 | Daily target |
| `glass_ml` | 250 | Size of one glass |
| `quiet_hours` | 22:00–08:00 | No reminders in this window (`"enabled": false` to turn off) |
| `auto_hide_minutes` | 3 | Leave on its own if ignored |
| `sound` | true | Play a sound |
| `name` | your login name | Name shown in the bubble |

Changes apply from the next reminder.

## Uninstall

- **Windows:** double-click `uninstall.bat`
- **macOS / Linux:** `bash uninstall.sh`

Add `--purge` to also delete your history and settings.

## How it works

It's a single Python file, `water_reminder.py`, built with [PySide6 (Qt)](https://doc.qt.io/qtforpython/). The mascot lives in a small transparent, always-on-top window that moves across the screen. The wave is a separate strip at the bottom of the screen that lets clicks pass through it. Everything is drawn in code, so there are no image files.

Start-at-login uses each system's normal mechanism:

- **Windows:** a `Run` registry entry for your user
- **macOS:** a LaunchAgent
- **Linux:** an XDG autostart entry

## License

MIT
