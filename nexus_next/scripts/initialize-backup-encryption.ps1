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
$RecoveryDirectory = Split-Path -Parent $RecoveryFile
if (-not $RecoveryDirectory) { $RecoveryDirectory = (Get-Location).Path }
if (-not (Test-Path -LiteralPath $RecoveryDirectory -PathType Container)) { throw "Recovery USB folder is unavailable." }
if (Test-Path -LiteralPath $RecoveryFile) { throw "Recovery file already exists; it will not be replaced." }
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
