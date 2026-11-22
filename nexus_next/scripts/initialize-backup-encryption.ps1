param(
    [Parameter(Mandatory = $true)]
    [string]$RecoveryFile
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = Join-Path $Root "runtime\python.exe"
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Backend Python runtime is missing." }
if (-not (Test-Path -LiteralPath $RecoveryFile -PathType Leaf)) { throw "Recovery USB file does not exist." }
$KeyFile = Join-Path $Backend ".secrets\backup-key.dpapi"
if (Test-Path -LiteralPath $KeyFile) { throw "A local backup key already exists; it will not be replaced." }
Push-Location $Backend
try {
    & $Python manage.py init_backup_encryption --recovery-file $RecoveryFile
    if ($LASTEXITCODE -ne 0) { throw "Backup encryption initialization failed." }
} finally { Pop-Location }
$CurrentIdentity = "$env:USERDOMAIN\$env:USERNAME"
$KeyDirectory = Split-Path -Parent $KeyFile
& icacls.exe $KeyDirectory /inheritance:r /grant:r "${CurrentIdentity}:(OI)(CI)(F)" "SYSTEM:(OI)(CI)(F)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict access to the backup key directory." }
& icacls.exe $KeyFile /inheritance:r /grant:r "${CurrentIdentity}:(F)" "SYSTEM:(F)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict access to the local backup encryption key." }
Write-Host "Encrypted backup key initialized and access restricted." -ForegroundColor Green
Write-Host "Keep the recovery USB offline and store its passphrase separately."
