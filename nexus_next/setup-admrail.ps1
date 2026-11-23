param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"
$Python = Join-Path $Root "runtime\python.exe"
$EnvExample = Join-Path $Backend ".env.example"
$EnvFile = Join-Path $Backend ".env"

function Read-PlainSecret([string]$Prompt) {
    $Secure = Read-Host $Prompt -AsSecureString
    $Ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Ptr) }
}

function Invoke-Psql([string]$Database, [string]$Sql) {
    $Output = & $script:Psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d $Database -Atc $Sql 2>&1
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL rejected a setup command: $($Output -join ' ')" }
    return ($Output -join [Environment]::NewLine).Trim()
}

try {
    Write-Host "Admrail ERP - guided first-time setup" -ForegroundColor Cyan
    Write-Host "This wizard does NOT install PostgreSQL. Install PostgreSQL 16 for Windows yourself first." -ForegroundColor Yellow
    if (-not (Test-Path -LiteralPath $Python)) { throw "Embedded Python is missing. Extract the complete package before running setup." }
    if (-not (Test-Path -LiteralPath $EnvExample)) { throw "backend\.env.example is missing." }

    $Service = Get-Service -Name "postgresql-x64-16" -ErrorAction SilentlyContinue
    if (-not $Service) { throw "PostgreSQL 16 service was not found. Install PostgreSQL 16, then run this setup again." }
    if ($Service.Status -ne "Running") { throw "PostgreSQL 16 is installed but not running. Start its Windows service, then retry." }

    $PsqlCommand = Get-Command "psql.exe" -ErrorAction SilentlyContinue
    if ($PsqlCommand) { $script:Psql = $PsqlCommand.Source }
    else {
        $PsqlPath = "C:\Program Files\PostgreSQL\16\bin\psql.exe"
        if (-not (Test-Path -LiteralPath $PsqlPath)) { throw "psql.exe was not found. In the PostgreSQL installer, include Command Line Tools." }
        $script:Psql = $PsqlPath
    }

    if (Test-Path -LiteralPath $EnvFile) {
        throw "backend\.env already exists. Setup will not overwrite it. Back it up and contact support before retrying."
    }

    $AdminPassword = Read-PlainSecret "Enter the PostgreSQL administrator (postgres) password"
    if ([string]::IsNullOrWhiteSpace($AdminPassword)) { throw "The PostgreSQL password cannot be empty." }
    $env:PGPASSWORD = $AdminPassword
    $null = Invoke-Psql "postgres" "SELECT 1;"
    $AdminPassword = $null

    $ExistingRole = Invoke-Psql "postgres" "SELECT 1 FROM pg_roles WHERE rolname = 'admrail_app';"
    $ExistingDb = Invoke-Psql "postgres" "SELECT 1 FROM pg_database WHERE datname = 'admrail_db';"
    if ($ExistingRole -or $ExistingDb) {
        throw "An older Admrail database or login already exists (admrail_db / admrail_app). Setup will not delete it. If you have confirmed that its data is no longer needed, run Remove-Old-Admrail-Database.cmd from the package, then start setup again."
    }

    $Bytes = New-Object byte[] 36
    $Rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Rng.GetBytes($Bytes) } finally { $Rng.Dispose() }
    $DbPassword = [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+','A').Replace('/','B')
    $SqlPassword = $DbPassword.Replace("'","''")
    $null = Invoke-Psql "postgres" "CREATE ROLE admrail_app LOGIN PASSWORD '$SqlPassword';"
    $null = Invoke-Psql "postgres" "CREATE DATABASE admrail_db OWNER admrail_app ENCODING 'UTF8';"
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue

    $SecretBytes = New-Object byte[] 64
    $Rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Rng.GetBytes($SecretBytes) } finally { $Rng.Dispose() }
    $DjangoSecret = [Convert]::ToBase64String($SecretBytes).TrimEnd('=').Replace('+','-').Replace('/','_')
    $EnvText = Get-Content -LiteralPath $EnvExample -Raw
    $Replacements = @{
        "DJANGO_SECRET_KEY" = $DjangoSecret
        "DJANGO_DEBUG" = "False"
        "DJANGO_ALLOWED_HOSTS" = "127.0.0.1,localhost"
        "POSTGRES_DB" = "admrail_db"
        "POSTGRES_USER" = "admrail_app"
        "POSTGRES_PASSWORD" = $DbPassword
        "POSTGRES_HOST" = "127.0.0.1"
        "POSTGRES_PORT" = "5432"
    }
    foreach ($Key in $Replacements.Keys) {
        $Pattern = "(?m)^" + [regex]::Escape($Key) + "=.*$"
        $EnvText = [regex]::Replace($EnvText, $Pattern, "$Key=$($Replacements[$Key])")
    }
    [IO.File]::WriteAllText($EnvFile, $EnvText, (New-Object System.Text.UTF8Encoding($false)))

    $env:DJANGO_SETTINGS_MODULE = "config.settings"
    Push-Location $Backend
    try {
        Write-Host "Checking application configuration..." -ForegroundColor Cyan
        & $Python manage.py check
        if ($LASTEXITCODE -ne 0) { throw "Django configuration check failed." }

        Write-Host "Creating database tables..." -ForegroundColor Cyan
        & $Python manage.py migrate --noinput
        if ($LASTEXITCODE -ne 0) { throw "Database migration failed. The new database was not deleted." }

        $Username = Read-Host "Choose the first Admrail administrator username"
        if ([string]::IsNullOrWhiteSpace($Username) -or $Username -notmatch "^[A-Za-z0-9_.@+-]{3,150}$") {
            throw "Username must be 3-150 characters and use only letters, numbers, . _ @ + -"
        }
        $Email = Read-Host "Administrator email address"
        if ([string]::IsNullOrWhiteSpace($Email)) { throw "Administrator email is required." }
        $AdminAppPassword = Read-PlainSecret "Choose administrator password (minimum 12 characters)"
        if ($AdminAppPassword.Length -lt 12) { throw "Administrator password must be at least 12 characters." }

        $env:DJANGO_SUPERUSER_USERNAME = $Username
        $env:DJANGO_SUPERUSER_EMAIL = $Email
        $env:DJANGO_SUPERUSER_PASSWORD = $AdminAppPassword
        & $Python manage.py createsuperuser --noinput
        if ($LASTEXITCODE -ne 0) { throw "Could not create the administrator account." }
        Remove-Item Env:DJANGO_SUPERUSER_PASSWORD -ErrorAction SilentlyContinue
        Remove-Item Env:DJANGO_SUPERUSER_USERNAME -ErrorAction SilentlyContinue
        Remove-Item Env:DJANGO_SUPERUSER_EMAIL -ErrorAction SilentlyContinue
        $AdminAppPassword = $null

        & $Python manage.py bootstrap_platform_owner --username $Username
        if ($LASTEXITCODE -ne 0) { throw "Could not register the administrator as the platform owner." }
    }
    finally { Pop-Location }

    Write-Host ""
    Write-Host "Admrail first-time setup completed successfully." -ForegroundColor Green
    Write-Host "Next: double-click run-admrail.cmd, then open http://127.0.0.1:8000" -ForegroundColor Green
    Write-Host "The database password and Django secret are stored in backend\.env. Keep that file private." -ForegroundColor Yellow
    Read-Host "Press Enter to close"
}
catch {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:DJANGO_SUPERUSER_PASSWORD -ErrorAction SilentlyContinue
    Write-Host ""
    Write-Host "SETUP STOPPED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "No existing database was dropped or overwritten. If setup stopped after database creation, admrail_db may remain; do not rerun blindly." -ForegroundColor Yellow
    Read-Host "Press Enter to close"
    exit 1
}
