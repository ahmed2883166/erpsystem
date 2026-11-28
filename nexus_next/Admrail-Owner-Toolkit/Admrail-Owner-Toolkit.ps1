#requires -Version 5.1
[CmdletBinding()]
param([string]$AppRoot)

$ErrorActionPreference = "Stop"
$ToolkitRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogRoot = Join-Path $ToolkitRoot "logs"
New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
$LogFile = Join-Path $LogRoot ("owner-toolkit-" + (Get-Date -Format "yyyyMMdd") + ".log")

function Write-Log([string]$Message) {
    Add-Content -LiteralPath $LogFile -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message) -Encoding UTF8
}
function Find-AppRoot {
    $current = Get-Item -LiteralPath $ToolkitRoot
    while ($current) {
        $backend = Join-Path $current.FullName "backend"
        $manage = Join-Path $backend "manage.py"
        $python = Join-Path $current.FullName "runtime\python.exe"
        $venvPython = Join-Path $backend ".venv\Scripts\python.exe"
        if ((Test-Path -LiteralPath $manage -PathType Leaf) -and
            ((Test-Path -LiteralPath $python -PathType Leaf) -or (Test-Path -LiteralPath $venvPython -PathType Leaf))) {
            return $current.FullName
        }
        $current = $current.Parent
    }
    Write-Host "الأداة تعمل من وسيط مستقل. حدد مجلد Admrail الموجود على سيرفر العميل." -ForegroundColor Yellow
    $selected = (Read-Host "مسار مجلد Admrail الذي يحتوي على backend\manage.py").Trim().Trim('"')
    if (-not $selected -or -not (Test-Path -LiteralPath $selected -PathType Container)) {
        throw "لم يتم تحديد مجلد Admrail صالح."
    }
    $manage = Join-Path $selected "backend\manage.py"
    $python = Join-Path $selected "runtime\python.exe"
    $venvPython = Join-Path $selected "backend\.venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $manage -PathType Leaf) -or
        (-not (Test-Path -LiteralPath $python -PathType Leaf) -and -not (Test-Path -LiteralPath $venvPython -PathType Leaf))) {
        throw "المسار المحدد لا يحتوي على backend\manage.py وبيئة Python صالحة."
    }
    return (Resolve-Path -LiteralPath $selected).Path
}
function Get-AppPython {
    $candidate = Join-Path $script:AppRoot "runtime\python.exe"
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        $candidate = Join-Path $script:AppRoot "backend\.venv\Scripts\python.exe"
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "Python runtime غير موجود."
    }
    return $candidate
}
function Get-OwnerPinPath { Join-Path $ToolkitRoot ".owner-auth.json" }
function Get-PinHash([string]$Pin, [byte[]]$Salt) {
    $derive = New-Object -TypeName Security.Cryptography.Rfc2898DeriveBytes -ArgumentList $Pin, $Salt, 220000
    try { return [Convert]::ToBase64String($derive.GetBytes(32)) } finally { $derive.Dispose() }
}
function Test-OwnerAuthentication {
    $authPath = Get-OwnerPinPath
    if (-not (Test-Path -LiteralPath $authPath -PathType Leaf)) {
        Write-Host "أول تشغيل: أنشئ PIN خاصًا بمالك النظام (12 حرفًا/رقمًا على الأقل)." -ForegroundColor Yellow
        $secure = Read-Host "PIN جديد" -AsSecureString
        $ptr = [IntPtr]::Zero
        try {
            $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            $pin = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
            if ($pin.Length -lt 12) { throw "PIN يجب ألا يقل عن 12 حرفًا أو رقمًا." }
            $salt = New-Object byte[] 16
            $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
            try { $rng.GetBytes($salt) } finally { $rng.Dispose() }
            $record = [ordered]@{ version = 1; iterations = 220000; salt = [Convert]::ToBase64String($salt); hash = (Get-PinHash $pin $salt) }
            $record | ConvertTo-Json | Set-Content -LiteralPath $authPath -Encoding ASCII
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            & (Join-Path $env:SystemRoot "System32\icacls.exe") $authPath /inheritance:r /grant:r "$($identity):(R,W)" "SYSTEM:(R,W)" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "تعذر تقييد صلاحيات ملف PIN على حساب المالك وSYSTEM." }
            Write-Log "Owner PIN initialized."
        } finally {
            if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
            $pin = $null
        }
    }
    $record = Get-Content -LiteralPath $authPath -Raw | ConvertFrom-Json
    if ($record.version -ne 1 -or -not $record.salt -or -not $record.hash) { throw "ملف مصادقة المالك تالف." }
    $lockPath = Join-Path $ToolkitRoot ".owner-lockout.json"
    if (Test-Path -LiteralPath $lockPath -PathType Leaf) {
        $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
        if ($lock.locked_until -and ([datetime]::Parse($lock.locked_until) -gt (Get-Date))) {
            throw "تم إيقاف محاولات الدخول مؤقتًا حتى $($lock.locked_until)."
        }
    }
    $secure = Read-Host "أدخل PIN مالك النظام" -AsSecureString
    $ptr = [IntPtr]::Zero
    try {
        $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        $pin = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
        $actual = Get-PinHash $pin ([Convert]::FromBase64String($record.salt))
        $expectedBytes = [Convert]::FromBase64String($record.hash)
        $actualBytes = [Convert]::FromBase64String($actual)
        if ($actualBytes.Length -ne $expectedBytes.Length) { throw "PIN غير صحيح." }
        $diff = 0
        for ($i = 0; $i -lt $actualBytes.Length; $i++) { $diff = $diff -bor ($actualBytes[$i] -bxor $expectedBytes[$i]) }
        if ($diff -ne 0) {
            $attempts = 1
            if (Test-Path -LiteralPath $lockPath -PathType Leaf) {
                try { $prior = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json; $attempts = [int]$prior.failures + 1 } catch { $attempts = 1 }
            }
            $delayMinutes = [Math]::Min(60, [Math]::Pow(2, [Math]::Min(5, [Math]::Max(0, $attempts - 3))))
            $lock = [ordered]@{ failures = $attempts; locked_until = if ($attempts -ge 3) { (Get-Date).AddMinutes($delayMinutes).ToString("o") } else { $null } }
            $lock | ConvertTo-Json | Set-Content -LiteralPath $lockPath -Encoding ASCII
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            & (Join-Path $env:SystemRoot "System32\icacls.exe") $lockPath /inheritance:r /grant:r "$($identity):(R,W)" "SYSTEM:(R,W)" | Out-Null
            throw "PIN غير صحيح. عدد المحاولات الفاشلة: $attempts."
        }
        if (Test-Path -LiteralPath $lockPath) { Remove-Item -LiteralPath $lockPath -Force }
        Write-Log "Owner authenticated."
    } finally {
        if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
        $pin = $null
    }
}
function Get-WindowsPowerShellPath {
    $candidate = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { throw "Windows PowerShell executable was not found in System32." }
    return $candidate
}
function Get-PostgresTools {
    $roots = @()
    if ($env:ProgramFiles) { $roots += (Join-Path $env:ProgramFiles "PostgreSQL") }
    if ($env:ProgramW6432 -and $env:ProgramW6432 -ne $env:ProgramFiles) { $roots += (Join-Path $env:ProgramW6432 "PostgreSQL") }
    $found = @{}
    foreach ($root in $roots) {
        foreach ($version in @("18","17","16","15","14")) {
            $bin = Join-Path $root ($version + "\bin")
            foreach ($name in @("psql.exe","pg_dump.exe","pg_restore.exe")) {
                $path = Join-Path $bin $name
                if ((Test-Path -LiteralPath $path -PathType Leaf) -and -not $found.ContainsKey($name)) { $found[$name] = $path }
            }
        }
    }
    return $found
}
function Read-AppConfig {
    $envFile = Join-Path $script:AppRoot "backend\.env"
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { throw "ملف backend\.env غير موجود؛ لم يتم تفعيله بعد." }
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*(.*)$') {
            $value = $Matches[2].Trim()
            if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $values[$Matches[1]] = $value
        }
    }
    foreach ($key in @("POSTGRES_DB","POSTGRES_USER","POSTGRES_PASSWORD")) {
        if (-not $values[$key] -or $values[$key] -match '^replace-') { throw "إعداد $key غير مكتمل في backend\.env." }
    }
    return $values
}
function Show-Diagnostics {
    $python = $null
    try { $python = Get-AppPython } catch { }
    $pg = Get-PostgresTools
    $services = @(Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^postgresql-x64-' -or $_.DisplayName -match 'PostgreSQL' } | Sort-Object Name)
    $serviceSummary = if ($services.Count) { ($services | ForEach-Object { "$($_.Name)=$($_.Status)" }) -join "; " } else { "not detected" }
    $envFile = Join-Path $script:AppRoot "backend\.env"
    $lines = @(
        "Admrail Owner Toolkit diagnostics",
        "Date: $(Get-Date -Format o)",
        "Computer: $env:COMPUTERNAME",
        "Windows: $([Environment]::OSVersion.VersionString)",
        "Toolkit: $ToolkitRoot",
        "Application root: $script:AppRoot",
        "Backend present: $(Test-Path (Join-Path $script:AppRoot 'backend\manage.py'))",
        "Python runtime: $([bool]$python)",
        "Environment file present: $(Test-Path -LiteralPath $envFile)",
        "PostgreSQL services: $serviceSummary",
        "psql available: $($pg.ContainsKey('psql.exe'))",
        "pg_dump available: $($pg.ContainsKey('pg_dump.exe'))",
        "pg_restore available: $($pg.ContainsKey('pg_restore.exe'))",
        "Database actions: $(if (Test-Path -LiteralPath $envFile) { 'configuration file present; credentials not tested' } else { 'disabled until backend\.env is configured' })",
        "Recent toolkit log:",
        (Get-Content -LiteralPath $LogFile -Tail 15 -ErrorAction SilentlyContinue)
    )
    $report = Join-Path $LogRoot ("diagnostics-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".txt")
    $lines | Set-Content -LiteralPath $report -Encoding UTF8
    Write-Host ($lines -join [Environment]::NewLine)
    Write-Host "تم حفظ التقرير: $report" -ForegroundColor Green
    Write-Log "Diagnostics report created: $report"
}
function Invoke-Backup {
    $null = Read-AppConfig
    $scriptPath = Join-Path $script:AppRoot "scripts\backup-postgres.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw "ملف النسخ الاحتياطي غير موجود." }
    & (Get-WindowsPowerShellPath) -NoLogo -NoProfile -ExecutionPolicy Bypass -File $scriptPath
    if ($LASTEXITCODE -ne 0) { throw "فشل النسخ الاحتياطي. راجع سجل البرنامج." }
    Write-Log "Backup script completed."
}
function Invoke-Restore {
    $null = Read-AppConfig
    $file = Read-Host "المسار الكامل لملف النسخة المشفرة أو dump"
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "ملف النسخة غير موجود." }
    $target = Read-Host "اسم قاعدة الاستعادة الجديدة (لن يستبدل القاعدة الحالية)"
    if ($target -notmatch '^[A-Za-z][A-Za-z0-9_]{2,62}$') { throw "اسم قاعدة الاستعادة غير صالح." }
    $scriptPath = Join-Path $script:AppRoot "scripts\restore-postgres-to-new-db.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw "ملف الاستعادة غير موجود." }
    & (Get-WindowsPowerShellPath) -NoLogo -NoProfile -ExecutionPolicy Bypass -File $scriptPath -BackupFile $file -TargetDatabase $target
    if ($LASTEXITCODE -ne 0) { throw "فشلت الاستعادة إلى القاعدة المنفصلة." }
    Write-Log "Restore completed to separate database $target."
}
function Invoke-Migrations {
    $null = Read-AppConfig
    $python = Get-AppPython
    Write-Host "سيتم إنشاء نسخة احتياطية مشفرة أولًا؛ لن يتم حذف أي بيانات." -ForegroundColor Yellow
    Invoke-Backup
    Push-Location (Join-Path $script:AppRoot "backend")
    try {
        & $python manage.py check
        if ($LASTEXITCODE -ne 0) { throw "فشل Django system check." }
        & $python manage.py migrate --plan
        if ($LASTEXITCODE -ne 0) { throw "تعذر قراءة خطة الترحيلات." }
        $approval = Read-Host "لمتابعة تطبيق الترحيلات اكتب APPLY"
        if ($approval -cne "APPLY") { Write-Host "تم الإلغاء دون تعديل قاعدة البيانات."; return }
        & $python manage.py migrate --noinput
        if ($LASTEXITCODE -ne 0) { throw "فشل تطبيق الترحيلات. لا تحذف القاعدة؛ راجع الخطأ." }
    } finally { Pop-Location }
    Write-Log "Django migrations applied after verified backup script."
}
function Show-Logs {
    Get-ChildItem -LiteralPath $LogRoot -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 20 Name,Length,LastWriteTime |
        Format-Table -AutoSize
    $appLogs = Join-Path $script:AppRoot "logs"
    if (Test-Path -LiteralPath $appLogs -PathType Container) {
        Write-Host "Application log files:"
        Get-ChildItem -LiteralPath $appLogs -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 20 Name,Length,LastWriteTime |
            Format-Table -AutoSize
    }
}
function Invoke-DatabaseManager {
    $manager = Join-Path $script:AppRoot "scripts\Admrail-Database-Manager.ps1"
    if (-not (Test-Path -LiteralPath $manager -PathType Leaf)) {
        throw "أداة إدارة قواعد البيانات غير موجودة داخل scripts."
    }
    Write-Log "Owner opened PostgreSQL database and role manager."
    & (Get-WindowsPowerShellPath) -NoLogo -NoProfile -ExecutionPolicy Bypass -File $manager
    if ($LASTEXITCODE -ne 0) {
        throw "توقفت أداة إدارة قواعد البيانات بخطأ. راجع الرسالة الظاهرة."
    }
    Write-Log "Owner database manager closed."
}
try {
    if ($AppRoot) {
        $resolved = (Resolve-Path -LiteralPath $AppRoot -ErrorAction Stop).Path
        $manage = Join-Path $resolved "backend\manage.py"
        $pythonA = Join-Path $resolved "runtime\python.exe"
        $pythonB = Join-Path $resolved "backend\.venv\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $manage -PathType Leaf) -or
            (-not (Test-Path -LiteralPath $pythonA -PathType Leaf) -and -not (Test-Path -LiteralPath $pythonB -PathType Leaf))) {
            throw "المسار المحدد في AppRoot غير صالح."
        }
        $script:AppRoot = $resolved
    } else { $script:AppRoot = Find-AppRoot }
    Test-OwnerAuthentication
    Write-Log "Toolkit started. AppRoot=$script:AppRoot"
    $script:ExitRequested = $false
    while (-not $script:ExitRequested) {
        Clear-Host
        Write-Host "ADMRAIL OWNER TOOLKIT - Vendor maintenance only" -ForegroundColor Cyan
        Write-Host "Application: $script:AppRoot"
        Write-Host ""
        Write-Host "1) Diagnostics (does not require backend\.env)"
        Write-Host "2) Create encrypted backup"
        Write-Host "3) Restore backup to a NEW database"
        Write-Host "4) Apply Django migrations (backup + explicit confirmation)"
        Write-Host "5) View toolkit/application logs"
        Write-Host "6) إدارة قواعد البيانات ومستخدمي PostgreSQL (مالك البرنامج فقط)"
        Write-Host "0) Exit"
        $choice = Read-Host "اختيارك"
        try {
            switch ($choice) {
                "1" { Show-Diagnostics }
                "2" { Invoke-Backup }
                "3" { Invoke-Restore }
                "4" { Invoke-Migrations }
                "5" { Show-Logs }
                "6" { Invoke-DatabaseManager }
                "0" { $script:ExitRequested = $true }
                default { Write-Host "اختيار غير صحيح." -ForegroundColor Yellow }
            }
        } catch {
            Write-Host ("ERROR: " + $_.Exception.Message) -ForegroundColor Red
            Write-Log ("ERROR: " + $_.Exception.Message)
        }
        if ($choice -ne "0") { Read-Host "اضغط Enter للمتابعة" | Out-Null }
    }
} catch {
    Write-Host ("TOOLKIT STOPPED: " + $_.Exception.Message) -ForegroundColor Red
    Write-Log ("FATAL: " + $_.Exception.Message)
    exit 1
}
