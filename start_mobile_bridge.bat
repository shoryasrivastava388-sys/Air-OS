@echo off
setlocal enabledelayedexpansion
title Air-OS Mobile Companion Wi-Fi Bridge
echo ========================================================
echo   AIR-OS COMPANION - MOBILE WI-FI BRIDGE
echo ========================================================
echo.

cd /d "%~dp0"

:: 1. Ensure Python Virtual Environment exists
if not exist "backend\.venv\Scripts\python.exe" (
    echo [*] First-time setup: Python environment not found.
    echo [*] Detecting system Python...
    
    set "PY_CMD="
    where py >nul 2>&1 && set "PY_CMD=py"
    if not defined PY_CMD (
        where python >nul 2>&1 && set "PY_CMD=python"
    )
    if not defined PY_CMD (
        if exist "%LOCALAPPDATA%\Python\bin\python.exe" set "PY_CMD=%LOCALAPPDATA%\Python\bin\python.exe"
    )
    if not defined PY_CMD (
        if exist "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe" set "PY_CMD=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
    )

    if not defined PY_CMD (
        echo [!] ERROR: Python was not found on your system!
        echo Please install Python 3.10+ from python.org and add it to PATH.
        pause
        exit /b 1
    )

    echo [*] Creating virtual environment in backend\.venv using !PY_CMD!...
    !PY_CMD! -m venv backend\.venv
    if errorlevel 1 (
        echo [!] Failed to create Python virtual environment.
        pause
        exit /b 1
    )

    echo [*] Installing backend dependencies from requirements.txt...
    backend\.venv\Scripts\pip.exe install -r backend\requirements.txt
    if errorlevel 1 (
        echo [!] Failed to install Python dependencies.
        pause
        exit /b 1
    )
)

:: 2. Ensure SSL Certificates exist for local HTTPS / WSS
if not exist "backend\key.pem" (
    echo [*] Generating local self-signed SSL certificates...
    backend\.venv\Scripts\python.exe backend\ensure_certs.py
)

:: 3. Detect Local Machine IP
for /f "tokens=*" %%i in ('backend\.venv\Scripts\python.exe -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8', 80)); print(s.getsockname()[0]); s.close()" 2^>nul') do set "LOCAL_IP=%%i"
if not defined LOCAL_IP set "LOCAL_IP=127.0.0.1"

echo ========================================================
echo   [*] Machine Local Wi-Fi IP: %LOCAL_IP%
echo   [*] Open this URL in Safari (iPhone) or Chrome (Android):
echo.
echo       https://%LOCAL_IP%:8000
echo.
echo   [*] Tip: Tap "Add to Home Screen" on your phone to install
echo       as a native standalone mobile app!
echo ========================================================
echo.

"backend\.venv\Scripts\python.exe" -m uvicorn main:app --app-dir backend --host 0.0.0.0 --port 8000 --ssl-keyfile backend\key.pem --ssl-certfile backend\cert.pem
