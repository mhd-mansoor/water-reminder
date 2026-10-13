@echo off
rem Removes Water Reminder. Run "uninstall.bat --purge" to also delete history and settings.
setlocal
set "BASE=%LOCALAPPDATA%\WaterReminder"
if exist "%BASE%\venv\Scripts\python.exe" "%BASE%\venv\Scripts\python.exe" "%~dp0water_reminder.py" --uninstall %*
timeout /t 2 /nobreak >nul
rmdir /s /q "%BASE%" 2>nul
echo Done.
pause
