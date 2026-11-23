param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvFile = Join-Path $Root "backend\.env"

if (-not (Test-Path -LiteralPath $EnvFile)) {
  throw "backend\.env is missing. Run Setup-Admrail.cmd first."
}

Write-Host "Admrail private LAN configuration" -ForegroundColor Cyan
Write-Host "This configures HTTPS for your private company network. It does not open internet access." -ForegroundColor Yellow
$LanHost = (Read-Host "Enter the fixed internal DNS name (example: erp.company.lan)").Trim().ToLowerInvariant()
if ($LanHost -notmatch "^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$" -or $LanHost.Contains("..") -or $LanHost -notmatch "\.") {
  throw "Enter a valid internal DNS name, such as erp.company.lan."
}

$Content = [IO.File]::ReadAllText($EnvFile)
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Backup = "$EnvFile.before-lan-$Timestamp.bak"
[IO.File]::Copy($EnvFile, $Backup, $false)

function Get-EnvValue([string]$Text, [string]$Key) {
  $Match = [regex]::Match($Text, "(?m)^\s*" + [regex]::Escape($Key) + "\s*=\s*(.*?)\s*$")
  if ($Match.Success) { return $Match.Groups[1].Value.Trim().Trim('"').Trim("'") }
  return ""
}

function Set-EnvValue([string]$Text, [string]$Key, [string]$Value) {
  $Pattern = "(?m)^\s*" + [regex]::Escape($Key) + "\s*=.*(?:\r?\n|$)"
  $Line = "$Key=$Value" + [Environment]::NewLine
  if ([regex]::IsMatch($Text, $Pattern)) {
    return [regex]::Replace($Text, $Pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $Line }, 1)
  }
  return $Text.TrimEnd() + [Environment]::NewLine + $Line
}

$Allowed = @((Get-EnvValue $Content "DJANGO_ALLOWED_HOSTS") -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($Allowed -notcontains $LanHost) { $Allowed += $LanHost }
$Origins = @((Get-EnvValue $Content "CSRF_TRUSTED_ORIGINS") -split "," | ForEach-Object { $_.Trim().TrimEnd("/") } | Where-Object { $_ })
$Origin = "https://$LanHost"
if ($Origins -notcontains $Origin) { $Origins += $Origin }

$Values = [ordered]@{
  "DJANGO_ENV" = "production"
  "DJANGO_DEBUG" = "False"
  "ADMRAIL_LAN_HOST" = $LanHost
  "DJANGO_ALLOWED_HOSTS" = ($Allowed -join ",")
  "CSRF_TRUSTED_ORIGINS" = ($Origins -join ",")
  "SESSION_COOKIE_SECURE" = "True"
  "CSRF_COOKIE_SECURE" = "True"
  "SECURE_SSL_REDIRECT" = "True"
  "SECURE_HSTS_SECONDS" = "31536000"
  "SECURE_HSTS_INCLUDE_SUBDOMAINS" = "True"
  "DJANGO_HTTPS_TERMINATED_BY_PROXY" = "True"
}
foreach ($Entry in $Values.GetEnumerator()) {
  $Content = Set-EnvValue $Content $Entry.Key ([string]$Entry.Value)
}

[IO.File]::WriteAllText($EnvFile, $Content, (New-Object System.Text.UTF8Encoding($false)))

Write-Host ""
Write-Host "LAN HTTPS settings saved. Existing database credentials were not changed." -ForegroundColor Green
Write-Host "Backup: $Backup" -ForegroundColor Green
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "1. Reserve a fixed IP for this Windows server and create an internal DNS A-record:"
Write-Host "   $LanHost -> the server's reserved LAN IP"
Write-Host "2. Install Caddy, or place caddy.exe in this package's tools folder."
Write-Host "3. Run run-admrail-lan.cmd. Use -ConfigureFirewall from an elevated terminal if needed."
Write-Host "4. On each client PC, trust only the public root.crt shown by the LAN launcher."
Write-Host "5. Open https://$LanHost. Do not expose ports 8000 or 5432 to client PCs."
Read-Host "Press Enter to close"
