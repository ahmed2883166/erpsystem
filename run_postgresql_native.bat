@echo off
setlocal

if not defined NEXUS_POSTGRES_DSN (
    echo ERROR: Set NEXUS_POSTGRES_DSN to the dedicated PostgreSQL database first.
    echo Example: set NEXUS_POSTGRES_DSN=host=127.0.0.1 port=5432 dbname=nexus_erp user=nexus_app password=CHANGE_ME
    pause
    exit /b 1
)

set "NEXUS_DEPLOYMENT_PROFILE=postgresql_native"
set "NEXUS_DB_BACKEND=postgresql"
set "NEXUS_POSTGRES_RUNTIME_ENABLE=1"
set "HOST=127.0.0.1"
set "PORT=5056"

if not defined LOCALAPPDATA (
    echo ERROR: LOCALAPPDATA is not available. Set NEXUS_DATA_DIR and NEXUS_BACKUP_DIR manually.
    pause
    exit /b 1
)

if not defined NEXUS_DATA_DIR set "NEXUS_DATA_DIR=%LOCALAPPDATA%\NEXUS ERP PostgreSQL\data"
if not defined NEXUS_BACKUP_DIR set "NEXUS_BACKUP_DIR=%LOCALAPPDATA%\NEXUS ERP PostgreSQL\Backups"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
)

%PYTHON_EXE% -c "import psycopg, psycopg_pool" >nul 2>&1
if errorlevel 1 (
    echo ERROR: PostgreSQL Python driver is missing.
    echo Run setup_windows.bat or install requirements.txt in the project environment.
    pause
    exit /b 1
)

echo Starting NEXUS ERP with PostgreSQL-native profile.
echo Local-only address: http://127.0.0.1:5056
echo Data directory: %NEXUS_DATA_DIR%
echo Backup directory: %NEXUS_BACKUP_DIR%
echo SQLite is not used as a fallback. Existing SQLite data is not modified.
%PYTHON_EXE% app.py

endlocal
