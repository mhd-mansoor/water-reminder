@echo off
rem Removes Water Reminder. Run "uninstall.bat --purge" to also delete your history and settings.
if exist "%LOCALAPPDATA%\WaterReminder\app\WaterReminder.exe" (
  start /wait "" "%LOCALAPPDATA%\WaterReminder\app\WaterReminder.exe" --uninstall %*
) else if exist "%LOCALAPPDATA%\WaterReminder\venv\Scripts\python.exe" (
  "%LOCALAPPDATA%\WaterReminder\venv\Scripts\python.exe" "%LOCALAPPDATA%\WaterReminder\app\water_reminder.py" --uninstall %*
  rmdir /s /q "%LOCALAPPDATA%\WaterReminder\venv" 2>nul
)
echo Water Reminder removed.
pause
