# 💧 Water Reminder

An animated hydration buddy for **Windows, macOS and Linux**. Every 30 minutes, a water drop swims onto your screen holding a glass of water, and a wave rises along the bottom of the screen. Click **I drank a glass** to log it, or snooze it.

![preview](preview.gif)

## Features

- 🫧 An animated mascot that moves around your screen. Its eyes follow your mouse, and it pauses when you hover over it.
- 🤫 **It won't interrupt you.** It waits while a full-screen app, presentation or call is running, or Do Not Disturb is on, and appears once you're free.
- 🚶 **It pauses while you're away.** The countdown stops when you leave the computer. After a long break, it greets you with a "welcome back" reminder.
- 📊 **History & stats** with a chart of glasses per day, your current and best streaks, a 7-day average and 8 achievements to unlock
- 🥤 A daily goal tracker, a celebration each time you log a glass, and undo if you clicked by mistake
- ⚙️ A **Settings window** for the interval, goal, glass size, quiet hours, focus/away behaviour, name, sound and start-at-login
- 🧭 A system tray / menu bar icon with Remind me now, Log a glass, History & stats, Pause and Settings
- 🔄 **Easy updates.** It checks once a day, or you can run the one-line install command again. Every download is checked against its SHA-256 checksum.
- 🧘 **Smart pacing and reduce motion.** It reminds you a bit sooner when you're behind your goal and less often once you've reached it. A calmer mode drops the roaming and the wave.
- 📤 **Export your history** to CSV from the stats window.
- 🪶 Low CPU use: it only animates while the reminder is on screen, and lowers its frame rate while the mascot moves slowly

| Settings | History & stats |
|---|---|
| ![Settings](docs/settings.png) | ![Stats](docs/stats.png) |

## Install or update — one command

**Windows** — open **PowerShell** (Start menu → type *PowerShell*) and paste:
```powershell
irm https://raw.githubusercontent.com/mhd-mansoor/water-reminder/main/install.ps1 | iex
```

**macOS / Linux** — open **Terminal** and paste:
```bash
curl -fsSL https://raw.githubusercontent.com/mhd-mansoor/water-reminder/main/install.sh | bash
```

**Updating uses the same command.** It downloads the newest version, checks its SHA-256 checksum, installs it for your user only (no admin rights needed), turns on start-at-login and restarts the app. Your history and settings are kept. This also works if you installed an older version in any other way.

It also updates itself. Once a day it checks for a new version and shows you what's new, with **Update now**, **Later** and **Skip this version**. To check right away, choose *Check for updates…* in the tray menu or *Settings → Check for updates*.

| System | What you get |
|---|---|
| Windows 10/11 | The app, in `%LOCALAPPDATA%\WaterReminder` |
| macOS 12+ on Apple Silicon | `Water Reminder.app` in Applications |
| Linux x86-64 | The app, in `~/.local/share/water-reminder` |
| Intel Macs, ARM Linux | A Python-based install (it needs Python 3.9+) |

### Prefer to download a file?

Get it from the **[latest release](https://github.com/mhd-mansoor/water-reminder/releases/latest)**:

- **Windows:** download `WaterReminder-Windows.zip`, then **extract it** (right-click → *Extract All*) and open `WaterReminder.exe`. If Windows shows *"Windows protected your PC"*, click **More info → Run anyway**.
- **macOS:** open `WaterReminder-macOS.dmg` and drag the app to Applications. The first time, right-click it → **Open**.
- **Linux:** `chmod +x WaterReminder-Linux-x86_64 && ./WaterReminder-Linux-x86_64`

> These warnings appear because the app isn't code-signed (signing certificates cost money). The one-command install avoids them. The code is open, and every release is built on GitHub's servers from this repository.

## Using it

- **The water-drop icon** in the system tray (Windows/Linux) or menu bar (macOS) has Remind me now, Log a glass, Undo, History & stats, Pause for 1 hour, Settings and Quit. Double-click the icon to open stats.
- **In the reminder**, the ⚙ gear opens Settings, the × skips this reminder, and clicking the mascot makes it hop.
- **Opening the app again** while it's already running opens Settings.
- **Windows Start menu:** *Water Reminder*, *Water Reminder Settings* and *Water Reminder Stats*
- **Linux:** the same three entries are in your app menu. You can also use the terminal commands:

| Command | What it does |
|---|---|
| `water-reminder --now` | Show the reminder right now |
| `water-reminder --settings` | Open Settings |
| `water-reminder --stats` | Open History & stats |
| `water-reminder --status` | Print today's progress and your streak |
| `water-reminder --stop` | Stop until next login |

## Settings

| Setting | Default |
|---|---|
| Remind me every | 30 min |
| Snooze for / hide if ignored after | 10 min / 3 min |
| Glasses per day / glass size | 8 × 250 ml |
| Quiet hours | 22:00 – 08:00 |
| Don't interrupt full-screen apps, presentations, calls, Do Not Disturb | on |
| Pause while I'm away (no input for…) | on, 5 min |
| Adapt to my pace (smart pacing) | on |
| Your name, sound, reduce motion | login name, on, off |
| Start at login, check for updates | on, on |

Changes apply right away. They're saved in `config.json`:
`%APPDATA%\WaterReminder\` (Windows), `~/Library/Application Support/WaterReminder/` (macOS), `~/.config/water-reminder/` (Linux).

### How "busy" and "away" are detected

| | Busy (reminder waits) | Away (countdown pauses) |
|---|---|---|
| **Windows** | Full-screen app or game, presentation mode, microphone or camera in use | No keyboard/mouse input |
| **macOS** | A full-screen app is in front | No keyboard/mouse input |
| **Linux** | Do Not Disturb (GNOME), microphone in use (PulseAudio/PipeWire), full-screen window (X11) | No input (GNOME, or any X11 desktop) |

If something can't be detected on your system, reminders work normally.

## Uninstall

Open **Settings → Uninstall…**. On macOS, also drag the app from Applications to the Trash.
Or run `uninstall.bat` (Windows) or `bash uninstall.sh` (macOS/Linux) from this repository. Add `--purge` to also delete your history.

## For maintainers: publishing a release

The workflow in `.github/workflows/release.yml` builds all three apps on GitHub's machines whenever you push. To publish a version:

1. Raise `APP_VERSION` in `water_reminder.py`, for example to `1.4.1`.
2. Commit and push.
3. Tag the commit with the same number and push the tag:

   ```bash
   git tag v1.4.1
   git push origin v1.4.1
   ```

The tag must match `APP_VERSION`, or the release step stops.

GitHub then runs the tests on Windows, macOS and Linux, builds the three apps and starts each one once as a smoke test. If everything passes, it publishes the release after about 10 minutes, with the apps, `water_reminder.py` and `SHA256SUMS.txt`.

Everyone with version 1.3.0 or newer is offered the update within a day. Anyone can also update straight away with the one-line install command. The text you write in the release description is what users see under "What's new".

Run the tests locally with `python -m pytest -q tests`.

You can build locally with `python packaging/build.py`.

## How it works

It's a single Python file, `water_reminder.py`, built with [PySide6 (Qt)](https://doc.qt.io/qtforpython/). The mascot lives in a small transparent, always-on-top window that moves across the screen. The wave is a separate strip that lets clicks pass through it. Everything is drawn in code, including the icons.

Start-at-login uses each system's normal mechanism:

- **Windows:** a `Run` registry entry for your user
- **macOS:** a LaunchAgent
- **Linux:** an XDG autostart entry

The packaged apps are built with PyInstaller.

## License

MIT
