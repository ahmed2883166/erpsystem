param(
    [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) "release")
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

$Tag = Get-Date -Format "yyyyMMdd_HHmmss"
$PackageName = "NEXUS-ERP-Trial-$Tag"
$Stage = Join-Path ([System.IO.Path]::GetTempPath()) $PackageName
$ZipPath = Join-Path $OutputDirectory "$PackageName.zip"

if (Test-Path -LiteralPath $Stage) {
    Remove-Item -LiteralPath $Stage -Recurse -Force
}
New-Item -ItemType Directory -Path $Stage | Out-Null

try {
    Get-ChildItem -LiteralPath $Root -File -Recurse -Force | ForEach-Object {
        $Relative = $_.FullName.Substring($Root.Length).TrimStart("\", "/")
        $Segments = $Relative -split "[\\/]"
        if ($Segments -contains ".git" -or
            $Segments -contains ".venv" -or
            $Segments -contains "node_modules" -or
            $Segments -contains "__pycache__" -or
            $Segments -contains ".pytest_cache" -or
            $Segments -contains "dist" -or
            $Segments -contains "release" -or
            $Segments -contains "data" -or
            $Segments -contains "backups" -or
            $Relative -match '(^|[\\/])backend[\\/]\.env$' -or
            $_.Extension -in @(".pyc", ".pyo", ".sqlite", ".sqlite3", ".db", ".dump") -or
            $_.Name -eq ".DS_Store") {
            return
        }

        $Destination = Join-Path $Stage $Relative
        $Parent = Split-Path -Parent $Destination
        New-Item -ItemType Directory -Force -Path $Parent | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $Destination -Force
    }

    if (Test-Path -LiteralPath $ZipPath) {
        Remove-Item -LiteralPath $ZipPath -Force
    }
    Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $ZipPath -CompressionLevel Optimal
    $Hash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash
    Set-Content -LiteralPath "$ZipPath.sha256" -Value "$Hash  $(Split-Path -Leaf $ZipPath)" -Encoding ASCII
    Write-Host "Trial package created: $ZipPath" -ForegroundColor Green
    Write-Host "SHA256: $Hash"
}
finally {
    if (Test-Path -LiteralPath $Stage) {
        Remove-Item -LiteralPath $Stage -Recurse -Force
    }
}
