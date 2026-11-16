@echo off
setlocal
cd /d "%~dp0"

REM Local developer-only license bypass. Do not ship this launcher to customers.
set "NEXUS_DEMO_MODE=1"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" app.py
) else (
    python app.py
)

if errorlevel 1 (
    echo.
    echo NEXUS ERP stopped because of a startup error. Review the message above.
    pause
)
endlocal
