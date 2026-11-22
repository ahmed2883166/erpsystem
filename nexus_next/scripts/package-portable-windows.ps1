param(
  [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) "release")
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
$HostPython = (Get-Command python.exe -ErrorAction Stop).Source
$PythonVersion = (& $HostPython -c "import platform; print(platform.python_version())").Trim()
$PythonMajorMinor = (& $HostPython -c "import sys; print(f'{sys.version_info.major}{sys.version_info.minor}')").Trim()
if ($PythonMajorMinor -ne "312") { throw "Portable runtime builder requires Python 3.12; found $PythonVersion." }
if (-not (Test-Path (Join-Path $Frontend "dist\index.html"))) { throw "React production build is missing. Run npm run build first." }
if (-not (Test-Path (Join-Path $Backend "requirements.txt"))) { throw "Backend requirements.txt is missing." }

$Tag = Get-Date -Format "yyyyMMdd_HHmmss"
$PackageName = "Admrail-ERP-v2-Portable-Windows-$Tag"
$Stage = Join-Path ([System.IO.Path]::GetTempPath()) $PackageName
$Runtime = Join-Path $Stage "runtime"
$SitePackages = Join-Path $Runtime "Lib\site-packages"
$ZipPath = Join-Path $OutputDirectory "$PackageName.zip"
$EmbedUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$EmbedZip = Join-Path ([System.IO.Path]::GetTempPath()) "python-$PythonVersion-embed-amd64.zip"
$EnvKeys = @("DJANGO_SECRET_KEY", "DJANGO_DEBUG", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST", "POSTGRES_PORT")
$PreviousEnv = @{}
foreach ($Key in $EnvKeys) { $PreviousEnv[$Key] = [Environment]::GetEnvironmentVariable($Key, "Process") }

try {
  New-Item -ItemType Directory -Force -Path $Stage, $OutputDirectory | Out-Null
  Invoke-WebRequest -Uri $EmbedUrl -OutFile $EmbedZip
  Expand-Archive -LiteralPath $EmbedZip -DestinationPath $Runtime -Force
  New-Item -ItemType Directory -Force -Path $SitePackages | Out-Null

  & $HostPython -m pip install --disable-pip-version-check --upgrade --target $SitePackages -r (Join-Path $Backend "requirements.txt")
  if ($LASTEXITCODE -ne 0) { throw "Installing application dependencies into the embedded runtime failed." }

  $PthFile = Join-Path $Runtime "python$PythonMajorMinor._pth"
  if (-not (Test-Path $PthFile)) { throw "Embedded Python path configuration was not found: $PthFile" }
  @("python$PythonMajorMinor.zip", ".", "..\backend", "Lib\site-packages", "import site") | Set-Content -LiteralPath $PthFile -Encoding ASCII

  $BackendTarget = Join-Path $Stage "backend"
  New-Item -ItemType Directory -Force -Path $BackendTarget | Out-Null
  Get-ChildItem -LiteralPath $Backend -Force | ForEach-Object {
    if ($_.Name -in @(".venv", "__pycache__", ".pytest_cache", "data", "backups", "staticfiles") -or
        $_.Name -eq ".env" -or
        ($_.Name -like ".env.*" -and $_.Name -ne ".env.example") -or
        $_.Extension -in @(".sqlite", ".sqlite3", ".db", ".dump", ".pyc", ".pyo")) { return }
    Copy-Item -LiteralPath $_.FullName -Destination $BackendTarget -Recurse -Force
  }
  $FrontendTarget = Join-Path $Stage "frontend"
  $ScriptsTarget = Join-Path $Stage "scripts"
  New-Item -ItemType Directory -Force -Path $FrontendTarget, $ScriptsTarget | Out-Null
  Copy-Item -LiteralPath (Join-Path $Frontend "dist") -Destination $FrontendTarget -Recurse -Force
  Get-ChildItem -LiteralPath $PSScriptRoot -File -Force | Copy-Item -Destination $ScriptsTarget -Force
  Copy-Item -LiteralPath (Join-Path $Root "backend\.env.example") -Destination (Join-Path $BackendTarget ".env.example") -Force

  @'
@echo off
setlocal
set "ROOT=%~dp0"
set "DJANGO_SETTINGS_MODULE=config.settings"
if not exist "%ROOT%backend\.env" (
  echo Configure the database connection and secret key before first use.
  echo Copy backend\.env.example to backend\.env and configure it securely.
  pause
  exit /b 2
)
cd /d "%ROOT%backend"
"%ROOT%runtime\python.exe" -m waitress --listen=127.0.0.1:8000 config.wsgi:application
'@ | Set-Content -LiteralPath (Join-Path $Stage "run-admrail.cmd") -Encoding ASCII

  @'
Admrail ERP v2 — Portable Windows Runtime

This package contains an embedded Python 3.12 runtime, the Django backend dependencies,
and the prebuilt React frontend. Python and Node.js do not need to be installed on the
target Windows computer.

PostgreSQL 16 for Windows remains a required external database service. This portable
runtime is a packaging milestone, not the final guided installer. It does not create a
PostgreSQL role/database, write secrets, migrate a live database, or open network access.

Before first use, configure backend\.env from backend\.env.example using a secure
administrative process. Start the local service with run-admrail.cmd. The application
binds to 127.0.0.1:8000 only.

For upgrades, create and verify a PostgreSQL backup before applying migrations.
Do not use this package against the legacy Flask/SQLite database.
'@ | Set-Content -LiteralPath (Join-Path $Stage "PORTABLE-README.txt") -Encoding UTF8

  $env:DJANGO_SECRET_KEY = "portable-build-check-only-secret"
  $env:DJANGO_DEBUG = "False"
  $env:POSTGRES_DB = "nexus_build_check"
  $env:POSTGRES_USER = "nexus_build_check"
  $env:POSTGRES_PASSWORD = "build-check-only"
  $env:POSTGRES_HOST = "127.0.0.1"
  $env:POSTGRES_PORT = "5432"
  Push-Location $BackendTarget
  try {
    & (Join-Path $Runtime "python.exe") -c "import django, psycopg, waitress; print('embedded imports OK', django.get_version())"
    if ($LASTEXITCODE -ne 0) { throw "Embedded runtime import verification failed." }
    & (Join-Path $Runtime "python.exe") manage.py check
    if ($LASTEXITCODE -ne 0) { throw "Django system check failed inside the embedded runtime." }
  } finally { Pop-Location }
  foreach ($Name in @("runtime/python.exe", "backend/manage.py", "frontend/dist/index.html", "run-admrail.cmd", "PORTABLE-README.txt")) {
    if (-not (Test-Path (Join-Path $Stage $Name))) { throw "Portable package is missing required item: $Name" }
  }

  if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
  Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $ZipPath -CompressionLevel Optimal
  $Hash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash
  Set-Content -LiteralPath "$ZipPath.sha256" -Value "$Hash  $(Split-Path -Leaf $ZipPath)" -Encoding ASCII
  Write-Host "Portable Windows package created: $ZipPath" -ForegroundColor Green
  Write-Host "SHA256: $Hash"
}
finally {
  foreach ($Key in $EnvKeys) {
    if ($null -eq $PreviousEnv[$Key]) { Remove-Item "Env:\$Key" -ErrorAction SilentlyContinue }
    else { Set-Item "Env:\$Key" -Value $PreviousEnv[$Key] }
  }
  if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
  if (Test-Path -LiteralPath $EmbedZip) { Remove-Item -LiteralPath $EmbedZip -Force }
}