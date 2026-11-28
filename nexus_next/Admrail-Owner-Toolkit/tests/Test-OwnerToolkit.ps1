#requires -Version 5.1
$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$Toolkit = Join-Path $RepoRoot "nexus_next\Admrail-Owner-Toolkit"
$Scripts = Join-Path $RepoRoot "nexus_next\scripts"
$Failures = New-Object System.Collections.Generic.List[string]
$Passed = 0

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { $script:Failures.Add($Message) } else { $script:Passed++ }
}
function Read-RequiredFile([string]$Path) {
    Assert-True (Test-Path -LiteralPath $Path -PathType Leaf) "Missing required file: $Path"
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "" }
    return Get-Content -LiteralPath $Path -Raw
}

$required = @(
    (Join-Path $Toolkit "Admrail-Owner-Toolkit.ps1"),
    (Join-Path $Toolkit "Start-Owner-Toolkit.cmd"),
    (Join-Path $Toolkit "README.md"),
    (Join-Path $Scripts "Admrail-Database-Manager.ps1"),
    (Join-Path $Scripts "backup-postgres.ps1"),
    (Join-Path $Scripts "restore-postgres-to-new-db.ps1"),
    (Join-Path $Scripts "initialize-backup-encryption.ps1")
)
foreach ($path in $required) { [void](Read-RequiredFile $path) }

$psFiles = @(
    (Join-Path $Toolkit "Admrail-Owner-Toolkit.ps1"),
    (Join-Path $Scripts "Admrail-Database-Manager.ps1"),
    (Join-Path $Scripts "backup-postgres.ps1"),
    (Join-Path $Scripts "restore-postgres-to-new-db.ps1"),
    (Join-Path $Scripts "initialize-backup-encryption.ps1")
)
foreach ($path in $psFiles) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$parseErrors)
    Assert-True ($parseErrors.Count -eq 0) ("PowerShell parse errors in {0}: {1}" -f $path, (($parseErrors | ForEach-Object { $_.Message }) -join "; "))
}

$toolkitText = Read-RequiredFile (Join-Path $Toolkit "Admrail-Owner-Toolkit.ps1")
$dbManagerText = Read-RequiredFile (Join-Path $Scripts "Admrail-Database-Manager.ps1")
$backupText = Read-RequiredFile (Join-Path $Scripts "backup-postgres.ps1")
$restoreText = Read-RequiredFile (Join-Path $Scripts "restore-postgres-to-new-db.ps1")
$launcherText = Read-RequiredFile (Join-Path $Toolkit "Start-Owner-Toolkit.cmd")
$readmeText = Read-RequiredFile (Join-Path $Toolkit "README.md")

Assert-True ($toolkitText -match "Rfc2898DeriveBytes" -and $toolkitText -match "220000") "Owner PIN must use PBKDF2 with the configured work factor."
Assert-True ($toolkitText -match "owner-lockout" -and $toolkitText -match "locked_until") "Owner PIN lockout protection is missing."
Assert-True ($toolkitText -match 'Restore backup to a NEW database') "Owner menu must label restore as a separate-database operation."
Assert-True ($dbManagerText -match "Save-VerifiedBackup" -and $dbManagerText -match "DROP DATABASE") "Database deletion must retain the verified-backup path."
Assert-True ($dbManagerText -match "DELETE ROLE" -and $dbManagerText -match "NOLOGIN") "PostgreSQL role management safeguards are missing."
Assert-True ($backupText -match "encrypt_backup_file" -and $backupText -match "nexusenc") "Backup script must encrypt archives."
Assert-True ($backupText.Contains('Remove-Item Env:\PGPASSWORD')) "Backup script must clear the PostgreSQL password environment variable."
Assert-True ($restoreText -match "TargetDatabase" -and $restoreText -match "must be different from the active database") "Restore must reject the active database as a target."
Assert-True ($restoreText -match "django_migrations" -and $restoreText -match "information_schema.tables") "Restore must verify the restored schema and migration history."
Assert-True ($launcherText -match "Admrail-Owner-Toolkit.ps1") "Launcher does not invoke the owner toolkit."
Assert-True ($readmeText -match "does **not** download or install PostgreSQL" -and $readmeText -match "first/main ERP administrator account") "README must preserve the agreed split: owner manages PostgreSQL; ERP creates the company and first administrator."

if ($Failures.Count -gt 0) {
    Write-Host ("FAILED: {0} checks passed; {1} failed." -f $Passed, $Failures.Count) -ForegroundColor Red
    $Failures | ForEach-Object { Write-Host (" - " + $_) -ForegroundColor Red }
    exit 1
}
Write-Host ("PASS: {0} owner-toolkit static safety checks." -f $Passed) -ForegroundColor Green
