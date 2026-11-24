@echo off
setlocal
title Admrail Owner Toolkit
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Admrail-Owner-Toolkit.ps1"
if errorlevel 1 (
  echo.
  echo Owner Toolkit stopped. Review the message above.
  pause
)
