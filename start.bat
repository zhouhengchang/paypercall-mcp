@echo off
REM Pay-Per-Call Gateway launcher (ASCII only: cmd parses GBK and would break on UTF-8)
setlocal
cd /d "%~dp0"
if exist "data\server.log" del /q "data\server.log"
echo Starting gateway on http://127.0.0.1:8300 ...
python app.py
if errorlevel 1 (
  echo.
  echo python not found in PATH. Fallback to managed interpreter:
  "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" app.py
  pause
)
endlocal
