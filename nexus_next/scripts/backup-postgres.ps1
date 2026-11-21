$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Compose = Join-Path $Root "compose.yaml"
$BackupRoot = Join-Path $Root "backups"
$DailyRoot = Join-Path $BackupRoot "daily"
$MonthlyRoot = Join-Path $BackupRoot "monthly"
$Tag = Get-Date -Format "yyyyMMdd_HHmmss"
$ContainerPath = "/tmp/nexus-backup-$Tag.dump"
$DailyFile = Join-Path $DailyRoot "nexus-$Tag.dump"

New-Item -ItemType Directory -Force -Path $DailyRoot, $MonthlyRoot | Out-Null
Push-Location $Root
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
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $DailyFile) -or (Get-Item $DailyFile).Length -lt 1024) {
        throw "Backup file was not copied or is unexpectedly small."
    }

    if ((Get-Date).Day -eq 1) {
        Copy-Item $DailyFile (Join-Path $MonthlyRoot "nexus-$Tag.dump")
    }

    Get-ChildItem $DailyRoot -Filter "nexus-*.dump" -File |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
        Remove-Item -Force
    Get-ChildItem $MonthlyRoot -Filter "nexus-*.dump" -File |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddMonths(-12) } |
        Remove-Item -Force

    Write-Host "Backup verified: $DailyFile" -ForegroundColor Green
} finally {
    docker compose -f $Compose exec -T db sh -lc "rm -f '$ContainerPath'" 2>$null | Out-Null
    Pop-Location
}
