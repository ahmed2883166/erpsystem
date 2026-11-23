param(
  [switch]$EnableLan,
  [switch]$ConfigureFirewall
)

$ErrorActionPreference = "Stop"
foreach ($command in @("py", "node", "npm")) {
  if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
    throw "المتطلب غير مثبت أو غير موجود في PATH: $command"
  }
}
& py -3.12 --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "يلزم Python 3.12." }
$NodeMajor = [int]((node --version).TrimStart("v").Split(".")[0])
if ($NodeMajor -lt 22) { throw "يلزم Node.js 22 أو أحدث لبناء ملفات الواجهة." }

$PostgresServices = @(Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue)
if ($PostgresServices.Count -eq 0) {
  throw "لم يتم العثور على خدمة PostgreSQL في Windows. ثبّت PostgreSQL 16 وشغّل خدمته أولًا."
}
$RunningPostgres = $PostgresServices | Where-Object { $_.Status -eq "Running" } | Select-Object -First 1
if (-not $RunningPostgres) {
  $ServiceNames = ($PostgresServices | ForEach-Object { $_.Name }) -join ", "
  throw "خدمة PostgreSQL متوقفة ($ServiceNames). شغّلها من Windows Services ثم أعد المحاولة."
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$EnvFile = Join-Path $Backend ".env"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
$Waitress = Join-Path $Backend ".venv\Scripts\waitress-serve.exe"
if (-not (Test-Path -LiteralPath $EnvFile)) {
  throw "ملف backend/.env غير موجود. جهّز إعداد الاتصال بقاعدة PostgreSQL أولًا."
}

# Fail closed: production must be served behind a trusted HTTPS-terminating proxy.
$ProductionEnv = @{}
foreach ($Line in Get-Content -LiteralPath $EnvFile) {
  if ($Line -match "^\s*([A-Z0-9_]+)\s*=\s*(.*)$") {
    $ProductionEnv[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
  }
}
foreach ($SecureKey in @("SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_SSL_REDIRECT", "DJANGO_HTTPS_TERMINATED_BY_PROXY", "SECURE_HSTS_INCLUDE_SUBDOMAINS")) {
  if ($ProductionEnv[$SecureKey] -notin @("True", "true", "1", "yes", "on")) {
    throw "إعداد الإنتاج $SecureKey يجب أن يكون مفعّلًا. لم يتم تشغيل التطبيق أو ترحيل قاعدة البيانات."
  }
}
$HstsSeconds = 0
if (-not [int]::TryParse($ProductionEnv["SECURE_HSTS_SECONDS"], [ref]$HstsSeconds) -or $HstsSeconds -lt 31536000) {
  throw "SECURE_HSTS_SECONDS يجب ألا يقل عن سنة (31536000 ثانية)."
}
if (-not $ProductionEnv["CSRF_TRUSTED_ORIGINS"] -or $ProductionEnv["CSRF_TRUSTED_ORIGINS"] -notmatch "https://") {
  throw "أضف عنوان HTTPS الخاص ببوابة الشركة إلى CSRF_TRUSTED_ORIGINS."
}
$env:DJANGO_DEBUG = "False"
$env:DJANGO_HTTPS_TERMINATED_BY_PROXY = "True"

if (-not (Test-Path $Python)) {
  py -3.12 -m venv (Join-Path $Backend ".venv")
  if ($LASTEXITCODE -ne 0) { throw "تعذر إنشاء بيئة Python." }
}
& $Python -m pip install -r (Join-Path $Backend "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "فشل تثبيت متطلبات Backend." }

Push-Location $Frontend
try {
  if (-not (Test-Path "package-lock.json")) {
    throw "ملف package-lock.json غير موجود؛ أُوقف تشغيل الإنتاج."
  }
  & npm.cmd ci
  if ($LASTEXITCODE -ne 0) { throw "فشل تثبيت متطلبات Frontend المثبتة." }
  & npm.cmd run build
  if ($LASTEXITCODE -ne 0) { throw "فشل بناء واجهة React." }
} finally { Pop-Location }

Push-Location $Backend
try {
  & $Python manage.py check --deploy
  if ($LASTEXITCODE -ne 0) { throw "فشل فحص إعدادات النشر. راجع التحذيرات والإعدادات." }

  $MigrationPlan = & $Python manage.py migrate --plan
  if ($LASTEXITCODE -ne 0) { throw "تعذر فحص خطة ترحيل قاعدة البيانات." }
  $MigrationPlanText = $MigrationPlan -join "`n"
  if ($MigrationPlanText -notmatch "No planned migration operations") {
    $EnvValues = @{}
    foreach ($Line in Get-Content -LiteralPath $EnvFile) {
      if ($Line -match "^\s*([A-Z0-9_]+)\s*=\s*(.*)$") {
        $Value = $Matches[2].Trim().Trim('"').Trim("'")
        $EnvValues[$Matches[1]] = $Value
      }
    }
    foreach ($RequiredKey in @("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")) {
      if (-not $EnvValues.ContainsKey($RequiredKey) -or -not $EnvValues[$RequiredKey]) {
        throw "إعداد $RequiredKey غير موجود في backend/.env؛ أُوقفت الترقية قبل تعديل قاعدة البيانات."
      }
    }

    $PgDumpCommand = Get-Command "pg_dump.exe" -ErrorAction SilentlyContinue
    $PgDumpPath = if ($PgDumpCommand) { $PgDumpCommand.Source } else { $null }
    if (-not $PgDumpPath) {
      $PostgresRoot = Join-Path $env:ProgramFiles "PostgreSQL"
      if (Test-Path $PostgresRoot) {
        $PgDumpPath = Get-ChildItem -Path $PostgresRoot -Filter "pg_dump.exe" -File -Recurse -ErrorAction SilentlyContinue |
          Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
      }
    }
    if (-not $PgDumpPath) {
      throw "توجد migrations معلقة، لكن pg_dump.exe غير موجود. أضف PostgreSQL bin إلى PATH؛ لم يتم تعديل قاعدة البيانات."
    }

    $BackupDirectory = Join-Path $Root "backups"
    New-Item -ItemType Directory -Force -Path $BackupDirectory | Out-Null
    $BackupName = "admrail-before-migration-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".dump"
    $BackupPath = Join-Path $BackupDirectory $BackupName
    $env:PGPASSWORD = $EnvValues["POSTGRES_PASSWORD"]
    try {
      $PgHost = if ($EnvValues["POSTGRES_HOST"]) { $EnvValues["POSTGRES_HOST"] } else { "127.0.0.1" }
      $PgPort = if ($EnvValues["POSTGRES_PORT"]) { $EnvValues["POSTGRES_PORT"] } else { "5432" }
      & $PgDumpPath --host=$PgHost --port=$PgPort --username=$EnvValues["POSTGRES_USER"] --dbname=$EnvValues["POSTGRES_DB"] --format=custom --no-password --file=$BackupPath
      if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $BackupPath) -or (Get-Item $BackupPath).Length -eq 0) {
        throw "فشل إنشاء النسخة الاحتياطية قبل الترقية؛ لم يتم تطبيق migrations."
      }
    } finally {
      Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
    Write-Host "تم إنشاء نسخة احتياطية قبل الترقية: $BackupPath" -ForegroundColor Green
  }

  & $Python manage.py migrate --noinput
  if ($LASTEXITCODE -ne 0) { throw "فشل تطبيق migrations." }
  & $Python manage.py seed_reference_data
  if ($LASTEXITCODE -ne 0) { throw "فشل تجهيز العملات المرجعية." }
  & $Python manage.py collectstatic --noinput
  if ($LASTEXITCODE -ne 0) { throw "فشل تجميع ملفات Django الثابتة." }
  $SuperuserCount = & $Python manage.py shell -c "from django.contrib.auth import get_user_model; print(get_user_model().objects.filter(is_superuser=True).count())"
  if ($LASTEXITCODE -ne 0) { throw "تعذر التحقق من مستخدم الإدارة." }
  if ([int]($SuperuserCount | Select-Object -Last 1) -eq 0) {
    & $Python manage.py createsuperuser
    if ($LASTEXITCODE -ne 0) { throw "تعذر إنشاء مستخدم الإدارة." }
  }
  $SuperuserNames = & $Python manage.py shell -c "from django.contrib.auth import get_user_model; print(','.join(get_user_model().objects.filter(is_superuser=True, is_active=True).values_list('username', flat=True)))"
  if ($LASTEXITCODE -ne 0) { throw "تعذر مراجعة حسابات مشرفي النظام." }
  $ActiveSuperusers = @($SuperuserNames | Select-Object -Last 1) -split ","
  if ($ActiveSuperusers.Count -gt 2) {
    Write-Warning ("عدد مشرفي النظام النشطين أكبر من الموصى به (2): " + ($ActiveSuperusers -join ", "))
    Write-Warning "استخدم أدوار الشركة للاستخدام اليومي وراجع سجل قراءة المشرفين العام."
  }
} finally { Pop-Location }

$Bind = "127.0.0.1:8000"
if ($EnableLan) {
  $LanHost = $ProductionEnv["ADMRAIL_LAN_HOST"]
  if (-not $LanHost -or $LanHost -notmatch "^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$") {
    throw "اضبط ADMRAIL_LAN_HOST باسم DNS داخلي ثابت مثل erp.company.lan داخل backend/.env."
  }
  $AllowedHosts = @()
  if ($ProductionEnv["DJANGO_ALLOWED_HOSTS"]) {
    $AllowedHosts = $ProductionEnv["DJANGO_ALLOWED_HOSTS"].Split(",") | ForEach-Object { $_.Trim() }
  }
  if ($AllowedHosts -notcontains $LanHost) {
    throw "أضف $LanHost إلى DJANGO_ALLOWED_HOSTS داخل backend/.env."
  }
  $TrustedOrigins = @()
  if ($ProductionEnv["CSRF_TRUSTED_ORIGINS"]) {
    $TrustedOrigins = $ProductionEnv["CSRF_TRUSTED_ORIGINS"].Split(",") | ForEach-Object { $_.Trim().TrimEnd("/") }
  }
  if ($TrustedOrigins -notcontains "https://$LanHost") {
    throw "أضف https://$LanHost إلى CSRF_TRUSTED_ORIGINS داخل backend/.env."
  }
  $CaddyCommand = Get-Command "caddy.exe" -ErrorAction SilentlyContinue
  if (-not $CaddyCommand) { $CaddyCommand = Get-Command "caddy" -ErrorAction SilentlyContinue }
  if (-not $CaddyCommand) {
    throw "وضع LAN الآمن يحتاج Caddy مثبتًا على جهاز الخادم. ثبّته مرة واحدة ثم أعد التشغيل."
  }
  $CaddyDirectory = Join-Path $Root "lan"
  New-Item -ItemType Directory -Force -Path $CaddyDirectory | Out-Null
  $CaddyFile = Join-Path $CaddyDirectory "Caddyfile"
  $CaddyConfig = @("https://$LanHost {", "    tls internal", "    encode zstd gzip", "    reverse_proxy 127.0.0.1:8000", "}") -join [Environment]::NewLine
  Set-Content -LiteralPath $CaddyFile -Value $CaddyConfig -Encoding ASCII
  $ExistingCaddy = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match "^caddy(\.exe)?$" -and $_.CommandLine -like "*$CaddyFile*"
  } | Select-Object -First 1
  if (-not $ExistingCaddy) {
    $CaddyProcess = Start-Process -FilePath $CaddyCommand.Path -WorkingDirectory $CaddyDirectory -ArgumentList @("run", "--config", "`"$CaddyFile`"", "--adapter", "caddyfile") -PassThru -WindowStyle Minimized
    Start-Sleep -Seconds 2
    if ($CaddyProcess.HasExited) { throw "تعذر تشغيل Caddy. راجع تعارض المنفذ 443 أو سجل Caddy." }
  }
  if ($ConfigureFirewall) {
    $IsAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $IsAdmin) { throw "شغّل PowerShell كمسؤول لاستخدام -ConfigureFirewall." }
    New-NetFirewallRule -DisplayName "Admrail ERP HTTPS LAN" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 443 -Profile Private -ErrorAction SilentlyContinue | Out-Null
  } else {
    Write-Host "إذا لم يكن المنفذ 443 مسموحًا على ملف تعريف Private، افتح PowerShell كمسؤول ونفّذ: New-NetFirewallRule -DisplayName 'Admrail ERP HTTPS LAN' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 443 -Profile Private" -ForegroundColor Yellow
  }
  Write-Host "LAN URL: https://$LanHost" -ForegroundColor Green
  Write-Host "Waitress remains private on 127.0.0.1:8000; Caddy terminates internal HTTPS on TCP 443." -ForegroundColor Green
  Write-Host "Trust the Caddy local root certificate on each client: %APPDATA%\Caddy\pki\authorities\local\root.crt" -ForegroundColor Yellow
} else {
  Write-Host "Admrail ERP v2 Production mode (localhost only): http://$Bind" -ForegroundColor Green
}

if (-not (Test-Path $Waitress)) { throw "لم يتم تثبيت Waitress داخل بيئة Backend." }
& $Waitress "--listen=$Bind" "config.wsgi:application"
if ($LASTEXITCODE -ne 0) { throw "توقف خادم Waitress بخطأ." }
