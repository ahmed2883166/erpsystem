@echo off
setlocal
title Admrail ERP - Control Center
set "SCRIPT=%~dp0Admrail-Control-Center.ps1"
if not exist "%SCRIPT%" (
  echo ERROR: Admrail-Control-Center.ps1 was not found.
  pause
  exit /b 1
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
if errorlevel 1 (
  echo.
  echo Admrail Control Center stopped. Review the error above.
  pause
)
