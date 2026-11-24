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
    $plain = Join-Path $env:TEMP "admrail-$tag.dump"
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
        if (Test-Path $plain) { Remove-Item $plain -Force -ErrorAction SilentlyContinue }
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
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
function Rename-Db {
    Assert-AppStopped
    $c = Read-Config; $old = $c.POSTGRES_DB; $new = (Read-Host "الاسم الجديد للقاعدة").Trim()
    if ($new -notmatch '^[A-Za-z][A-Za-z0-9_]{2,62}$' -or $new -eq $old) { throw "الاسم الجديد غير صالح أو مطابق للحالي." }
    $exists = Invoke-Psql "SELECT 1 FROM pg_database WHERE datname = '$($new.Replace("'","''"))';"
    if ($exists) { throw "الاسم الجديد مستخدم بالفعل." }
    $null = Save-VerifiedBackup $old
    $null = Invoke-Psql "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$($old.Replace("'","''"))' AND pid <> pg_backend_pid();"
    $null = Invoke-Psql ("ALTER DATABASE " + (Quote-Name $old) + " RENAME TO " + (Quote-Name $new) + ";")
    try { Save-ConfigValue "POSTGRES_DB" $new } catch { $null = Invoke-Psql ("ALTER DATABASE " + (Quote-Name $new) + " RENAME TO " + (Quote-Name $old) + ";"); throw "فشل تحديث ملف الاتصال وتم التراجع عن تغيير الاسم." }
    Write-Host "تم تغيير الاسم وتحديث اتصال البرنامج دون تغيير البيانات." -ForegroundColor Green
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
try {
    $svc = Get-Service -Name "postgresql-x64-16" -ErrorAction SilentlyContinue
    if (-not $svc -or $svc.Status -ne "Running") { throw "خدمة PostgreSQL 16 غير موجودة أو متوقفة." }
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
        Write-Host "4 - تغيير اسم القاعدة الحالية"
        Write-Host "5 - حذف قاعدة قديمة غير نشطة بعد نسخة مشفرة"
        Write-Host "6 - تغيير كلمة مرور مستخدم PostgreSQL الخاص بالبرنامج"
        Write-Host "7 - تغيير كلمة مرور مستخدم Admrail"
        Write-Host "8 - استعادة نسخة إلى قاعدة منفصلة"
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
