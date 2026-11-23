@echo off
setlocal
set "ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%run-admrail-lan.ps1" %*
if errorlevel 1 (
  echo Admrail LAN service stopped. Review the error above.
  pause
  exit /b 1
)
