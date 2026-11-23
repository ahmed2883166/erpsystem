param(
  [switch]$ConfigureFirewall
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"
$EnvFile = Join-Path $Backend ".env"
$Python = Join-Path $Root "runtime\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { throw "Embedded Python runtime is missing." }
if (-not (Test-Path -LiteralPath $EnvFile)) { throw "Create backend\.env from backend\.env.example and configure PostgreSQL first." }

$Settings = @{}
foreach ($Line in Get-Content -LiteralPath $EnvFile) {
  if ($Line -match "^\s*([A-Z0-9_]+)\s*=\s*(.*)$") {
    $Settings[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
  }
}
foreach ($Key in @("DJANGO_SECRET_KEY", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "ADMRAIL_LAN_HOST")) {
  if (-not $Settings[$Key] -or $Settings[$Key] -match "^replace-") { throw "Set a real value for $Key in backend\.env." }
}
if ($Settings["DJANGO_ENV"] -ne "production" -or $Settings["DJANGO_DEBUG"] -notin @("False", "false", "0")) {
  throw "Set DJANGO_ENV=production and DJANGO_DEBUG=False."
}
if (-not $Settings["DJANGO_ALLOWED_HOSTS"] -or -not $Settings["CSRF_TRUSTED_ORIGINS"]) {
  throw "DJANGO_ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS must be configured for LAN."
}
$WebThreads = 32
if ($Settings["ADMRAIL_WEB_THREADS"]) {
  $ParsedThreads = 0
  if (-not [int]::TryParse($Settings["ADMRAIL_WEB_THREADS"], [ref]$ParsedThreads) -or $ParsedThreads -lt 8 -or $ParsedThreads -gt 64) {
    throw "ADMRAIL_WEB_THREADS must be an integer from 8 to 64."
  }
  $WebThreads = $ParsedThreads
}
$LanHost = $Settings["ADMRAIL_LAN_HOST"]
if ($LanHost -notmatch "^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$") { throw "ADMRAIL_LAN_HOST must be a stable internal DNS name." }
$AllowedHosts = @($Settings["DJANGO_ALLOWED_HOSTS"].Split(",") | ForEach-Object { $_.Trim() })
if ($AllowedHosts -notcontains $LanHost) { throw "Add $LanHost to DJANGO_ALLOWED_HOSTS." }
$TrustedOrigins = @($Settings["CSRF_TRUSTED_ORIGINS"].Split(",") | ForEach-Object { $_.Trim().TrimEnd("/") })
if ($TrustedOrigins -notcontains "https://$LanHost") { throw "Add https://$LanHost to CSRF_TRUSTED_ORIGINS." }
foreach ($Key in @("SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_SSL_REDIRECT", "DJANGO_HTTPS_TERMINATED_BY_PROXY", "SECURE_HSTS_INCLUDE_SUBDOMAINS")) {
  if ($Settings[$Key] -notin @("True", "true", "1", "yes", "on")) { throw "$Key must be enabled for LAN production." }
}
$Hsts = 0
if (-not [int]::TryParse($Settings["SECURE_HSTS_SECONDS"], [ref]$Hsts) -or $Hsts -lt 31536000) {
  throw "SECURE_HSTS_SECONDS must be at least 31536000."
}

if ($ConfigureFirewall) {
  $IsAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if (-not $IsAdmin) { throw "Run PowerShell as Administrator when using -ConfigureFirewall." }
}

$Caddy = Join-Path $Root "tools\caddy.exe"
if (-not (Test-Path -LiteralPath $Caddy)) {
  $Command = Get-Command "caddy.exe" -ErrorAction SilentlyContinue
  if (-not $Command) { $Command = Get-Command "caddy" -ErrorAction SilentlyContinue }
  if ($Command) { $Caddy = $Command.Path }
  else { throw "Caddy is required for HTTPS LAN mode. Place caddy.exe in tools\caddy.exe or add it to PATH." }
}

$env:DJANGO_SETTINGS_MODULE = "config.settings"
$env:DJANGO_ENV = "production"
$env:DJANGO_DEBUG = "False"
$env:DJANGO_HTTPS_TERMINATED_BY_PROXY = "True"

Push-Location $Backend
try {
  & $Python manage.py check --deploy
  if ($LASTEXITCODE -ne 0) { throw "Django production checks failed." }
  & $Python manage.py migrate --check --noinput
  if ($LASTEXITCODE -ne 0) { throw "Pending migrations detected. Stop and use the backed-up upgrade procedure before starting LAN service." }
  & $Python manage.py collectstatic --noinput
  if ($LASTEXITCODE -ne 0) { throw "Django static asset collection failed." }
} finally { Pop-Location }

$LanDirectory = Join-Path $Root "lan"
New-Item -ItemType Directory -Force -Path $LanDirectory | Out-Null
$CaddyFile = Join-Path $LanDirectory "Caddyfile"
$CaddyConfig = @("https://$LanHost {", "    tls internal", "    encode zstd gzip", "    reverse_proxy 127.0.0.1:8000", "}") -join [Environment]::NewLine
Set-Content -LiteralPath $CaddyFile -Value $CaddyConfig -Encoding ASCII
$Listening = Get-NetTCPConnection -LocalPort 443 -State Listen -ErrorAction SilentlyContinue
if ($Listening) { throw "TCP 443 is already in use. Stop the existing service before starting Admrail LAN." }

$CaddyProcess = Start-Process -FilePath $Caddy -WorkingDirectory $LanDirectory -ArgumentList @("run", "--config", "`"$CaddyFile`"", "--adapter", "caddyfile") -PassThru -WindowStyle Minimized
try {
  Start-Sleep -Seconds 2
  if ($CaddyProcess.HasExited) { throw "Caddy failed to start. Check port 443 and the Caddy log." }

  if ($ConfigureFirewall) {
    New-NetFirewallRule -DisplayName "Admrail ERP HTTPS LAN" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 443 -Profile Private -ErrorAction SilentlyContinue | Out-Null
  } else {
    Write-Host "If needed, allow TCP 443 on the Windows Private profile only; do not open 8000 or 5432." -ForegroundColor Yellow
  }

  $RootCertificate = Join-Path $env:APPDATA "Caddy\pki\authorities\local\root.crt"
  Write-Host "Admrail ERP LAN URL: https://$LanHost" -ForegroundColor Green
  Write-Host "Waitress is private on 127.0.0.1:8000 with $WebThreads worker threads. Caddy provides HTTPS on TCP 443." -ForegroundColor Green
  Write-Host "Install this public root certificate on each client: $RootCertificate" -ForegroundColor Yellow
  Write-Host "Never distribute root.key. On client Windows, import root.crt into Trusted Root Certification Authorities." -ForegroundColor Yellow
  Push-Location $Backend
  try {
    & $Python -m waitress --listen=127.0.0.1:8000 "--threads=$WebThreads" config.wsgi:application
    if ($LASTEXITCODE -ne 0) { throw "Waitress stopped with an error." }
  } finally { Pop-Location }
} finally {
  if ($CaddyProcess -and -not $CaddyProcess.HasExited) {
    Stop-Process -Id $CaddyProcess.Id -Force -ErrorAction SilentlyContinue
    try { Wait-Process -Id $CaddyProcess.Id -Timeout 5 -ErrorAction SilentlyContinue } catch { }
  }
}
