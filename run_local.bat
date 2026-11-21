@echo off
setlocal
cd /d "%~dp0"

REM Initialize only the isolated preview database selected below.
set "NEXUS_SKIP_DB_INIT=0"

REM Preview-only isolated storage; never reuse the normal company data folder.
set "NEXUS_DATA_DIR=%~dp0test_data"
set "NEXUS_BACKUP_DIR=%USERPROFILE%\NEXUS ERP Test Backups"

title ADMRAIL ERP - Local Server
REM Local developer-only license bypass. Do not ship this launcher to customers.
set "NEXUS_DEMO_MODE=1"
REM Use a dedicated port so an older NEXUS process on port 5000 cannot show its license page.
set "PORT=5055"

echo Starting ADMRAIL ERP...
echo Please keep this window open while using the program.
echo The browser will open at http://127.0.0.1:5055
echo.

REM Open the browser after the Flask/Waitress server has had time to start.
start "" /min powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process 'http://127.0.0.1:5055'"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" app.py
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo ERROR: Python was not found. Install Python 3.11 or newer and try again.
        pause
        exit /b 1
    )
    python app.py
)

if errorlevel 1 (
    echo.
    echo ADMRAIL ERP could not start. Read the error message above.
    pause
)
endlocal
