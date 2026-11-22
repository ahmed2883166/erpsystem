$ErrorActionPreference = "Stop"

foreach ($command in @("py", "node", "npm")) {
  if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
    throw "المتطلب غير مثبت أو غير موجود في PATH: $command"
  }
}

& py -3.12 --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "يلزم تثبيت Python 3.12 وإتاحته من خلال py launcher." }
$NodeMajor = [int]((node --version).TrimStart("v").Split(".")[0])
if ($NodeMajor -lt 22) { throw "يلزم Node.js 22 أو أحدث لتشغيل نسخة التطوير." }
& npm --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "تعذر تشغيل npm." }

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
if (-not (Test-Path -LiteralPath $EnvFile)) {
  throw "ملف backend/.env غير موجود. جهّز إعداد الاتصال بقاعدة PostgreSQL أولًا؛ لن ينشئ هذا المشغل قاعدة بيانات أو يغيّر كلمات المرور."
}

$Python = Join-Path $Backend ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
  py -3.12 -m venv (Join-Path $Backend ".venv")
  if ($LASTEXITCODE -ne 0) { throw "تعذر إنشاء بيئة Python 3.12." }
}
if (-not (Test-Path -LiteralPath $Python)) { throw "ملف Python داخل البيئة الافتراضية غير موجود." }

& $Python -m pip install -r (Join-Path $Backend "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "فشل تثبيت متطلبات Backend." }

Push-Location $Backend
try {
  & $Python manage.py check
  if ($LASTEXITCODE -ne 0) { throw "فشل فحص إعدادات Django." }
  & $Python manage.py migrate --noinput
  if ($LASTEXITCODE -ne 0) { throw "فشل تطبيق migrations على PostgreSQL المحلي." }
  & $Python manage.py seed_reference_data
  if ($LASTEXITCODE -ne 0) { throw "فشل تجهيز العملات المرجعية." }
  $SuperuserCount = & $Python manage.py shell -c "from django.contrib.auth import get_user_model; print(get_user_model().objects.filter(is_superuser=True).count())"
  if ($LASTEXITCODE -ne 0) { throw "تعذر التحقق من مستخدم الإدارة." }
} finally { Pop-Location }

Push-Location $Frontend
try {
  if (-not (Test-Path -LiteralPath (Join-Path $Frontend "node_modules"))) {
    & npm.cmd install
    if ($LASTEXITCODE -ne 0) { throw "فشل تثبيت متطلبات Frontend." }
  }
} finally { Pop-Location }

Start-Process -FilePath $Python -WorkingDirectory $Backend -ArgumentList @("manage.py", "runserver", "127.0.0.1:8000")
Start-Process -FilePath "cmd.exe" -WorkingDirectory $Frontend -ArgumentList @("/c", "npm.cmd run dev -- --host 127.0.0.1")

if ([int]($SuperuserCount | Select-Object -Last 1) -eq 0) {
  Start-Process -FilePath $Python -WorkingDirectory $Backend -ArgumentList @("manage.py", "createsuperuser")
}
Write-Host "Admrail ERP v2 بدأ على Windows باستخدام PostgreSQL المحلي: http://127.0.0.1:5173" -ForegroundColor Green
