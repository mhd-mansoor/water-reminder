# 💧 Water Reminder

An animated hydration buddy for Linux. Every 30 minutes, a water drop swims onto your screen holding a glass of water, and a wave rises along the bottom of the screen. Click **I drank a glass** to log it, or snooze it for 10 minutes.

![preview](preview.gif)

## Features

- Animated mascot that moves around the screen. Its eyes follow your mouse, and it pauses when you hover over it.
- Daily goal tracker (8 glasses by default) and history with `--status`
- Greets you by name, shows hydration tips and plays a small "bloop" sound
- Quiet hours (22:00 to 08:00), snooze, and automatic hiding if ignored
- Clicks go through to your desktop everywhere except on the mascot and its bubble
- Starts automatically at login

## Install

```bash
git clone https://github.com/mhd-mansoor/water-reminder.git
bash water-reminder/install.sh
```

The installer adds the GTK Python bindings if they're missing (Ubuntu/Debian, Fedora, Arch).

## Usage

| Command | What it does |
|---|---|
| `water-reminder --now` | Show the reminder right now |
| `water-reminder --status` | Today's progress and the last few days |
| `water-reminder --stop` | Stop until next login |

You can also search **Water Reminder** in your app menu.

## Settings

Edit `~/.config/water-reminder/config.json`. Changes apply from the next reminder.

| Key | Default | Meaning |
|---|---|---|
| `interval_minutes` | 30 | How often it appears |
| `snooze_minutes` | 10 | Snooze length |
| `daily_goal_glasses` | 8 | Daily target |
| `glass_ml` | 250 | Size of one glass |
| `quiet_hours` | 22:00–08:00 | No reminders in this window |
| `sound` | true | Play a sound |
| `name` | your login name | Name shown in the bubble |

## Uninstall

```bash
bash water-reminder/uninstall.sh          # keep history
bash water-reminder/uninstall.sh --purge  # remove everything
```

## Requirements

A Linux desktop (GNOME, KDE, XFCE…, on X11 or Wayland) with Python 3 and GTK 3.
