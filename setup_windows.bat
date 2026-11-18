@echo off
setlocal
cd /d "%~dp0"
title NEXUS ERP - Windows Setup

echo ==========================================
echo NEXUS ERP - Prepare a local Python environment
echo ==========================================
echo This setup installs Python packages only.
echo It does NOT create, migrate, reset, or modify company data.
echo.

where py >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo ERROR: Python 3.11 or newer was not found.
        echo Install Python 3.11+ and enable the Python launcher or PATH.
        pause
        exit /b 1
    )
    set "PY_CMD=python"
)

%PY_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.11 or newer is required.
    %PY_CMD% --version
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    %PY_CMD% -m venv .venv
    if errorlevel 1 goto :failed
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo.
echo Running read-only readiness check...
".venv\Scripts\python.exe" scripts\check_runtime_readiness.py
if errorlevel 1 goto :failed

echo.
echo Setup completed. No company database was created or modified.
echo Start the local development copy with run_local.bat.
pause
exit /b 0

:failed
echo.
echo ERROR: Setup did not complete. Company data was not intentionally changed.
pause
exit /b 1
