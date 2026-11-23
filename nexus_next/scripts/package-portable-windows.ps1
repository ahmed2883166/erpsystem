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
  $ExcludedBackendSegments = @(
    ".venv", "__pycache__", ".pytest_cache", ".secrets", ".cache",
    "data", "backups", "media", "logs", "staticfiles", "node_modules"
  )
  Get-ChildItem -LiteralPath $Backend -File -Recurse -Force | ForEach-Object {
    $Relative = $_.FullName.Substring($Backend.Length).TrimStart([char[]]@('\', '/'))
    $Segments = $Relative -split '[\\/]'
    if (($Segments | Where-Object { $ExcludedBackendSegments -contains $_ }).Count -gt 0 -or
        $_.Name -eq ".env" -or
        ($_.Name -like ".env.*" -and $_.Name -ne ".env.example") -or
        $_.Extension -in @(".sqlite", ".sqlite3", ".db", ".dump", ".pyc", ".pyo")) {
      return
    }
    $Destination = Join-Path $BackendTarget $Relative
    $Parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $Parent | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $Destination -Force
  }
  $FrontendTarget = Join-Path $Stage "frontend"
  $ScriptsTarget = Join-Path $Stage "scripts"
  New-Item -ItemType Directory -Force -Path $FrontendTarget, $ScriptsTarget | Out-Null
  Copy-Item -LiteralPath (Join-Path $Frontend "dist") -Destination $FrontendTarget -Recurse -Force
  Get-ChildItem -LiteralPath $PSScriptRoot -File -Force | Copy-Item -Destination $ScriptsTarget -Force
  Copy-Item -LiteralPath (Join-Path $Root "backend\.env.example") -Destination (Join-Path $BackendTarget ".env.example") -Force
  Copy-Item -LiteralPath (Join-Path $Root "initialize-admrail.cmd") -Destination (Join-Path $Stage "initialize-admrail.cmd") -Force
  Copy-Item -LiteralPath (Join-Path $Root "run-admrail-lan.ps1") -Destination (Join-Path $Stage "run-admrail-lan.ps1") -Force
  Copy-Item -LiteralPath (Join-Path $Root "run-admrail-lan.cmd") -Destination (Join-Path $Stage "run-admrail-lan.cmd") -Force
  $CaddyCommand = Get-Command "caddy.exe" -ErrorAction SilentlyContinue
  if (-not $CaddyCommand) { $CaddyCommand = Get-Command "caddy" -ErrorAction SilentlyContinue }
  if ($CaddyCommand) {
    $ToolsTarget = Join-Path $Stage "tools"
    New-Item -ItemType Directory -Force -Path $ToolsTarget | Out-Null
    Copy-Item -LiteralPath $CaddyCommand.Path -Destination (Join-Path $ToolsTarget "caddy.exe") -Force
  }

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
Admrail ERP v2 — Portable Windows Trial Runtime

Included: embedded Python 3.12, Django backend dependencies, and the prebuilt React
frontend. Python and Node.js do not need to be installed on the target Windows computer.

Required external service: PostgreSQL 16 for Windows. This is a trial runtime, not the
final guided installer. It does not create PostgreSQL roles/databases or open network access.

FIRST RUN
1. Install/start PostgreSQL 16.
2. Create a dedicated empty trial database and login. Example in psql as a PostgreSQL administrator
   (choose your own strong password):
     CREATE ROLE nexus_app LOGIN PASSWORD 'your-strong-password';
     CREATE DATABASE nexus_dev OWNER nexus_app ENCODING 'UTF8';
   Do not run these commands against an existing production database.
3. Copy backend\.env.example to backend\.env. Set a unique DJANGO_SECRET_KEY and the
   PostgreSQL values. Keep DJANGO_DEBUG=False and do not share backend\.env.
   Generate a secret with:
     runtime\python.exe -c "import secrets; print(secrets.token_urlsafe(64))"
4. Run initialize-admrail.cmd. It checks the project, applies migrations, creates the
   initial administrator if needed, and registers that user as the platform owner.
5. Run run-admrail.cmd and open http://127.0.0.1:8000.

OPTIONAL PRIVATE LAN MODE
1. Install Caddy once on the server, or copy caddy.exe to the package's tools\\caddy.exe.
2. Choose a stable internal DNS name such as erp.company.lan and map it to the server's reserved LAN IP.
3. In backend\\.env set DJANGO_ENV=production, ADMRAIL_LAN_HOST, add the host to
   DJANGO_ALLOWED_HOSTS, add https://<host> to CSRF_TRUSTED_ORIGINS, and enable
   SESSION_COOKIE_SECURE, CSRF_COOKIE_SECURE, SECURE_SSL_REDIRECT,
   DJANGO_HTTPS_TERMINATED_BY_PROXY. Set SECURE_HSTS_SECONDS=31536000.
4. Run run-admrail-lan.cmd from an elevated PowerShell/Command Prompt if you want it to
   configure the Private-profile firewall rule; otherwise open TCP 443 on Private manually.
   Waitress remains bound to 127.0.0.1:8000 and Caddy serves HTTPS on 443.
5. Copy only the public root.crt from %APPDATA%\\Caddy\\pki\\authorities\\local\\root.crt
   to each client and import it into Trusted Root Certification Authorities. Never share root.key.
6. Open https://<host> from client PCs. Do not expose ports 8000 or 5432 to the LAN.

The LAN launcher checks production security settings, refuses pending migrations, and
uses Caddy's internal TLS CA. DNS/hosts records and client certificate trust are managed
by the company network administrator. It does not enable internet access.

The initializer never creates or deletes a database and never deletes existing records.
Use a new empty trial database. For upgrades, create and verify a PostgreSQL backup first.
Do not use this package against the legacy Flask/SQLite database.

Default local mode binds to 127.0.0.1 only. Optional private LAN mode is available through
run-admrail-lan.cmd after configuring the production HTTPS settings in backend\\.env and
placing Caddy in tools\\caddy.exe (or PATH). It uses Caddy internal TLS and keeps Waitress
bound to localhost. DNS mapping and trusting the public root.crt on each client are required.
Never expose ports 8000 or 5432 to employee devices.
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

  # Django's embedded-runtime check imports application modules and creates bytecode caches.
  # Remove those generated caches from the distributable after validation, then enforce the scan below.
  Get-ChildItem -LiteralPath $Stage -Directory -Recurse -Force |
    Where-Object { $_.Name -in @("__pycache__", ".pytest_cache") } |
    Sort-Object { $_.FullName.Length } -Descending |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
  Get-ChildItem -LiteralPath $Stage -File -Recurse -Force |
    Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
    Remove-Item -Force

  foreach ($Name in @("runtime/python.exe", "backend/manage.py", "frontend/dist/index.html", "initialize-admrail.cmd", "run-admrail.cmd", "run-admrail-lan.cmd", "run-admrail-lan.ps1", "PORTABLE-README.txt")) {
    if (-not (Test-Path (Join-Path $Stage $Name))) { throw "Portable package is missing required item: $Name" }
  }

  $ForbiddenPackageFiles = @(Get-ChildItem -LiteralPath $Stage -File -Recurse -Force | Where-Object {
    $_.Extension -in @(".pyc", ".pyo", ".sqlite", ".sqlite3", ".db", ".dump") -or
    $_.Name -eq ".env" -or
    ($_.Name -like ".env.*" -and $_.Name -ne ".env.example") -or
    (($_.FullName.Substring($Stage.Length) -split "[\\\\/]") | Where-Object {
      $_ -in @("__pycache__", ".pytest_cache", ".secrets", ".cache", "data", "backups", "media", "logs")
    }).Count -gt 0
  })
  if ($ForbiddenPackageFiles.Count -gt 0) {
    throw "Portable package contains forbidden local/cache files: $($ForbiddenPackageFiles[0].FullName)"
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