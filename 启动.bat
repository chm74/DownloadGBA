@echo off
setlocal

cd /d "%~dp0"
title GBA Status Server - Port 8765

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found in PATH.
    echo Please install Python or add it to PATH, then try again.
    pause
    exit /b 1
)

echo Starting GBA status server...
echo Open http://127.0.0.1:8765/ in your browser.
echo Press Ctrl+C to stop the server.
echo.

python -u scripts\status_server.py --port 8765

echo.
echo The status server has stopped.
pause
