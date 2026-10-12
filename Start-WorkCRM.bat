@echo off
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 (
  echo Python is not installed.
  echo Install Python 3.11 or newer from https://www.python.org/downloads/
  echo Check "Add python.exe to PATH", then try this file again.
  pause
  exit /b 1
)
echo WorkCRM is starting. Leave this window open.
echo Your browser should open http://127.0.0.1:8765
echo Close this window to stop.
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8765"
py -3 crm.py
pause
