@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
set "PYTHON=%ROOT%runtime\python.exe"
set "ENV_FILE=%ROOT%backend\.env"
set "ENV_EXAMPLE=%ROOT%backend\.env.example"

if not exist "%PYTHON%" (
  echo Embedded Python runtime is missing.
  pause
  exit /b 2
)
if not exist "%ENV_FILE%" (
  copy "%ENV_EXAMPLE%" "%ENV_FILE%" >nul
  echo Created backend\.env from the example.
  echo Edit backend\.env now: set a unique DJANGO_SECRET_KEY and your PostgreSQL credentials.
  echo Replace the sample password. Do not share backend\.env.
  pause
  exit /b 2
)
findstr /x /c:"DJANGO_SECRET_KEY=replace-this-for-local-development" "%ENV_FILE%" >nul
if not errorlevel 1 (
  echo Change the placeholder DJANGO_SECRET_KEY in backend\.env before continuing.
  pause
  exit /b 2
)
findstr /x /c:"POSTGRES_PASSWORD=replace-with-a-strong-local-password" "%ENV_FILE%" >nul
if not errorlevel 1 (
  echo Change the sample PostgreSQL password in backend\.env before continuing.
  pause
  exit /b 2
)

set "DJANGO_SETTINGS_MODULE=config.settings"
cd /d "%ROOT%backend"
echo Checking Django configuration...
"%PYTHON%" manage.py check
if errorlevel 1 goto :failed

echo Applying additive database migrations...
"%PYTHON%" manage.py migrate --noinput
if errorlevel 1 goto :failed

set "OWNER_USERNAME="
set /p OWNER_USERNAME=Enter the username for the initial platform owner:
if "%OWNER_USERNAME%"=="" (
  echo A username is required.
  goto :failed
)

"%PYTHON%" manage.py shell -c "from django.contrib.auth import get_user_model; import sys; sys.exit(0 if get_user_model().objects.filter(username=r'%OWNER_USERNAME%').exists() else 1)"
if errorlevel 2 goto :failed
if errorlevel 1 (
  echo Create the initial administrator account now.
  "%PYTHON%" manage.py createsuperuser --username "%OWNER_USERNAME%"
  if errorlevel 1 goto :failed
)

"%PYTHON%" manage.py bootstrap_platform_owner --username "%OWNER_USERNAME%"
if errorlevel 1 goto :failed

echo.
echo Initial setup completed.
echo Portable LAN package: start Admrail-Control-Center.cmd to manage Admrail and owner tools.
echo Source checkout: use start-local.ps1 for localhost development only.
echo Do not expose the development server to the LAN.
pause
exit /b 0

:failed
echo.
echo Setup stopped because a command failed. Review the error above.
echo No existing database or records were deleted by this script.
pause
exit /b 1
