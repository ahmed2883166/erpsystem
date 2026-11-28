#requires -Version 5.1
# Admrail ERP v2 - Arabic guided PostgreSQL maintenance utility.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$EnvFile = Join-Path $Backend ".env"
$Python = Join-Path $Root "runtime\python.exe"
if (-not (Test-Path $Python)) { $Python = Join-Path $Backend ".venv\Scripts\python.exe" }

function Read-Config {
    if (-not (Test-Path $EnvFile)) { throw "ملف backend\.env غير موجود." }
    $c = @{}
    foreach ($line in Get-Content $EnvFile) {
        if ($line -match '^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*?)\s*$') {
            $v = $Matches[2].Trim()
            if ($v.Length -ge 2 -and (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'")))) { $v = $v.Substring(1,$v.Length-2) }
            $c[$Matches[1]] = $v
        }
    }
    foreach ($k in @("POSTGRES_DB","POSTGRES_USER","POSTGRES_PASSWORD")) { if (-not $c[$k]) { throw "الإعداد $k غير موجود في backend\.env." } }
    if (-not $c.POSTGRES_HOST) { $c.POSTGRES_HOST = "127.0.0.1" }
    if (-not $c.POSTGRES_PORT) { $c.POSTGRES_PORT = "5432" }
    return $c
}
function Save-ConfigValue([string]$Key,[string]$Value) {
    $out = [System.Collections.Generic.List[string]]::new(); $found = $false
    foreach ($line in Get-Content $EnvFile) {
        if ($line -match ('^\s*(?:export\s+)?' + [regex]::Escape($Key) + '\s*=')) { $out.Add("$Key=$Value"); $found = $true } else { $out.Add($line) }
    }
    if (-not $found) { $out.Add("$Key=$Value") }
    $tmp = "$EnvFile.admrail-tmp"
    [IO.File]::WriteAllLines($tmp,$out,(New-Object System.Text.UTF8Encoding($false)))
    Move-Item $tmp $EnvFile -Force
}
function Get-PgTool([string]$Name) {
    $allowed = @('pg_dump', 'pg_restore', 'psql', 'createdb')
    if ($allowed -notcontains $Name) { throw "Unsupported PostgreSQL tool: $Name" }
    $roots = @()
    if ($env:ProgramFiles) { $roots += (Join-Path $env:ProgramFiles 'PostgreSQL') }
    if ($env:ProgramW6432 -and $env:ProgramW6432 -ne $env:ProgramFiles) { $roots += (Join-Path $env:ProgramW6432 'PostgreSQL') }
    $versions = @('16', '17', '18', '15', '14')
    foreach ($root in $roots) {
        foreach ($version in $versions) {
            $candidate = Join-Path $root ($version + '\bin\' + $Name + '.exe')
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return (Resolve-Path -LiteralPath $candidate).Path }
        }
    }
    throw "Trusted PostgreSQL $Name.exe was not found under Program Files\PostgreSQL. Install the official PostgreSQL client tools."
}
function Assert-AppStopped {
    $listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    if ($listener) { throw "أوقف تشغيل Admrail على جهاز الخادم أولًا (المنفذ 8000 ما زال مستخدمًا)، ثم أعد المحاولة." }
}
function Read-AdminPassword {
    $s = Read-Host "أدخل كلمة مرور مسؤول PostgreSQL (postgres)" -AsSecureString
    $p = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { $script:AdminPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p) }
    if (-not $script:AdminPassword) { throw "كلمة المرور مطلوبة." }
    $null = Invoke-Psql "SELECT 1;"
}
function Invoke-Psql([string]$Sql,[string]$Db="postgres") {
    $c = Read-Config; $psql = Get-PgTool "psql"
    $env:PGPASSWORD = $script:AdminPassword
    $o = & $psql -X -q -v ON_ERROR_STOP=1 -h $c.POSTGRES_HOST -p $c.POSTGRES_PORT -U postgres -d $Db -Atc $Sql 2>&1
    if ($LASTEXITCODE -ne 0) { throw "فشل أمر PostgreSQL: $($o -join ' ')" }
    return (($o | Out-String).Trim())
}
function Quote-Name([string]$n) { return '"' + $n.Replace('"','""') + '"' }
function New-DbPassword {
    $b = New-Object byte[] 36; $r = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $r.GetBytes($b) } finally { $r.Dispose() }
    return [Convert]::ToBase64String($b).TrimEnd('=').Replace('+','A').Replace('/','B')
}
function Ensure-BackupEncryption {
    $keyFile = Join-Path $Backend ".secrets\backup-key.dpapi"
    if (Test-Path -LiteralPath $keyFile -PathType Leaf) { return }
    Write-Host "يلزم إعداد مفتاح تشفير النسخ مرة واحدة فقط." -ForegroundColor Yellow
    Write-Host "جهّز وحدة USB واحفظ عليها ملف الاسترداد المشفر؛ لا تحفظه بجوار قاعدة البيانات."
    $recoveryFile = Read-Host "اكتب مسار ملف الاسترداد على USB (مثال E:\Admrail-Recovery\backup-key.recovery)"
    if ([string]::IsNullOrWhiteSpace($recoveryFile)) { throw "تم إلغاء إعداد مفتاح التشفير؛ لم تُنشأ نسخة ولم تتغير قاعدة البيانات." }
    $initializer = Join-Path $PSScriptRoot "initialize-backup-encryption.ps1"
    if (-not (Test-Path -LiteralPath $initializer -PathType Leaf)) { throw "ملف إعداد تشفير النسخ غير موجود." }
    & $initializer -RecoveryFile $recoveryFile
    if (-not (Test-Path -LiteralPath $keyFile -PathType Leaf)) { throw "لم يكتمل إعداد مفتاح التشفير؛ لم تُنشأ نسخة ولم تتغير قاعدة البيانات." }
}
function Save-VerifiedBackup([string]$Db) {
    $c = Read-Config; $dump = Get-PgTool "pg_dump"; $restore = Get-PgTool "pg_restore"
    $py = $script:Python
    if (-not (Test-Path $py)) { throw "Python المرفق غير موجود." }
    $folder = Read-Host "مسار حفظ النسخة المشفرة على قرص غير C (مثال D:\Admrail-Backups)"
    if (-not $folder) { throw "مسار النسخ مطلوب." }
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    if ([IO.Path]::GetPathRoot($folder).TrimEnd('\') -ieq "C:") { throw "اختر قرصًا غير C." }
    Ensure-BackupEncryption
    $tag = Get-Date -Format "yyyyMMdd_HHmmss"
    $secureTempRoot = Join-Path ([IO.Path]::GetTempPath()) "AdmrailOwnerToolkit"
    New-Item -ItemType Directory -Force -Path $secureTempRoot | Out-Null
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & (Join-Path $env:SystemRoot "System32\icacls.exe") $secureTempRoot /inheritance:r /grant:r "$($identity):(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "تعذر تأمين مجلد الملفات المؤقتة؛ أُلغيت عملية النسخ قبل إنشاء dump غير مشفر." }
    $operationTemp = Join-Path $secureTempRoot ([guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $operationTemp | Out-Null
    & (Join-Path $env:SystemRoot "System32\icacls.exe") $operationTemp /inheritance:r /grant:r "$($identity):(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -LiteralPath $operationTemp -Recurse -Force -ErrorAction SilentlyContinue
        throw "تعذر تقييد صلاحيات مجلد النسخة المؤقتة."
    }
    $plain = Join-Path $operationTemp "database.dump"
    $enc = Join-Path $folder "admrail-$Db-$tag.dump.nexusenc"
    try {
        $env:PGPASSWORD = $script:AdminPassword
        & $dump --host=$($c.POSTGRES_HOST) --port=$($c.POSTGRES_PORT) --username=postgres --dbname=$Db --format=custom --no-password --file=$plain
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $plain)) { throw "فشل إنشاء النسخة." }
        & $restore --list $plain | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "فحص النسخة فشل؛ لن يتم تغيير أو حذف القاعدة." }
        Push-Location $Backend
        try { & $py manage.py encrypt_backup_file --input $plain --output $enc; if ($LASTEXITCODE -ne 0) { throw "فشل تشفير النسخة والتحقق منها." } } finally { Pop-Location }
        if (-not (Test-Path $enc) -or (Get-Item $enc).Length -lt 1024) { throw "ملف النسخة المشفرة غير سليم." }
        Write-Host "تم حفظ نسخة مشفرة والتحقق منها: $enc" -ForegroundColor Green
        return $enc
    } finally {
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
        if ($operationTemp -and (Test-Path -LiteralPath $operationTemp -PathType Container)) {
            Remove-Item -LiteralPath $operationTemp -Recurse -Force -ErrorAction Stop
        }
    }
}
function Update-Db {
    Assert-AppStopped
    $c = Read-Config
    Write-Host "سيتم تحديث الجداول مع الاحتفاظ بالحركات والبيانات." -ForegroundColor Cyan
    $null = Save-VerifiedBackup $c.POSTGRES_DB
    Push-Location $Backend
    try {
        & $Python manage.py migrate --noinput
        if ($LASTEXITCODE -ne 0) { throw "فشل تحديث الجداول. لا تحذف القاعدة؛ راجع رسالة الخطأ." }
        & $Python manage.py seed_reference_data
        if ($LASTEXITCODE -ne 0) { throw "تم تحديث الجداول لكن فشل تجهيز العملات المرجعية." }
        & $Python manage.py check
        if ($LASTEXITCODE -ne 0) { throw "فحص التطبيق فشل بعد التحديث." }
    } finally { Pop-Location }
    Write-Host "اكتمل التحديث والفحص دون حذف البيانات." -ForegroundColor Green
}
function Show-DbStatus {
    $c = Read-Config
    $v = Invoke-Psql "SELECT json_build_object('database',current_database(),'size',pg_size_pretty(pg_database_size(current_database())),'connections',(SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()))::text;" $c.POSTGRES_DB
    ($v | ConvertFrom-Json) | Format-List
}
function Create-Db {
    Assert-AppStopped
    $c = Read-Config; $name = (Read-Host "اسم قاعدة البيانات الجديدة").Trim()
    if ($name -notmatch '^[A-Za-z][A-Za-z0-9_]{2,62}$') { throw "اسم غير صالح: استخدم حروفًا إنجليزية وأرقامًا و _." }
    $exists = Invoke-Psql "SELECT 1 FROM pg_database WHERE datname = '$($name.Replace("'","''"))';"
    if ($exists) { throw "قاعدة بهذا الاسم موجودة." }
    $null = Invoke-Psql ("CREATE DATABASE " + (Quote-Name $name) + " OWNER " + (Quote-Name $c.POSTGRES_USER) + " ENCODING 'UTF8' TEMPLATE template0;")
    Write-Host "تم إنشاء قاعدة فارغة. القاعدة الحالية لم تتغير." -ForegroundColor Green
    $go = Read-Host "هل تريد تحويل البرنامج إليها وتجهيز الجداول؟ اكتب نعم"
    if ($go -ceq "نعم") {
        $null = Save-VerifiedBackup $c.POSTGRES_DB
        Save-ConfigValue "POSTGRES_DB" $name
        try {
            Push-Location $Backend
            try { & $Python manage.py migrate --noinput; if ($LASTEXITCODE -ne 0) { throw "فشل الترحيل." }; & $Python manage.py seed_reference_data; if ($LASTEXITCODE -ne 0) { throw "فشل تجهيز العملات." } } finally { Pop-Location }
        } catch { Save-ConfigValue "POSTGRES_DB" $c.POSTGRES_DB; throw "أُعيد اتصال البرنامج للقاعدة القديمة. القاعدة الجديدة تُركت منفصلة. $($_.Exception.Message)" }
        Write-Host "البرنامج متصل الآن بالقاعدة الجديدة. القاعدة القديمة محفوظة ولم تُحذف." -ForegroundColor Green
    }
}
function Get-DatabaseNames {
    $raw = Invoke-Psql "SELECT datname FROM pg_database WHERE NOT datistemplate AND datallowconn AND datname <> 'postgres' ORDER BY datname;"
    return @($raw -split "\r?\n" | Where-Object { $_ })
}
function Show-DatabaseList {
    $config = Read-Config
    $names = @(Get-DatabaseNames)
    Write-Host ""
    Write-Host "قواعد البيانات الموجودة على خادم PostgreSQL:" -ForegroundColor Cyan
    if ($names.Count -eq 0) { Write-Host "لا توجد قواعد بيانات للتطبيق."; return }
    foreach ($name in $names) {
        $marker = if ($name -eq $config.POSTGRES_DB) { "  <== قاعدة Admrail النشطة" } else { "" }
        $escaped = $name.Replace("'","''")
        $size = Invoke-Psql "SELECT pg_size_pretty(pg_database_size('$escaped'));"
        Write-Host (" - {0}   ({1}){2}" -f $name,$size,$marker)
    }
}
function Connect-ExistingDatabase {
    Assert-AppStopped
    $config = Read-Config
    $names = @(Get-DatabaseNames)
    if ($names.Count -eq 0) { throw "لم يتم اكتشاف قواعد بيانات على خادم PostgreSQL الحالي." }
    $rows = @()
    foreach ($dbName in $names) {
        $safe = $dbName.Replace("'","''")
        $json = Invoke-Psql "SELECT json_build_object('name',datname,'owner',pg_get_userbyid(datdba),'encoding',pg_encoding_to_char(encoding),'size',pg_size_pretty(pg_database_size(datname)))::text FROM pg_database WHERE datname='$safe';"
        if ($json) { $rows += ($json | ConvertFrom-Json) }
    }
    Write-Host "قواعد PostgreSQL المكتشفة:" -ForegroundColor Cyan
    for ($i = 0; $i -lt $rows.Count; $i++) {
        $r = $rows[$i]
        $active = if ($r.name -eq $config.POSTGRES_DB) { " [النشطة حاليًا]" } else { "" }
        Write-Host ("[{0}] {1} | المالك: {2} | الترميز: {3} | الحجم: {4}{5}" -f ($i + 1),$r.name,$r.owner,$r.encoding,$r.size,$active)
    }
    Write-Host "[M] إدخال اسم يدويًا"
    $pick = (Read-Host "اختر رقم القاعدة أو M (Enter للإلغاء)").Trim()
    if (-not $pick) { return }
    if ($pick -match "^[Mm]$") {
        $target = (Read-Host "اسم قاعدة البيانات الموجودة").Trim()
    } elseif ($pick -match "^\d+$" -and [int]$pick -ge 1 -and [int]$pick -le $rows.Count) {
        $target = [string]$rows[[int]$pick - 1].name
    } else { throw "اختيار قاعدة البيانات غير صحيح." }
    if ($target -notmatch "^[A-Za-z0-9_][A-Za-z0-9_$-]{0,62}$") { throw "اسم قاعدة البيانات غير صالح." }
    $safeTarget = $target.Replace("'","''")
    $exists = Invoke-Psql "SELECT 1 FROM pg_database WHERE datname='$safeTarget' AND datallowconn;"
    if (-not $exists) { throw "قاعدة البيانات [$target] غير موجودة أو لا تسمح بالاتصال." }
    if ($target -eq $config.POSTGRES_DB) { Write-Host "هذه هي القاعدة النشطة بالفعل." -ForegroundColor Green; return }
    $schemaText = Invoke-Psql "SELECT json_build_object('has_migrations',to_regclass('public.django_migrations') IS NOT NULL,'table_count',(SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'))::text;" $target
    $schema = $schemaText | ConvertFrom-Json
    if (-not $schema.has_migrations -and [int]$schema.table_count -gt 0) { throw "القاعدة تحتوي جداول ولا تحتوي سجل Django migrations؛ قد تكون من نظام أو إصدار قديم. لم يتم تعديلها." }
    $backup = Save-VerifiedBackup $target
    try {
        Save-ConfigValue "POSTGRES_DB" $target
        Push-Location $Backend
        try {
            $plan = & $Python manage.py migrate --plan 2>&1
            $code = $LASTEXITCODE
            $plan | ForEach-Object { Write-Host $_ }
            if ($code -ne 0) { throw "فشل فحص خطة الترحيلات؛ لم تُنفذ ترحيلات." }
            & $Python manage.py check
            if ($LASTEXITCODE -ne 0) { throw "فشل فحص التطبيق مع القاعدة المختارة." }
        } finally { Pop-Location }
        Write-Host "النسخة الاحتياطية المشفرة والتحقق منها: $backup" -ForegroundColor Green
        if ((Read-Host "لتنفيذ خطة الترحيلات وربط البرنامج، اكتب APPLY") -cne "APPLY") {
            Save-ConfigValue "POSTGRES_DB" $config.POSTGRES_DB
            Write-Host "تم الإلغاء. اتصال البرنامج عاد للقاعدة السابقة ولم تُنفذ ترحيلات." -ForegroundColor Cyan
            return
        }
        Push-Location $Backend
        try {
            & $Python manage.py migrate --noinput
            if ($LASTEXITCODE -ne 0) { throw "فشل تنفيذ الترحيلات؛ النسخة الاحتياطية محفوظة." }
            & $Python manage.py check
            if ($LASTEXITCODE -ne 0) { throw "فشل الفحص النهائي بعد الترحيل." }
        } finally { Pop-Location }
        Write-Host "تم ربط Admrail بقاعدة [$target] بنجاح. لم يتم حذف أي قاعدة." -ForegroundColor Green
    } catch {
        Save-ConfigValue "POSTGRES_DB" $config.POSTGRES_DB
        throw "لم يكتمل الربط. أُعيد الاتصال إلى [$($config.POSTGRES_DB)]. النسخة الاحتياطية: $backup. التفاصيل: $($_.Exception.Message)"
    }
}
function Install-FreshDatabase {
    Assert-AppStopped
    $config = Read-Config
    $names = @(Get-DatabaseNames)
    if ($names.Count -eq 0) { throw "لم يتم العثور على قواعد بيانات يمكن إدارتها." }
    Write-Host "اختر القاعدة القديمة التي تريد استبدالها بعد تجهيز قاعدة جديدة:" -ForegroundColor Cyan
    for ($i = 0; $i -lt $names.Count; $i++) {
        $marker = if ($names[$i] -eq $config.POSTGRES_DB) { "  <== النشطة حاليًا" } else { "" }
        Write-Host ("[{0}] {1}{2}" -f ($i + 1),$names[$i],$marker)
    }
    $pick = (Read-Host "رقم القاعدة القديمة (Enter للإلغاء)").Trim()
    if (-not $pick) { return }
    if ($pick -notmatch '^\d+$' -or [int]$pick -lt 1 -or [int]$pick -gt $names.Count) {
        throw "اختيار قاعدة البيانات غير صحيح."
    }
    $oldDb = $names[[int]$pick - 1]
    $newDb = (Read-Host "اكتب اسم قاعدة البيانات الجديدة").Trim()
    if ($newDb -notmatch '^[A-Za-z][A-Za-z0-9_]{2,62}$' -or $newDb -eq $oldDb) {
        throw "اسم القاعدة الجديدة غير صالح أو مطابق لاسم القاعدة القديمة."
    }
    $escapedNew = $newDb.Replace("'","''")
    $exists = Invoke-Psql "SELECT 1 FROM pg_database WHERE datname = '$escapedNew';"
    if ($exists) { throw "قاعدة البيانات الجديدة موجودة بالفعل؛ اختر اسمًا مختلفًا." }

    Write-Host "سيتم حفظ نسخة مشفرة من [$oldDb] ثم إنشاء [$newDb] وتجهيزها." -ForegroundColor Yellow
    Write-Host "لن تُحذف القاعدة القديمة إلا بعد نجاح التجهيز وتأكيدك مرتين." -ForegroundColor Yellow
    $backup = Save-VerifiedBackup $oldDb
    $null = Invoke-Psql ("CREATE DATABASE " + (Quote-Name $newDb) + " OWNER " + (Quote-Name $config.POSTGRES_USER) + " ENCODING 'UTF8' TEMPLATE template0;")
    try {
        Save-ConfigValue "POSTGRES_DB" $newDb
        Push-Location $Backend
        try {
            & $Python manage.py migrate --noinput
            if ($LASTEXITCODE -ne 0) { throw "فشل إنشاء جداول قاعدة البيانات الجديدة." }
            & $Python manage.py seed_reference_data
            if ($LASTEXITCODE -ne 0) { throw "فشل تجهيز العملات المرجعية." }
            & $Python manage.py check
            if ($LASTEXITCODE -ne 0) { throw "فشل فحص التطبيق بعد تجهيز القاعدة الجديدة." }
        } finally { Pop-Location }
    } catch {
        Save-ConfigValue "POSTGRES_DB" $config.POSTGRES_DB
        throw "لم يكتمل تجهيز القاعدة الجديدة. أُعيد اتصال البرنامج إلى [$($config.POSTGRES_DB)]، ولم تُحذف [$oldDb]. التفاصيل: $($_.Exception.Message)"
    }

    Write-Host "نجح تجهيز [$newDb] وأصبح البرنامج متصلًا بها." -ForegroundColor Green
    Write-Host "النسخة الاحتياطية المشفرة: $backup" -ForegroundColor Green
    Write-Host "القاعدة القديمة [$oldDb] ما زالت موجودة." -ForegroundColor Yellow
    if ((Read-Host "لحذف القاعدة القديمة بعد نجاح الجديدة، اكتب نعم") -cne "نعم") {
        Write-Host "تم الاحتفاظ بالقاعدة القديمة كما هي." -ForegroundColor Cyan
        return
    }
    if ((Read-Host "تأكيد نهائي: اكتب اسم القاعدة حرفيًا [$oldDb]") -cne $oldDb) {
        Write-Host "تم إلغاء الحذف؛ القاعدة القديمة محفوظة." -ForegroundColor Cyan
        return
    }
    if ((Read-Config).POSTGRES_DB -eq $oldDb) { throw "رفض الحذف لأن القاعدة المختارة ما زالت نشطة." }
    $escapedOld = $oldDb.Replace("'","''")
    $null = Invoke-Psql "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$escapedOld' AND pid <> pg_backend_pid();"
    $null = Invoke-Psql ("DROP DATABASE " + (Quote-Name $oldDb) + " WITH (FORCE);")
    Write-Host "تم حذف [$oldDb] بعد نجاح القاعدة الجديدة وحفظ النسخة المشفرة." -ForegroundColor Green
}
function Rename-Db {
    Assert-AppStopped
    $c = Read-Config
    $names = @(Get-DatabaseNames)
    if ($names.Count -eq 0) { throw "لا توجد قواعد بيانات يمكن إعادة تسميتها." }
    Write-Host "اختر قاعدة البيانات التي تريد تغيير اسمها:" -ForegroundColor Cyan
    for ($i=0; $i -lt $names.Count; $i++) {
        $marker = if ($names[$i] -eq $c.POSTGRES_DB) { "  <== قاعدة Admrail النشطة" } else { "" }
        Write-Host ("[{0}] {1}{2}" -f ($i+1),$names[$i],$marker)
    }
    $pick = (Read-Host "رقم القاعدة (Enter للإلغاء)").Trim()
    if (-not $pick) { return }
    if ($pick -notmatch '^\d+$' -or [int]$pick -lt 1 -or [int]$pick -gt $names.Count) { throw "اختيار قاعدة البيانات غير صحيح." }
    $old = [string]$names[[int]$pick-1]
    $new = (Read-Host "الاسم الجديد للقاعدة [$old]").Trim()
    if ($new -notmatch '^[A-Za-z][A-Za-z0-9_]{2,62}$' -or $new -eq $old) { throw "الاسم الجديد غير صالح أو مطابق للحالي." }
    $exists = Invoke-Psql "SELECT 1 FROM pg_database WHERE datname = '$($new.Replace("'","''"))';"
    if ($exists) { throw "الاسم الجديد مستخدم بالفعل." }
    $null = Save-VerifiedBackup $old
    $null = Invoke-Psql "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$($old.Replace("'","''"))' AND pid <> pg_backend_pid();"
    $null = Invoke-Psql ("ALTER DATABASE " + (Quote-Name $old) + " RENAME TO " + (Quote-Name $new) + ";")
    if ($old -eq $c.POSTGRES_DB) {
        try { Save-ConfigValue "POSTGRES_DB" $new }
        catch {
            $null = Invoke-Psql ("ALTER DATABASE " + (Quote-Name $new) + " RENAME TO " + (Quote-Name $old) + ";")
            throw "فشل تحديث ملف الاتصال وتم التراجع عن تغيير الاسم."
        }
        Write-Host "تم تغيير اسم قاعدة Admrail النشطة وتحديث اتصال البرنامج دون تغيير البيانات." -ForegroundColor Green
    } else {
        Write-Host "تم تغيير اسم القاعدة المحددة. لم يتغير اتصال Admrail لأنه متصل بقاعدة أخرى." -ForegroundColor Green
    }
}
function Delete-Db {
    Assert-AppStopped
    $c = Read-Config
    $raw = Invoke-Psql "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY datname;"
    $list = @($raw -split "\r?\n" | Where-Object { $_ })
    for ($i=0; $i -lt $list.Count; $i++) { Write-Host "[$($i+1)] $($list[$i])" }
    $n = Read-Host "رقم القاعدة القديمة المراد حذفها (Enter للإلغاء)"
    if (-not $n) { return }
    if ($n -notmatch '^\d+$' -or [int]$n -lt 1 -or [int]$n -gt $list.Count) { throw "اختيار غير صحيح." }
    $target = $list[[int]$n-1]
    if ($target -eq $c.POSTGRES_DB) { throw "هذه قاعدة البرنامج النشطة. لن نحذفها. غيّر الاتصال أولًا." }
    $null = Save-VerifiedBackup $target
    Write-Host "تحذير: حذف $target يمسح كل بياناتها نهائيًا." -ForegroundColor Red
    if ((Read-Host "اكتب اسم القاعدة حرفيًا لتأكيد الحذف: $target") -cne $target) { Write-Host "تم الإلغاء؛ لم يُحذف شيء."; return }
    $null = Invoke-Psql "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$($target.Replace("'","''"))' AND pid <> pg_backend_pid();"
    $null = Invoke-Psql ("DROP DATABASE " + (Quote-Name $target) + " WITH (FORCE);")
    Write-Host "تم حذف القاعدة بعد حفظ النسخة المشفرة." -ForegroundColor Green
}
function Change-DbPassword {
    Assert-AppStopped
    $c = Read-Config; $role = $c.POSTGRES_USER; $new = New-DbPassword
    $sqlFile = Join-Path $env:TEMP ("admrail-secret-" + [guid]::NewGuid().ToString("N") + ".sql")
    try {
        $sql = "ALTER ROLE " + (Quote-Name $role) + " PASSWORD '" + $new.Replace("'","''") + "';"
        [IO.File]::WriteAllText($sqlFile,$sql,(New-Object System.Text.UTF8Encoding($false)))
        $who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        & icacls.exe $sqlFile /inheritance:r /grant:r "$($who):F" "SYSTEM:F" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "تعذر حماية ملف كلمة المرور المؤقت." }
        $psql = Get-PgTool "psql"; $env:PGPASSWORD = $script:AdminPassword
        & $psql -X -q -v ON_ERROR_STOP=1 -h $c.POSTGRES_HOST -p $c.POSTGRES_PORT -U postgres -d postgres -f $sqlFile
        if ($LASTEXITCODE -ne 0) { throw "فشل تغيير كلمة مرور مستخدم التطبيق." }
        Save-ConfigValue "POSTGRES_PASSWORD" $new
        Write-Host "تم تغيير كلمة مرور مستخدم قاعدة البيانات وتحديث ملف الاتصال." -ForegroundColor Green
        Write-Host "احتفظ بملف backend\.env ولا تشاركه." -ForegroundColor Yellow
    } finally {
        if (Test-Path $sqlFile) { Remove-Item $sqlFile -Force -ErrorAction SilentlyContinue }
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
        $new = $null
    }
}
function Reset-AppPassword {
    Assert-AppStopped
    $u = Read-Host "اسم مستخدم Admrail"
    if (-not $u) { throw "اسم المستخدم مطلوب." }
    Push-Location $Backend
    try { & $Python manage.py changepassword $u; if ($LASTEXITCODE -ne 0) { throw "لم يتم تغيير كلمة مرور مستخدم البرنامج." } } finally { Pop-Location }
}
function Get-LoginRoles {
    $config = Read-Config
    $appRole = $config.POSTGRES_USER.Replace("'","''")
    $raw = Invoke-Psql "SELECT rolname FROM pg_roles WHERE NOT rolsuper AND NOT rolreplication AND NOT rolbypassrls AND NOT rolcreatedb AND NOT rolcreaterole AND rolname !~ '^pg_' AND rolname <> '$appRole' ORDER BY rolname;"
    return @($raw -split "\r?\n" | Where-Object { $_ })
}
function Show-LoginRoles {
    $sql = "SELECT rolname || ' | ' || CASE WHEN rolsuper OR rolreplication OR rolbypassrls OR rolcreatedb OR rolcreaterole THEN 'صلاحيات مرتفعة' ELSE 'مستخدم عادي' END || ' | ' || CASE WHEN rolcanlogin THEN 'دخول مفعّل' ELSE 'دخول موقوف' END FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY rolname;"
    $raw = Invoke-Psql $sql
    Write-Host "حسابات PostgreSQL وصلاحياتها:" -ForegroundColor Cyan
    if (-not $raw) { Write-Host "لا توجد حسابات."; return }
    $raw -split "\r?\n" | ForEach-Object { Write-Host (" - " + $_) }
}
function Invoke-RoleSqlFile([string]$Sql) {
    $file = Join-Path $env:TEMP ("admrail-role-" + [guid]::NewGuid().ToString("N") + ".sql")
    try {
        [IO.File]::WriteAllText($file,$Sql,(New-Object System.Text.UTF8Encoding($false)))
        $who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        & icacls.exe $file /inheritance:r /grant:r "$($who):F" "SYSTEM:F" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "تعذر حماية ملف العملية المؤقت." }
        $c = Read-Config; $psql = Get-PgTool "psql"; $env:PGPASSWORD = $script:AdminPassword
        & $psql -X -q -v ON_ERROR_STOP=1 -h $c.POSTGRES_HOST -p $c.POSTGRES_PORT -U postgres -d postgres -f $file
        if ($LASTEXITCODE -ne 0) { throw "رفض PostgreSQL العملية؛ لم يتم اعتبارها ناجحة." }
    } finally {
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    }
}
function Read-RolePassword([string]$Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}
function Create-LoginRole {
    $name = (Read-Host "اسم المستخدم الجديد").Trim()
    if ($name -notmatch '^[A-Za-z][A-Za-z0-9_]{2,62}$' -or $name -match '^pg_') { throw "اسم المستخدم غير صالح." }
    $exists = Invoke-Psql "SELECT 1 FROM pg_roles WHERE rolname='$($name.Replace("'","''"))';"
    if ($exists) { throw "اسم المستخدم موجود بالفعل." }
    $password = Read-RolePassword "كلمة مرور المستخدم الجديد"
    if ($password.Length -lt 12) { $password = $null; throw "كلمة المرور يجب ألا تقل عن 12 حرفًا." }
    $confirm = Read-RolePassword "أعد كتابة كلمة المرور"
    if ($password -cne $confirm) { $password = $null; $confirm = $null; throw "كلمتا المرور غير متطابقتين." }
    $quoted = Quote-Name $name
    $escapedPassword = $password.Replace("'","''")
    try { Invoke-RoleSqlFile ("CREATE ROLE " + $quoted + " LOGIN PASSWORD '" + $escapedPassword + "';") }
    finally { $password = $null; $confirm = $null; $escapedPassword = $null }
    Write-Host "تم إنشاء حساب دخول PostgreSQL. لم تُمنح له صلاحيات إدارية أو صلاحيات قواعد بيانات تلقائيًا." -ForegroundColor Green
}
function Select-LoginRole {
    $roles = @(Get-LoginRoles)
    if ($roles.Count -eq 0) { throw "لا توجد حسابات مستخدمين عادية للاختيار." }
    for ($i=0; $i -lt $roles.Count; $i++) { Write-Host ("[{0}] {1}" -f ($i+1),$roles[$i]) }
    $choice = (Read-Host "اختر رقم المستخدم (Enter للإلغاء)").Trim()
    if (-not $choice) { return $null }
    if ($choice -notmatch '^\d+$' -or [int]$choice -lt 1 -or [int]$choice -gt $roles.Count) { throw "اختيار غير صحيح." }
    return [string]$roles[[int]$choice-1]
}
function Update-LoginRole {
    $name = Select-LoginRole
    if (-not $name) { return }
    Write-Host "1) تغيير كلمة المرور"
    Write-Host "2) إيقاف تسجيل الدخول"
    Write-Host "3) تفعيل تسجيل الدخول"
    $action = Read-Host "اختر الإجراء"
    $quoted = Quote-Name $name
    $config = Read-Config
    if ($action -eq "2" -and $name -eq $config.POSTGRES_USER) {
        throw "لا يمكن إيقاف دخول مستخدم اتصال Admrail الحالي؛ سيؤدي ذلك إلى توقف البرنامج."
    }
    switch ($action) {
        "1" {
            $password = Read-RolePassword "كلمة المرور الجديدة"
            if ($password.Length -lt 12) { $password = $null; throw "كلمة المرور يجب ألا تقل عن 12 حرفًا." }
            $escaped = $password.Replace("'","''")
            try { Invoke-RoleSqlFile ("ALTER ROLE " + $quoted + " PASSWORD '" + $escaped + "';") }
            finally { $password = $null; $escaped = $null }
        }
        "2" { Invoke-RoleSqlFile ("ALTER ROLE " + $quoted + " NOLOGIN;") }
        "3" { Invoke-RoleSqlFile ("ALTER ROLE " + $quoted + " LOGIN;") }
        default { throw "اختيار غير صحيح." }
    }
    Write-Host "تم تنفيذ التعديل." -ForegroundColor Green
}
function Remove-LoginRole {
    $name = Select-LoginRole
    if (-not $name) { return }
    $escaped = $name.Replace("'","''")
    $info = (Invoke-Psql "SELECT json_build_object('superuser',rolsuper,'replication',rolreplication,'bypassrls',rolbypassrls,'createdb',rolcreatedb,'createrole',rolcreaterole)::text FROM pg_roles WHERE rolname='$escaped';") | ConvertFrom-Json
    if (-not $info -or $info.superuser -or $info.replication -or $info.bypassrls -or $info.createdb -or $info.createrole) { throw "رفض الحذف: هذا الحساب يمتلك صلاحيات مرتفعة." }
    $config = Read-Config
    if ($name -eq $config.POSTGRES_USER) { throw "رفض الحذف: هذا هو مستخدم اتصال Admrail الحالي." }
    $owned = [int](Invoke-Psql "SELECT count(*) FROM pg_database WHERE datdba=(SELECT oid FROM pg_roles WHERE rolname='$escaped');")
    if ($owned -gt 0) { throw "رفض الحذف: المستخدم يمتلك قاعدة بيانات. انقل الملكية يدويًا بعد مراجعة المالك." }
    Write-Host "سيحاول PostgreSQL حذف الحساب فقط. إذا كانت له صلاحيات أو كائنات مرتبطة فسيرفض الأمر ولن تُحذف تلك الكائنات." -ForegroundColor Yellow
    if ((Read-Host "اكتب DELETE ROLE $name للتأكيد") -cne "DELETE ROLE $name") { Write-Host "تم الإلغاء."; return }
    Invoke-RoleSqlFile ("DROP ROLE " + (Quote-Name $name) + ";")
    Write-Host "تم حذف حساب الدخول." -ForegroundColor Green
}
function Manage-LoginRoles {
    while ($true) {
        Clear-Host
        Write-Host "إدارة مستخدمي PostgreSQL" -ForegroundColor Cyan
        Write-Host "1) عرض المستخدمين"
        Write-Host "2) إنشاء مستخدم جديد"
        Write-Host "3) تعديل مستخدم (كلمة المرور / تفعيل الدخول / إيقافه)"
        Write-Host "4) حذف مستخدم غير مستخدم"
        Write-Host "0) رجوع"
        $choice = Read-Host "اختيارك"
        try {
            switch ($choice) {
                "1" { Show-LoginRoles }
                "2" { Create-LoginRole }
                "3" { Update-LoginRole }
                "4" { Remove-LoginRole }
                "0" { return }
                default { Write-Host "اختيار غير صحيح." }
            }
        } catch { Write-Host ("لم تكتمل العملية: " + $_.Exception.Message) -ForegroundColor Red }
        if ($choice -ne "0") { [void](Read-Host "اضغط Enter للمتابعة") }
    }
}
try {
    $services = @(Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq "Running" })
    if ($services.Count -eq 0) { throw "لم يتم العثور على خدمة PostgreSQL قيد التشغيل." }
    if (-not (Test-Path $Python)) { throw "Python المرفق غير موجود." }
    Read-AdminPassword
    while ($true) {
        Clear-Host
        Write-Host "============================================" -ForegroundColor Cyan
        Write-Host "     Admrail ERP v2 - إدارة قاعدة البيانات" -ForegroundColor Cyan
        Write-Host "============================================" -ForegroundColor Cyan
        Write-Host "1 - فحص الاتصال وحالة القاعدة"
        Write-Host "2 - تحديث القاعدة الحالية مع الحفاظ على البيانات"
        Write-Host "3 - إنشاء قاعدة بيانات جديدة"
        Write-Host "4 - تغيير اسم قاعدة بيانات محددة"
        Write-Host "5 - حذف قاعدة قديمة غير نشطة بعد نسخة مشفرة"
        Write-Host "6 - تغيير كلمة مرور مستخدم PostgreSQL الخاص بالبرنامج"
        Write-Host "7 - تغيير كلمة مرور مستخدم Admrail"
        Write-Host "8 - استعادة نسخة إلى قاعدة منفصلة"
        Write-Host "9 - عرض قواعد البيانات وتحديد القاعدة النشطة"
        Write-Host "10 - تجهيز قاعدة جديدة واختيار قاعدة قديمة لحذفها بعد النجاح"
        Write-Host "11 - إدارة مستخدمي PostgreSQL"
        Write-Host "12 - اكتشاف وربط قاعدة بيانات موجودة تلقائيًا"
        Write-Host "0 - خروج"
        Write-Host "ملاحظة: استعادة كلمة مرور postgres المنسية تتطلب إجراء استرداد مسؤول Windows؛ الأداة لا تتجاوز حماية PostgreSQL." -ForegroundColor Yellow
        $choice = Read-Host "اختر رقم العملية"
        try {
            switch ($choice) {
                "1" { Show-DbStatus }
                "2" { Update-Db }
                "3" { Create-Db }
                "4" { Rename-Db }
                "5" { Delete-Db }
                "6" { Change-DbPassword }
                "7" { Reset-AppPassword }
                "8" {
                    $file = Read-Host "المسار الكامل لملف النسخة المشفرة"
                    $target = Read-Host "اسم قاعدة الاستعادة الجديدة (اتركه فارغًا للاسم التلقائي)"
                    $restore = Join-Path $PSScriptRoot "restore-postgres-to-new-db.ps1"
                    if ($target) { & $restore -BackupFile $file -TargetDatabase $target } else { & $restore -BackupFile $file }
                }
                "9" { Show-DatabaseList }
                "10" { Install-FreshDatabase }
                "11" { Manage-LoginRoles }
                "12" { Connect-ExistingDatabase }
                "0" { break }
                default { Write-Host "اختيار غير صحيح." }
            }
            if ($choice -eq "0") { break }
        } catch { Write-Host "لم تكتمل العملية: $($_.Exception.Message)" -ForegroundColor Red }
        [void](Read-Host "اضغط Enter للعودة للقائمة")
    }
} catch {
    Write-Host "توقفت الأداة: $($_.Exception.Message)" -ForegroundColor Red
    [void](Read-Host "اضغط Enter للإغلاق")
    exit 1
} finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue; $script:AdminPassword = $null }
