param(
    [Parameter(Mandatory = $true)]
    [string]$BackupFile,
    [string]$TargetDatabase = ("nexus_restore_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
)

$ErrorActionPreference = "Stop"

function Get-PgTool([string]$Name) {
    if ($Name -notmatch '^(pg_dump|pg_restore|psql|createdb)if (-not (Test-Path -LiteralPath $BackupFile -PathType Leaf)) { throw "Backup file does not exist: $BackupFile" }
$BackupFile = (Resolve-Path -LiteralPath $BackupFile).Path
if ($TargetDatabase -notmatch "^[a-zA-Z][a-zA-Z0-9_]{2,62}$") { throw "Target database name must be 3-63 letters, digits or underscores and start with a letter." }

$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { $Python = Join-Path $Root "runtime\python.exe" }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Backend Python runtime is missing." }
$EnvFile = Join-Path $Backend ".env"
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) { throw "backend/.env is missing." }
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
if ($TargetDatabase -eq $Values["POSTGRES_DB"]) { throw "Restore target must be different from the active database." }
$PgHost = if ($Values["POSTGRES_HOST"]) { $Values["POSTGRES_HOST"] } else { "127.0.0.1" }
$PgPort = if ($Values["POSTGRES_PORT"]) { $Values["POSTGRES_PORT"] } else { "5432" }

$PgRestorePath = Get-PgTool "pg_restore"
$CreatedbPath = Get-PgTool "createdb"
$PsqlPath = Get-PgTool "psql"

$DecryptedTemp = $null
$RestoreSource = $BackupFile
$RestoreTempRoot = Join-Path $env:TEMP "AdmrailRestore"
New-Item -ItemType Directory -Path $RestoreTempRoot -Force | Out-Null
$RestoreIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $RestoreTempRoot /inheritance:r /grant:r "$($RestoreIdentity):(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict access to the restore temporary directory." }
Get-ChildItem -LiteralPath $RestoreTempRoot -Filter "nexus-restore-*.dump" -File -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-1) } |
    Remove-Item -Force -ErrorAction SilentlyContinue
$AdminPassword = $null
$AdminPasswordPtr = [IntPtr]::Zero
try {
    $AdminSecure = Read-Host "Enter PostgreSQL administrator (postgres) password to create the restore database" -AsSecureString
    $AdminPasswordPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($AdminSecure)
    $AdminPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($AdminPasswordPtr)
    if ([string]::IsNullOrWhiteSpace($AdminPassword)) { throw "The PostgreSQL administrator password cannot be empty." }
    if ($BackupFile.EndsWith(".nexusenc", [System.StringComparison]::OrdinalIgnoreCase)) {
        $DecryptedTemp = Join-Path $RestoreTempRoot ("nexus-restore-" + [guid]::NewGuid().ToString("N") + ".dump")
        Push-Location $Backend
        try {
            & $Python manage.py decrypt_backup_file --input $BackupFile --output $DecryptedTemp
            if ($LASTEXITCODE -ne 0) { throw "Encrypted backup could not be authenticated and decrypted." }
        } finally { Pop-Location }
        $RestoreSource = $DecryptedTemp
    }
    & $PgRestorePath --list $RestoreSource | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Backup archive is invalid or unreadable." }

    # The application role intentionally has no CREATEDB privilege. Create only
    # the new, separately named target as postgres, then restore as the app role.
    $env:PGPASSWORD = $AdminPassword
    & $CreatedbPath --host=$PgHost --port=$PgPort --username=postgres --no-password --owner=$Values["POSTGRES_USER"] --template=template0 $TargetDatabase
    if ($LASTEXITCODE -ne 0) {
        throw "Target database could not be created by PostgreSQL administrator. Check the postgres password and confirm the target name is unused; no existing database was overwritten."
    }
    $env:PGPASSWORD = $Values["POSTGRES_PASSWORD"]

    & $PgRestorePath --host=$PgHost --port=$PgPort --username=$Values["POSTGRES_USER"] --no-password --exit-on-error --no-owner --dbname=$TargetDatabase $RestoreSource
    if ($LASTEXITCODE -ne 0) { throw "Restore failed. The incomplete database '$TargetDatabase' was left separate; the active database was not changed." }

    # Verify the restored database itself, not just the archive extraction exit code.
    $PublicTableCount = & $PsqlPath --host=$PgHost --port=$PgPort --username=$Values["POSTGRES_USER"] --no-password --dbname=$TargetDatabase --tuples-only --no-align --set=ON_ERROR_STOP=1 --command="SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';"
    if ($LASTEXITCODE -ne 0 -or [int]($PublicTableCount | Select-Object -Last 1) -lt 1) {
        throw "Restore verification failed: target database has no readable public tables. The active database was not changed."
    }
    $MigrationCount = & $PsqlPath --host=$PgHost --port=$PgPort --username=$Values["POSTGRES_USER"] --no-password --dbname=$TargetDatabase --tuples-only --no-align --set=ON_ERROR_STOP=1 --command="SELECT COUNT(*) FROM django_migrations;"
    if ($LASTEXITCODE -ne 0 -or [int]($MigrationCount | Select-Object -Last 1) -lt 1) {
        throw "Restore verification failed: Django migration history is missing or unreadable. The active database was not changed."
    }

    Write-Host "Restore completed and verified in separate database '$TargetDatabase'." -ForegroundColor Green
    Write-Host "Verified public tables: $($PublicTableCount | Select-Object -Last 1); Django migrations: $($MigrationCount | Select-Object -Last 1)." -ForegroundColor Green
    Write-Host "The active NEXUS database was not overwritten. Validate application login and critical reports before any controlled switch."
} finally {
    if ($DecryptedTemp -and (Test-Path -LiteralPath $DecryptedTemp)) {
        Remove-Item -LiteralPath $DecryptedTemp -Force -ErrorAction SilentlyContinue
    }
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    if ($AdminPasswordPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($AdminPasswordPtr)
    }
    $AdminPassword = $null
}
) { throw "Unsupported PostgreSQL tool: $Name" }
    $roots = @()
    if ($env:ProgramFiles) { $roots += (Join-Path $env:ProgramFiles "PostgreSQL") }
    if ($env:ProgramFiles) { }
    $roots += "C:\Program Files\PostgreSQL"
    if ($env:ProgramW6432 -and $env:ProgramW6432 -ne $env:ProgramFiles) {
        $roots += (Join-Path $env:ProgramW6432 "PostgreSQL")
    }
    $candidates = foreach ($root in ($roots | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $root -PathType Container) {
            Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
                Sort-Object { [version]($_.Name -replace '[^0-9.]','') } -Descending |
                ForEach-Object {
                    $candidate = Join-Path $_.FullName ("bin\$Name.exe")
                    if (Test-Path -LiteralPath $candidate -PathType Leaf) { Get-Item -LiteralPath $candidate }
                }
        }
    }
    $tool = $candidates | Select-Object -First 1
    if (-not $tool) { throw "Trusted PostgreSQL $Name.exe was not found under Program Files\PostgreSQL. Add the official PostgreSQL 16 client tools." }
    return $tool.FullName
}

if (-not (Test-Path -LiteralPath $BackupFile -PathType Leaf)) { throw "Backup file does not exist: $BackupFile" }
$BackupFile = (Resolve-Path -LiteralPath $BackupFile).Path
if ($TargetDatabase -notmatch "^[a-zA-Z][a-zA-Z0-9_]{2,62}$") { throw "Target database name must be 3-63 letters, digits or underscores and start with a letter." }

$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { $Python = Join-Path $Root "runtime\python.exe" }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Backend Python runtime is missing." }
$EnvFile = Join-Path $Backend ".env"
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) { throw "backend/.env is missing." }
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
if ($TargetDatabase -eq $Values["POSTGRES_DB"]) { throw "Restore target must be different from the active database." }
$PgHost = if ($Values["POSTGRES_HOST"]) { $Values["POSTGRES_HOST"] } else { "127.0.0.1" }
$PgPort = if ($Values["POSTGRES_PORT"]) { $Values["POSTGRES_PORT"] } else { "5432" }

$PgRestorePath = Get-PgTool "pg_restore"
$CreatedbPath = Get-PgTool "createdb"
$PsqlPath = Get-PgTool "psql"

$DecryptedTemp = $null
$RestoreSource = $BackupFile
$RestoreTempRoot = Join-Path $env:TEMP "AdmrailRestore"
New-Item -ItemType Directory -Path $RestoreTempRoot -Force | Out-Null
$RestoreIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $RestoreTempRoot /inheritance:r /grant:r "$($RestoreIdentity):(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict access to the restore temporary directory." }
Get-ChildItem -LiteralPath $RestoreTempRoot -Filter "nexus-restore-*.dump" -File -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-1) } |
    Remove-Item -Force -ErrorAction SilentlyContinue
$AdminPassword = $null
$AdminPasswordPtr = [IntPtr]::Zero
try {
    $AdminSecure = Read-Host "Enter PostgreSQL administrator (postgres) password to create the restore database" -AsSecureString
    $AdminPasswordPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($AdminSecure)
    $AdminPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($AdminPasswordPtr)
    if ([string]::IsNullOrWhiteSpace($AdminPassword)) { throw "The PostgreSQL administrator password cannot be empty." }
    if ($BackupFile.EndsWith(".nexusenc", [System.StringComparison]::OrdinalIgnoreCase)) {
        $DecryptedTemp = Join-Path $RestoreTempRoot ("nexus-restore-" + [guid]::NewGuid().ToString("N") + ".dump")
        Push-Location $Backend
        try {
            & $Python manage.py decrypt_backup_file --input $BackupFile --output $DecryptedTemp
            if ($LASTEXITCODE -ne 0) { throw "Encrypted backup could not be authenticated and decrypted." }
        } finally { Pop-Location }
        $RestoreSource = $DecryptedTemp
    }
    & $PgRestorePath --list $RestoreSource | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Backup archive is invalid or unreadable." }

    # The application role intentionally has no CREATEDB privilege. Create only
    # the new, separately named target as postgres, then restore as the app role.
    $env:PGPASSWORD = $AdminPassword
    & $CreatedbPath --host=$PgHost --port=$PgPort --username=postgres --no-password --owner=$Values["POSTGRES_USER"] --template=template0 $TargetDatabase
    if ($LASTEXITCODE -ne 0) {
        throw "Target database could not be created by PostgreSQL administrator. Check the postgres password and confirm the target name is unused; no existing database was overwritten."
    }
    $env:PGPASSWORD = $Values["POSTGRES_PASSWORD"]

    & $PgRestorePath --host=$PgHost --port=$PgPort --username=$Values["POSTGRES_USER"] --no-password --exit-on-error --no-owner --dbname=$TargetDatabase $RestoreSource
    if ($LASTEXITCODE -ne 0) { throw "Restore failed. The incomplete database '$TargetDatabase' was left separate; the active database was not changed." }

    # Verify the restored database itself, not just the archive extraction exit code.
    $PublicTableCount = & $PsqlPath --host=$PgHost --port=$PgPort --username=$Values["POSTGRES_USER"] --no-password --dbname=$TargetDatabase --tuples-only --no-align --set=ON_ERROR_STOP=1 --command="SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';"
    if ($LASTEXITCODE -ne 0 -or [int]($PublicTableCount | Select-Object -Last 1) -lt 1) {
        throw "Restore verification failed: target database has no readable public tables. The active database was not changed."
    }
    $MigrationCount = & $PsqlPath --host=$PgHost --port=$PgPort --username=$Values["POSTGRES_USER"] --no-password --dbname=$TargetDatabase --tuples-only --no-align --set=ON_ERROR_STOP=1 --command="SELECT COUNT(*) FROM django_migrations;"
    if ($LASTEXITCODE -ne 0 -or [int]($MigrationCount | Select-Object -Last 1) -lt 1) {
        throw "Restore verification failed: Django migration history is missing or unreadable. The active database was not changed."
    }

    Write-Host "Restore completed and verified in separate database '$TargetDatabase'." -ForegroundColor Green
    Write-Host "Verified public tables: $($PublicTableCount | Select-Object -Last 1); Django migrations: $($MigrationCount | Select-Object -Last 1)." -ForegroundColor Green
    Write-Host "The active NEXUS database was not overwritten. Validate application login and critical reports before any controlled switch."
} finally {
    if ($DecryptedTemp -and (Test-Path -LiteralPath $DecryptedTemp)) {
        Remove-Item -LiteralPath $DecryptedTemp -Force -ErrorAction SilentlyContinue
    }
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    if ($AdminPasswordPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($AdminPasswordPtr)
    }
    $AdminPassword = $null
}
