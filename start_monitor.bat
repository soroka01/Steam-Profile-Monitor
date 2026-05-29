@echo off
REM Start Steam Profile Monitor Bot on Windows

cd /d "%~dp0"

if not exist ".venv" (
    echo Virtual environment not found!
    echo Run: python -m venv .venv
    pause
    exit /b 1
)

if not exist "config.ini" (
    echo Configuration file not found!
    echo Copy config.ini.example to config.ini and configure it
    pause
    exit /b 1
)

call .venv\Scripts\activate
python SteamProfileMonitor.py
pause
