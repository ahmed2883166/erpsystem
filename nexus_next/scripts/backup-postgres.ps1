$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Compose = Join-Path $Root "compose.yaml"
$Tag = Get-Date -Format "yyyyMMdd_HHmmss"
$ContainerPath = "/tmp/nexus-backup-$Tag.dump"

Push-Location $Backend
try {
    $BackupRoot = (python manage.py get_backup_configuration | Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $BackupRoot) { throw "Backup destination is not configured or backups are disabled." }
} finally { Pop-Location }

$Drive = [System.IO.Path]::GetPathRoot($BackupRoot)
if (-not $Drive -or $Drive.TrimEnd("\").ToUpperInvariant() -eq "C:") {
    throw "Backup destination must be on a drive other than C:."
}
if (-not (Test-Path -LiteralPath $BackupRoot -PathType Container)) {
    throw "Configured backup folder is unavailable: $BackupRoot"
}
if (-not (Test-Path -LiteralPath $BackupRoot -PathType Container)) { throw "Backup folder unavailable." }
$DailyRoot = Join-Path $BackupRoot "daily"
$MonthlyRoot = Join-Path $BackupRoot "monthly"
New-Item -ItemType Directory -Force -Path $DailyRoot, $MonthlyRoot | Out-Null
$DailyFile = Join-Path $DailyRoot "nexus-$Tag.dump"

Push-Location $Root
$ContainerId = $null
try {
    docker compose -f $Compose ps --status running db | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL container is not running." }
    $ContainerId = (docker compose -f $Compose ps -q db | Select-Object -First 1).Trim()
    if (-not $ContainerId) { throw "Could not locate the PostgreSQL container." }

    docker compose -f $Compose exec -T db sh -lc "pg_dump -U `$POSTGRES_USER -d `$POSTGRES_DB -Fc -f '$ContainerPath'"
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed." }
    docker compose -f $Compose exec -T db sh -lc "pg_restore --list '$ContainerPath' >/dev/null"
    if ($LASTEXITCODE -ne 0) { throw "Backup archive verification failed inside PostgreSQL container." }
    docker cp "${ContainerId}:$ContainerPath" $DailyFile
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $DailyFile) -or (Get-Item -LiteralPath $DailyFile).Length -lt 1024) {
        throw "Backup file was not copied or is unexpectedly small."
    }

    if ((Get-Date).Day -eq 1) { Copy-Item -LiteralPath $DailyFile -Destination (Join-Path $MonthlyRoot "nexus-$Tag.dump") }
    $dailyKeep = 30
    $monthlyKeep = 12
    Push-Location $Backend
    try {
        $settings = python manage.py shell -c "from core.models import BackupConfiguration; c=BackupConfiguration.objects.first(); print(f'{c.daily_retention_days},{c.monthly_retention_months}' if c else '30,12')"
        if ($LASTEXITCODE -eq 0 -and $settings -match "^(\d+),(\d+)$") { $dailyKeep=[int]$Matches[1]; $monthlyKeep=[int]$Matches[2] }
    } finally { Pop-Location }
    Get-ChildItem $DailyRoot -Filter "nexus-*.dump" -File | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$dailyKeep) } | Remove-Item -Force
    Get-ChildItem $MonthlyRoot -Filter "nexus-*.dump" -File | Where-Object { $_.LastWriteTime -lt (Get-Date).AddMonths(-$monthlyKeep) } | Remove-Item -Force
    Write-Output "Backup verified: $DailyFile"
} finally {
    if ($ContainerId) { docker compose -f $Compose exec -T db sh -lc "rm -f '$ContainerPath'" 2>$null | Out-Null }
    Pop-Location
}
