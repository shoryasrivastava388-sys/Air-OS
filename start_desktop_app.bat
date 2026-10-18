@echo off
setlocal enabledelayedexpansion
title Air-OS Desktop Cockpit Launcher
echo ========================================================
echo   AIR-OS AUTONOMOUS COCKPIT - LAUNCHING DESKTOP APP
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

:: 3. Ensure Frontend Dependencies exist
if not exist "frontend\node_modules" (
    echo [*] First-time setup: Installing frontend dependencies...
    cd frontend
    call npm install
    cd ..
)

:: 4. Start Python Native Backend and Vision Engine
echo [*] Starting Python Native Backend and Vision Engine...
start "Air-OS Vision Backend" /b "backend\.venv\Scripts\python.exe" -m uvicorn main:app --app-dir backend --host 0.0.0.0 --port 8000 --ssl-keyfile backend\key.pem --ssl-certfile backend\cert.pem

:: 5. Start Vite Frontend Service
echo [*] Starting Vite Frontend Service...
cd frontend
start "Air-OS Frontend" /b cmd /c npm run dev -- --host --port 5173
cd ..

echo [*] Waiting 3 seconds for services to initialize...
timeout /t 3 /nobreak >nul

:: 6. Launch Standalone Desktop Application Window (Edge or Chrome App Mode)
echo [*] Launching Standalone Desktop Application Window...
start msedge.exe --app="http://localhost:5173" --window-size=1440,900 --user-data-dir="%LOCALAPPDATA%\AirOS_Desktop_App" 2>nul
if errorlevel 1 (
    start chrome.exe --app="http://localhost:5173" --window-size=1440,900 --user-data-dir="%LOCALAPPDATA%\AirOS_Desktop_App" 2>nul
)

echo.
echo ========================================================
echo   [OK] Air-OS Desktop App is running!
echo ========================================================
