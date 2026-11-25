@echo off
setlocal
set "ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%configure-admrail-lan.ps1"
if errorlevel 1 (
  echo LAN configuration stopped. Review the error above.
  pause
  exit /b 1
)
