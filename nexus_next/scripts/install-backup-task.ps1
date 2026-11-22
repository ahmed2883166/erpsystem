$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = Join-Path $Root "runtime\python.exe"
}
$BackupScript = Join-Path $PSScriptRoot "backup-postgres.ps1"
if (-not (Test-Path $BackupScript)) { throw "Backup script not found: $BackupScript" }
if (-not (Test-Path $Python)) { throw "Backend virtual-environment Python is missing." }

$KeyFile = Join-Path $Backend ".secrets\backup-key.dpapi"
if (-not (Test-Path -LiteralPath $KeyFile -PathType Leaf)) { throw "Initialize encrypted backups first using scripts/initialize-backup-encryption.ps1." }
Push-Location $Backend
try {
    & $Python manage.py check_backup_encryption_key
    if ($LASTEXITCODE -ne 0) { throw "The local DPAPI backup key could not be opened by this Windows account." }
} finally { Pop-Location }
$CurrentIdentity = "$env:USERDOMAIN\$env:USERNAME"
$KeyDirectory = Split-Path -Parent $KeyFile
& icacls.exe $KeyDirectory /inheritance:r /grant:r "${CurrentIdentity}:(OI)(CI)(F)" "SYSTEM:(OI)(CI)(F)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict access to the backup key directory." }
& icacls.exe $KeyFile /inheritance:r /grant:r "${CurrentIdentity}:(F)" "SYSTEM:(F)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict access to the local backup encryption key." }

Push-Location $Backend
try {
    $config = & $Python manage.py shell -c "from core.models import BackupConfiguration; c=BackupConfiguration.objects.first(); print(f'{c.enabled}|{c.schedule}|{c.backup_path}' if c else 'False|daily|')"
    if ($LASTEXITCODE -ne 0) { throw "Could not read backup configuration." }
} finally { Pop-Location }
$parts = $config.Trim().Split("|")
if ($parts.Count -ne 3 -or $parts[0] -ne "True" -or -not $parts[2]) { throw "Configure and enable the backup destination from NEXUS as the system administrator first." }
$drive = [System.IO.Path]::GetPathRoot($parts[2])
if (-not $drive -or $drive.TrimEnd("\").ToUpperInvariant() -eq "C:") { throw "Backup destination must be outside C:." }
if (-not (Test-Path -LiteralPath $parts[2] -PathType Container)) { throw "Backup destination folder is unavailable." }
& icacls.exe $parts[2] /grant "SYSTEM:(OI)(CI)(M)" /T /C | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not grant the Windows backup service access to the destination folder." }

$TaskName = "NEXUS PostgreSQL Backup"
$PowerShell = (Get-Command powershell.exe).Source
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$BackupScript`""
switch ($parts[1]) {
    "daily" { $Trigger = New-ScheduledTaskTrigger -Daily -At "10:00PM" }
    "weekly" { $Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "10:00PM" }
    "monthly" { $Trigger = New-ScheduledTaskTrigger -Monthly -DaysOfMonth 1 -At "10:00PM" }
    default { throw "Unsupported backup schedule: $($parts[1])" }
}
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description "NEXUS encrypted PostgreSQL backup to the configured local drive; runs as SYSTEM." -Force | Out-Null
Write-Host "Scheduled '$TaskName' ($($parts[1])) at 10:00 PM." -ForegroundColor Green
Write-Host "The configured destination is: $($parts[2])"
Write-Host "The task runs as SYSTEM and can run while no user is logged in."
