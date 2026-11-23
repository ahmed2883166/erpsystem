$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$EnvFile = Join-Path $Backend ".env"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = Join-Path $Root "runtime\python.exe"
}
$Tag = Get-Date -Format "yyyyMMdd_HHmmss"

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) { throw "backend/.env is missing." }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Backend virtual-environment Python is missing." }

# Read the destination from the protected Django configuration, not from script arguments.
Push-Location $Backend
try {
    $BackupRoot = (& $Python manage.py get_backup_configuration | Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $BackupRoot) { throw "Backup destination is not configured or backups are disabled." }
} finally { Pop-Location }

Push-Location $Backend
try {
    & $Python manage.py check_backup_encryption_key
    if ($LASTEXITCODE -ne 0) { throw "Initialize the offline backup encryption key before running backups." }
} finally { Pop-Location }

$Drive = [System.IO.Path]::GetPathRoot($BackupRoot)
if (-not $Drive -or $Drive.TrimEnd("\").ToUpperInvariant() -eq "C:") { throw "Backup destination must be on a drive other than C:." }
if (-not (Test-Path -LiteralPath $BackupRoot -PathType Container)) { throw "Configured backup folder is unavailable: $BackupRoot" }

# Remove only abandoned verification workspaces older than one day; a running
# backup's temporary directory is never touched.
Get-ChildItem -LiteralPath $BackupRoot -Directory -Filter ".nexus-verify-*" -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-1) } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$Values = @{}
foreach ($Line in Get-Content -LiteralPath $EnvFile) {
    if ($Line -match '^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*?)\s*$') {
        $Value = $Matches[2].Trim()
        if ($Value.Length -ge 2 -and (($Value.StartsWith('"') -and $Value.EndsWith('"')) -or ($Value.StartsWith("'") -and $Value.EndsWith("'")))) {
            $Value = $Value.Substring(1, $Value.Length - 2)
        }
        $Values[$Matches[1]] = $Value
    }
}
foreach ($Key in @("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")) {
    if (-not $Values.ContainsKey($Key) -or -not $Values[$Key]) { throw "Required PostgreSQL setting $Key is missing from backend/.env." }
}
$PgHost = if ($Values["POSTGRES_HOST"]) { $Values["POSTGRES_HOST"] } else { "127.0.0.1" }
$PgPort = if ($Values["POSTGRES_PORT"]) { $Values["POSTGRES_PORT"] } else { "5432" }

$PgDump = Get-Command "pg_dump.exe" -ErrorAction SilentlyContinue
$PgRestore = Get-Command "pg_restore.exe" -ErrorAction SilentlyContinue
if (-not $PgDump -or -not $PgRestore) {
    $PgRoot = Join-Path $env:ProgramFiles "PostgreSQL"
    $PgBin = if (Test-Path $PgRoot) { Get-ChildItem $PgRoot -Directory | Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName "bin" } | Where-Object { (Test-Path (Join-Path $_ "pg_dump.exe")) -and (Test-Path (Join-Path $_ "pg_restore.exe")) } | Select-Object -First 1 } else { $null }
    if (-not $PgBin) { throw "PostgreSQL pg_dump.exe and pg_restore.exe were not found in PATH or Program Files." }
    $PgDumpPath = Join-Path $PgBin "pg_dump.exe"
    $PgRestorePath = Join-Path $PgBin "pg_restore.exe"
} else {
    $PgDumpPath = $PgDump.Source
    $PgRestorePath = $PgRestore.Source
}

$DailyRoot = Join-Path $BackupRoot "daily"
$MonthlyRoot = Join-Path $BackupRoot "monthly"
New-Item -ItemType Directory -Force -Path $DailyRoot, $MonthlyRoot | Out-Null

# Upgrade old plaintext archives only after validating and encrypting each one.
$LegacyArchives = @()
$LegacyArchives += @(Get-ChildItem $DailyRoot -Filter "nexus-*.dump" -File -ErrorAction SilentlyContinue)
$LegacyArchives += @(Get-ChildItem $MonthlyRoot -Filter "nexus-*.dump" -File -ErrorAction SilentlyContinue)
foreach ($LegacyArchive in $LegacyArchives) {
    & $PgRestorePath --list $LegacyArchive.FullName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Legacy plaintext archive is invalid and was left untouched: $($LegacyArchive.FullName)" }
    $LegacyEncrypted = "$($LegacyArchive.FullName).nexusenc"
    Push-Location $Backend
    try {
        & $Python manage.py encrypt_backup_file --input $LegacyArchive.FullName --output $LegacyEncrypted --update-history --delete-source
        if ($LASTEXITCODE -ne 0) { throw "Could not encrypt a legacy archive; its plaintext source was retained." }
    } finally { Pop-Location }
}
$DailyFile = Join-Path $DailyRoot "nexus-$Tag.dump"
$EncryptedFile = "$DailyFile.nexusenc"
$env:PGPASSWORD = $Values["POSTGRES_PASSWORD"]
try {
    & $PgDumpPath --host=$PgHost --port=$PgPort --username=$Values["POSTGRES_USER"] --dbname=$Values["POSTGRES_DB"] --format=custom --no-password --file=$DailyFile
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $DailyFile) -or (Get-Item -LiteralPath $DailyFile).Length -lt 1024) {
        throw "pg_dump failed or produced an invalidly small archive."
    }
    & $PgRestorePath --list $DailyFile | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Backup archive verification failed." }

    Push-Location $Backend
    try {
        & $Python manage.py encrypt_backup_file --input $DailyFile --output $EncryptedFile --delete-source
        if ($LASTEXITCODE -ne 0) { throw "Backup encryption or authenticated verification failed." }
    } finally { Pop-Location }
    if (-not (Test-Path -LiteralPath $EncryptedFile) -or (Get-Item -LiteralPath $EncryptedFile).Length -lt 1024) {
        throw "Encrypted backup is missing or unexpectedly small."
    }

    if ((Get-Date).Day -eq 1) { Copy-Item -LiteralPath $EncryptedFile -Destination (Join-Path $MonthlyRoot "nexus-$Tag.dump.nexusenc") }
    $DailyKeep = 30
    $MonthlyKeep = 12
    Push-Location $Backend
    try {
        $Settings = & $Python manage.py shell -c "from core.models import BackupConfiguration; c=BackupConfiguration.objects.first(); print(f'{c.daily_retention_days},{c.monthly_retention_months}' if c else '30,12')"
        if ($LASTEXITCODE -eq 0 -and $Settings -match "^(\d+),(\d+)$") { $DailyKeep=[int]$Matches[1]; $MonthlyKeep=[int]$Matches[2] }
    } finally { Pop-Location }
    Get-ChildItem $DailyRoot -Filter "nexus-*.dump.nexusenc" -File | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$DailyKeep) } | Remove-Item -Force
    Get-ChildItem $MonthlyRoot -Filter "nexus-*.dump.nexusenc" -File | Where-Object { $_.LastWriteTime -lt (Get-Date).AddMonths(-$MonthlyKeep) } | Remove-Item -Force
    Push-Location $Backend
    try {
        & $Python manage.py record_backup_run --file $EncryptedFile
        if ($LASTEXITCODE -ne 0) { Write-Warning "The encrypted backup is valid, but its history record could not be saved." }
    } finally { Pop-Location }
    Write-Output "Encrypted backup verified: $EncryptedFile"
} finally {
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    # Never leave a newly generated plaintext PostgreSQL dump behind, including on failure.
    if (Test-Path -LiteralPath $DailyFile -PathType Leaf) {
        Remove-Item -LiteralPath $DailyFile -Force -ErrorAction Stop
    }
}
