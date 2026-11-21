$ErrorActionPreference = "Stop"
$BackupScript = Join-Path $PSScriptRoot "backup-postgres.ps1"
if (-not (Test-Path $BackupScript)) { throw "Backup script not found: $BackupScript" }

$TaskName = "NEXUS PostgreSQL Daily Backup"
$PowerShell = (Get-Command powershell.exe).Source
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$BackupScript`""
$Trigger = New-ScheduledTaskTrigger -Daily -At "10:00PM"
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description "Daily PostgreSQL backup for NEXUS ERP; monthly snapshots are copied automatically." -Force | Out-Null
Write-Host "Scheduled '$TaskName' daily at 10:00 PM for the current Windows user." -ForegroundColor Green
Write-Host "The user must be signed in and Docker Desktop/PostgreSQL must be running."
