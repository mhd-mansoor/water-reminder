@echo off
rem Water Reminder installer for Windows.  Double-click this file.
setlocal
set "HERE=%~dp0"
set "BASE=%LOCALAPPDATA%\WaterReminder"
set "VENV=%BASE%\venv"
echo.
echo  Installing Water Reminder...
echo.

rem 1. Find Python 3 (the "py" launcher, or python on PATH)
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo  Python 3 was not found. Trying to install it with winget...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PY (
  echo.
  echo  Please install Python 3 from https://www.python.org/downloads/
  echo  ^(tick "Add python.exe to PATH" during setup^), then run this file again.
  pause
  exit /b 1
)

rem 2. Private Python environment with PySide6 (Qt)
if not exist "%BASE%" mkdir "%BASE%"
if not exist "%VENV%\Scripts\python.exe" (
  echo  - Creating Python environment
  %PY% -m venv "%VENV%"
  if errorlevel 1 ( echo  Could not create the Python environment. & pause & exit /b 1 )
)
echo  - Installing PySide6 ^(Qt^) - first time takes a minute
"%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip
"%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade "PySide6-Essentials>=6.5"
if errorlevel 1 ( echo  Installing PySide6 failed. Check your internet connection. & pause & exit /b 1 )

rem 3. Start at login + launch
"%VENV%\Scripts\python.exe" "%HERE%water_reminder.py" --install
echo.
pause
