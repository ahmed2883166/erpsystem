param(
    [Parameter(Mandatory = $true)]
    [string]$BackupFile,
    [string]$TargetDatabase = ("nexus_restore_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $BackupFile -PathType Leaf)) {
    throw "Backup file does not exist: $BackupFile"
}
$BackupFile = (Resolve-Path -LiteralPath $BackupFile).Path
if ($TargetDatabase -notmatch "^[a-zA-Z][a-zA-Z0-9_]{2,62}$") {
    throw "Target database name must be 3-63 letters, digits or underscores and start with a letter."
}

$Root = Split-Path -Parent $PSScriptRoot
$Compose = Join-Path $Root "compose.yaml"
$ContainerPath = "/tmp/nexus-restore-$([guid]::NewGuid().ToString('N')).dump"
$ContainerId = $null

Push-Location $Root
try {
    $ContainerId = (docker compose -f $Compose ps -q db | Select-Object -First 1).Trim()
    if (-not $ContainerId) { throw "PostgreSQL container is not running." }

    docker cp $BackupFile "${ContainerId}:$ContainerPath"
    if ($LASTEXITCODE -ne 0) { throw "Could not copy backup into PostgreSQL container." }

    docker compose -f $Compose exec -T db sh -lc "pg_restore --list '$ContainerPath' >/dev/null"
    if ($LASTEXITCODE -ne 0) { throw "Backup archive is invalid or unreadable." }

    docker compose -f $Compose exec -T db sh -lc "createdb -U `$POSTGRES_USER -T template0 '$TargetDatabase'"
    if ($LASTEXITCODE -ne 0) { throw "Target database could not be created. It may already exist." }

    docker compose -f $Compose exec -T db sh -lc "pg_restore --exit-on-error --no-owner -U `$POSTGRES_USER -d '$TargetDatabase' '$ContainerPath'"
    if ($LASTEXITCODE -ne 0) {
        throw "Restore failed. The incomplete database '$TargetDatabase' was left separate; the active database was not changed."
    }

    Write-Host "Restore completed into separate database '$TargetDatabase'." -ForegroundColor Green
    Write-Host "The active NEXUS database was not overwritten. Validate this database before any controlled switch."
} finally {
    if ($ContainerId) {
        docker compose -f $Compose exec -T db sh -lc "rm -f '$ContainerPath'" 2>$null | Out-Null
    }
    Pop-Location
}
