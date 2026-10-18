@echo off
title Air-OS Desktop Cockpit Launcher
echo ========================================================
echo   AIR-OS AUTONOMOUS COCKPIT - LAUNCHING DESKTOP APP
echo ========================================================
echo.

cd /d "%~dp0"

echo [*] Starting Python Native Backend & Vision Engine...
start /b "" "backend\.venv\Scripts\python.exe" -m uvicorn main:app --app-dir backend --host 0.0.0.0 --port 8000 --ssl-keyfile backend\key.pem --ssl-certfile backend\cert.pem

echo [*] Starting Vite Frontend Service...
cd frontend
start /b "" npm run dev -- --host --port 5173
cd ..

echo [*] Waiting 3 seconds for services to initialize...
timeout /t 3 /nobreak >nul

echo [*] Launching Standalone Desktop Application Window...
start msedge.exe --app="http://localhost:5173" --window-size=1440,900 --user-data-dir="%LOCALAPPDATA%\AirOS_Desktop_App"

echo [✓] Air-OS Desktop App running!
