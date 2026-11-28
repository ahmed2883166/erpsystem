#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$AppRoot
)
$ErrorActionPreference = "Stop"

function Get-PgTool([string]$Name) {
    $roots = @()
    if ($env:ProgramFiles) { $roots += (Join-Path $env:ProgramFiles "PostgreSQL") }
    if ($env:ProgramW6432 -and $env:ProgramW6432 -ne $env:ProgramFiles) { $roots += (Join-Path $env:ProgramW6432 "PostgreSQL") }
    foreach ($root in $roots) {
        foreach ($version in @("18","17","16","15","14")) {
            $candidate = Join-Path $root ($version + "\bin\" + $Name + ".exe")
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return (Resolve-Path -LiteralPath $candidate).Path }
        }
    }
    throw "لم يتم العثور على أداة PostgreSQL الرسمية $Name.exe داخل Program Files."
}
function Quote-Identifier([string]$Value) { return '"' + $Value.Replace('"','""') + '"' }
function New-RandomSecret([int]$Bytes=48) {
    $buffer = New-Object byte[] $Bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($buffer).TrimEnd('=').Replace('+','A').Replace('/','B')
}
function Set-EnvValue([System.Collections.Generic.List[string]]$Lines,[string]$Key,[string]$Value) {
    $found = $false
    for ($i=0; $i -lt $Lines.Count; $i++) {
        if ($Lines[$i] -match ('^\s*' + [regex]::Escape($Key) + '\s*=')) {
            $Lines[$i] = $Key + "=" + $Value
            $found = $true
        }
    }
    if (-not $found) { $Lines.Add($Key + "=" + $Value) }
}
function Invoke-Psql([string]$Psql,[string]$HostName,[string]$Port,[string]$User,[string]$Database,[string]$Sql) {
    & $Psql -X -q -w -v ON_ERROR_STOP=1 -h $HostName -p $Port -U $User -d $Database -Atc $Sql 2>&1
    if ($LASTEXITCODE -ne 0) { throw "فشل الاتصال أو تنفيذ أمر PostgreSQL. تحقق من بيانات الدخول والصلاحيات." }
}
$secureAdmin = $null
$adminPassword = $null
$appPassword = $null
$sqlFile = $null
$createdRole = $false
$createdDatabase = $false
$envFile = Join-Path (Join-Path $AppRoot "backend") ".env"
try {
    $resolvedRoot = (Resolve-Path -LiteralPath $AppRoot -ErrorAction Stop).Path
    $backend = Join-Path $resolvedRoot "backend"
    $template = Join-Path $backend ".env.example"
    if (-not (Test-Path -LiteralPath (Join-Path $backend "manage.py") -PathType Leaf)) { throw "مجلد التطبيق لا يحتوي backend\manage.py." }
    if (-not (Test-Path -LiteralPath $template -PathType Leaf)) { throw "ملف backend\.env.example غير موجود." }
    if (Test-Path -LiteralPath $envFile -PathType Leaf) {
        throw "يوجد backend\.env بالفعل. لم يتم استبداله أو تعديل أي إعداد. استخدم إدارة القاعدة الحالية بعد مراجعتها."
    }

    $hostName = (Read-Host "عنوان خادم PostgreSQL (Enter = 127.0.0.1)").Trim()
    if (-not $hostName) { $hostName = "127.0.0.1" }
    $port = (Read-Host "منفذ PostgreSQL (Enter = 5432)").Trim()
    if (-not $port) { $port = "5432" }
    if ($port -notmatch '^\d{1,5}$' -or [int]$port -lt 1 -or [int]$port -gt 65535) { throw "منفذ PostgreSQL غير صالح." }
    $adminUser = (Read-Host "اسم مسؤول PostgreSQL (Enter = postgres)").Trim()
    if (-not $adminUser) { $adminUser = "postgres" }
    if ($adminUser -notmatch '^[A-Za-z][A-Za-z0-9_]{0,62}$') { throw "اسم مسؤول PostgreSQL غير صالح." }

    $dbName = (Read-Host "اسم قاعدة AdmRail الجديدة (Enter = admrail)").Trim()
    if (-not $dbName) { $dbName = "admrail" }
    if ($dbName -notmatch '^[A-Za-z][A-Za-z0-9_]{2,62}$') { throw "اسم قاعدة البيانات يجب أن يبدأ بحرف إنجليزي ويحتوي حروفًا أو أرقامًا أو _." }
    $appUser = (Read-Host "اسم مستخدم اتصال التطبيق (Enter = admrail_app)").Trim()
    if (-not $appUser) { $appUser = "admrail_app" }
    if ($appUser -notmatch '^[A-Za-z][A-Za-z0-9_]{2,62}$' -or $appUser -match '^pg_') { throw "اسم مستخدم التطبيق غير صالح." }

    $psql = Get-PgTool "psql"
    Write-Host "أدخل كلمة مرور مسؤول PostgreSQL. لن تُحفظ في ملفات الأداة أو سجلها." -ForegroundColor Yellow
    $secureAdmin = Read-Host "كلمة مرور مسؤول PostgreSQL" -AsSecureString
    $ptr = [IntPtr]::Zero
    try { $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureAdmin); $adminPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) } }
    if ([string]::IsNullOrEmpty($adminPassword)) { throw "كلمة مرور مسؤول PostgreSQL مطلوبة." }

    $env:PGPASSWORD = $adminPassword
    try {
        $version = Invoke-Psql $psql $hostName $port $adminUser "postgres" "SELECT current_setting('server_version');"
        Write-Host ("تم الاتصال بخادم PostgreSQL: " + $version) -ForegroundColor Green
        $dbExists = Invoke-Psql $psql $hostName $port $adminUser "postgres" ("SELECT 1 FROM pg_database WHERE datname='" + $dbName.Replace("'","''") + "';")
        $roleExists = Invoke-Psql $psql $hostName $port $adminUser "postgres" ("SELECT 1 FROM pg_roles WHERE rolname='" + $appUser.Replace("'","''") + "';")
        if ($dbExists) { throw "قاعدة البيانات [$dbName] موجودة بالفعل. لم يتم لمسها." }
        if ($roleExists) { throw "مستخدم PostgreSQL [$appUser] موجود بالفعل. لم يتم تعديله." }

        $appPassword = New-RandomSecret 48
        $escapedPassword = $appPassword.Replace("'","''")
        $sqlFile = Join-Path ([IO.Path]::GetTempPath()) ("admrail-provision-" + [guid]::NewGuid().ToString("N") + ".sql")
        $sql = "CREATE ROLE " + (Quote-Identifier $appUser) + " LOGIN PASSWORD '" + $escapedPassword + "' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;" + [Environment]::NewLine
        [IO.File]::WriteAllText($sqlFile,$sql,(New-Object System.Text.UTF8Encoding($false)))
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        & (Join-Path $env:SystemRoot "System32\icacls.exe") $sqlFile /inheritance:r /grant:r "$($identity):F" "SYSTEM:F" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "تعذر حماية ملف SQL المؤقت؛ أُلغيت العملية." }
        & $psql -X -q -w -v ON_ERROR_STOP=1 -h $hostName -p $port -U $adminUser -d postgres -f $sqlFile 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "تعذر إنشاء مستخدم التطبيق. لم يتم إنشاء قاعدة البيانات." }
        $createdRole = $true

        $createdb = Get-PgTool "createdb"
        & $createdb --no-password --host=$hostName --port=$port --username=$adminUser --owner=$appUser --encoding=UTF8 --template=template0 $dbName 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "تعذر إنشاء قاعدة البيانات الجديدة. لن يتم تعديل أي قاعدة موجودة." }
        $createdDatabase = $true

        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
        $env:PGPASSWORD = $appPassword
        $identityCheck = Invoke-Psql $psql $hostName $port $appUser $dbName "SELECT current_user || ':' || current_database();"
        if ($identityCheck -ne ($appUser + ":" + $dbName)) { throw "فشل اختبار دخول مستخدم التطبيق إلى القاعدة الجديدة." }
    } finally {
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    }

    $lines = New-Object 'System.Collections.Generic.List[string]'
    foreach ($line in [IO.File]::ReadAllLines($template)) { $lines.Add($line) }
    Set-EnvValue $lines "DJANGO_SECRET_KEY" (New-RandomSecret 64)
    Set-EnvValue $lines "DJANGO_DEBUG" "False"
    Set-EnvValue $lines "POSTGRES_DB" $dbName
    Set-EnvValue $lines "POSTGRES_USER" $appUser
    Set-EnvValue $lines "POSTGRES_PASSWORD" $appPassword
    Set-EnvValue $lines "POSTGRES_HOST" $hostName
    Set-EnvValue $lines "POSTGRES_PORT" $port
    $tempEnv = $envFile + ".provisioning"
    [IO.File]::WriteAllLines($tempEnv,$lines,(New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tempEnv -Destination $envFile
    $createdRole = $false
    $createdDatabase = $false
    Write-Host "تم إنشاء قاعدة AdmRail ومستخدم التطبيق واختبار الاتصال، وإنشاء backend\.env." -ForegroundColor Green
    Write-Host "الخطوة التالية: شغّل فحوص Django ثم migrations وتجهيز البيانات المرجعية." -ForegroundColor Cyan
    Write-Host "كلمة مرور مسؤول PostgreSQL لم تُحفظ. كلمة مرور التطبيق محفوظة في backend\.env بصلاحيات نظام الملفات الحالية."
} catch {
    if ($createdDatabase -or $createdRole) {
        Write-Host "تنبيه: قد تكون موارد جديدة قد أُنشئت أثناء العملية؛ لم يتم حذفها تلقائيًا حفاظًا على السلامة. راجع القاعدة والمستخدم قبل إعادة المحاولة." -ForegroundColor Yellow
    }
    throw
} finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    if ($sqlFile -and (Test-Path -LiteralPath $sqlFile)) { Remove-Item -LiteralPath $sqlFile -Force -ErrorAction SilentlyContinue }
    $adminPassword = $null
    $appPassword = $null
    if ($secureAdmin) { $secureAdmin.Dispose() }
}
