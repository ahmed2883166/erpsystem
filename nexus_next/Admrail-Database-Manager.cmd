@echo off
setlocal
title Admrail ERP v2 - Database Manager
cd /d "%~dp0"
set "SCRIPT=%~dp0scripts\Admrail-Database-Manager.ps1"
if not exist "%SCRIPT%" (
  echo ERROR: scripts\Admrail-Database-Manager.ps1 was not found.
  pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
