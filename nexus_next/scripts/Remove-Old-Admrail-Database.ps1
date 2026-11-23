param()

$ErrorActionPreference = "Stop"
$Psql = $null

function Read-PlainSecret([string]$Prompt) {
    $Secure = Read-Host $Prompt -AsSecureString
    $Ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Ptr) }
}

try {
    Write-Host "Admrail ERP - remove OLD PostgreSQL database" -ForegroundColor Yellow
    Write-Host "This utility targets ONLY database admrail_db and login role admrail_app." -ForegroundColor Yellow
    Write-Host "It does not uninstall PostgreSQL or delete other databases." -ForegroundColor Yellow
    Write-Host ""

    $Service = Get-Service -Name "postgresql-x64-16" -ErrorAction SilentlyContinue
    if (-not $Service) { throw "PostgreSQL 16 Windows service was not found." }
    if ($Service.Status -ne "Running") { throw "PostgreSQL 16 service is not running. Start it and retry." }

    $PsqlCommand = Get-Command "psql.exe" -ErrorAction SilentlyContinue
    if ($PsqlCommand) { $Psql = $PsqlCommand.Source }
    else {
        $PsqlPath = "C:\Program Files\PostgreSQL\16\bin\psql.exe"
        if (-not (Test-Path -LiteralPath $PsqlPath)) { throw "psql.exe was not found. Install PostgreSQL Command Line Tools." }
        $Psql = $PsqlPath
    }

    $AdminPassword = Read-PlainSecret "Enter PostgreSQL administrator (postgres) password"
    if ([string]::IsNullOrWhiteSpace($AdminPassword)) { throw "The PostgreSQL password cannot be empty." }
    $env:PGPASSWORD = $AdminPassword
    $Test = & $Psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d postgres -Atc "SELECT current_user;" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Could not connect to PostgreSQL: $($Test -join ' ')" }
    $AdminPassword = $null

    $DbExists = (& $Psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='admrail_db';").Trim()
    $RoleExists = (& $Psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d postgres -Atc "SELECT 1 FROM pg_roles WHERE rolname='admrail_app';").Trim()

    if (-not $DbExists -and -not $RoleExists) {
        Write-Host "Nothing to remove: admrail_db and admrail_app do not exist." -ForegroundColor Green
        exit 0
    }

    Write-Host ""
    Write-Host "The following objects were found:" -ForegroundColor Cyan
    if ($DbExists) { Write-Host "  DATABASE: admrail_db" }
    if ($RoleExists) { Write-Host "  LOGIN ROLE: admrail_app" }
    Write-Host ""
    Write-Host "WARNING: deleting the database permanently deletes all data inside admrail_db." -ForegroundColor Red
    Write-Host "If you may need the old records, cancel now and take a backup first." -ForegroundColor Red
    $Confirmation = Read-Host "To confirm, type exactly DELETE admrail_db"
    if ($Confirmation -cne "DELETE admrail_db") { throw "Confirmation did not match. Nothing was deleted." }

    if ($DbExists) {
        Write-Host "Disconnecting sessions and deleting admrail_db..." -ForegroundColor Yellow
        & $Psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "DROP DATABASE IF EXISTS admrail_db WITH (FORCE);"
        if ($LASTEXITCODE -ne 0) { throw "Could not delete admrail_db. The role was not removed." }
    }

    if ($RoleExists) {
        Write-Host "Removing the dedicated admrail_app login role..." -ForegroundColor Yellow
        & $Psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "DROP ROLE IF EXISTS admrail_app;"
        if ($LASTEXITCODE -ne 0) {
            throw "The database was deleted, but PostgreSQL reports that admrail_app still owns or has privileges on other objects. No other objects were removed; ask before proceeding."
        }
    }

    Write-Host ""
    Write-Host "Old Admrail database and application role removed successfully." -ForegroundColor Green
    Write-Host "You can now run Setup-Admrail.cmd to create a clean Admrail ERP v2 database." -ForegroundColor Green
    Read-Host "Press Enter to close"
}
catch {
    Write-Host ""
    Write-Host "REMOVAL STOPPED: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}
finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}
