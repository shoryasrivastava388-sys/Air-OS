@echo off
title Air-OS Mobile Companion Wi-Fi Bridge
echo ========================================================
echo   AIR-OS COMPANION - MOBILE WI-FI BRIDGE
echo ========================================================
echo.
echo [*] Machine Local Wi-Fi IP: 192.168.31.18
echo [*] Open this URL in Safari (iPhone) or Chrome (Android):
echo.
echo     https://192.168.31.18:8000
echo.
echo [*] Tip: Tap "Add to Home Screen" on your phone to install
echo     as a native standalone mobile app!
echo ========================================================
echo.

cd /d "%~dp0"
"backend\.venv\Scripts\python.exe" -m uvicorn main:app --app-dir backend --host 0.0.0.0 --port 8000 --ssl-keyfile backend\key.pem --ssl-certfile backend\cert.pem
