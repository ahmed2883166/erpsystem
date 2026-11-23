@echo off
setlocal
title Admrail - PostgreSQL Database Cleanup
cd /d "%~dp0"

set "SCRIPT=%~dp0Remove-Old-Admrail-Database.ps1"
if not exist "%SCRIPT%" set "SCRIPT=%~dp0scripts\Remove-Old-Admrail-Database.ps1"

if not exist "%SCRIPT%" (
    echo ERROR: Remove-Old-Admrail-Database.ps1 was not found.
    echo.
    echo Keep the CMD and PS1 files in the same folder,
    echo or place the PS1 file inside a scripts subfolder.
    echo.
    echo Download the PS1 file from the GitHub raw link and try again.
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
echo.
echo Cleanup utility has ended. Review any message above.
pause
