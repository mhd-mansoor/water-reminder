💧 WATER REMINDER — an animated hydration buddy for Linux
==========================================================

A cute water drop swims onto your screen every 30 minutes, holding a glass
of water, with a wave rising along the bottom of the screen. Click
"I drank a glass" to log it (it celebrates!), or snooze it for 10 minutes.
It greets you by your own name and tracks your daily goal (8 glasses).

INSTALL (takes ~10 seconds)
---------------------------
1. Unzip this folder anywhere (e.g. Downloads).
2. Open a terminal and run:

       bash ~/Downloads/water-reminder/install.sh

   (Adjust the path if you unzipped it somewhere else.)
   If GTK Python packages are missing, it will ask for your password once
   to install them.

That's it — it shows once right away, then every 30 minutes, and starts
automatically every time you log in.

USEFUL COMMANDS
---------------
  water-reminder --now       show the reminder right now
  water-reminder --status    today's progress + last few days
  water-reminder --stop      stop it until next login

You can also search "Water Reminder" in your app menu to show it instantly.

SETTINGS
--------
Edit ~/.config/water-reminder/config.json :
  interval_minutes    how often it appears          (default 30)
  snooze_minutes      snooze length                 (default 10)
  daily_goal_glasses  daily target                  (default 8)
  glass_ml            size of one glass             (default 250)
  quiet_hours         no reminders between these    (22:00 – 08:00)
  sound               true / false
  name                name shown in the bubble      (default: your login name)
Changes apply from the next reminder.

UNINSTALL
---------
       bash ~/Downloads/water-reminder/uninstall.sh

REQUIREMENTS
------------
Linux desktop (Ubuntu, Fedora, Mint, Arch…; GNOME/KDE/XFCE, X11 or Wayland),
Python 3 with GTK 3 bindings (installed automatically on Ubuntu/Debian,
Fedora and Arch).
