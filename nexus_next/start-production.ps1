param(
  [switch]$EnableLan
)

$ErrorActionPreference = "Stop"
foreach ($command in @("docker", "py", "node", "npm")) {
  if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
    throw "المتطلب غير مثبت أو غير موجود في PATH: $command"
  }
}
& docker compose version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "يلزم Docker Desktop مع Docker Compose." }
& py -3.12 --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "يلزم Python 3.12." }
$NodeMajor = [int]((node --version).TrimStart("v").Split(".")[0])
if ($NodeMajor -lt 22) { throw "يلزم Node.js 22 أو أحدث." }

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$EnvFile = Join-Path $Backend ".env"
$ComposeEnv = Join-Path $Root ".env"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
$Waitress = Join-Path $Backend ".venv\Scripts\waitress-serve.exe"

Push-Location $Root
try {
  if (-not (Test-Path $ComposeEnv)) {
    throw "ملف nexus_next/.env غير موجود. شغّل start-local.ps1 مرة واحدة لإنشاء إعدادات PostgreSQL المحلية."
  }
  if (-not (Test-Path $EnvFile)) {
    throw "ملف backend/.env غير موجود. شغّل start-local.ps1 مرة واحدة لإنشاء الأسرار المحلية."
  }
  $env:DJANGO_DEBUG = "False"

  docker compose -f compose.yaml up -d db
  if ($LASTEXITCODE -ne 0) { throw "تعذر تشغيل PostgreSQL." }
  $DatabaseReady = $false
  for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
    docker compose -f compose.yaml exec -T db pg_isready *> $null
    if ($LASTEXITCODE -eq 0) { $DatabaseReady = $true; break }
    Start-Sleep -Seconds 2
  }
  if (-not $DatabaseReady) { throw "PostgreSQL لم يصبح جاهزًا." }

  if (-not (Test-Path $Python)) {
    py -3.12 -m venv (Join-Path $Backend ".venv")
    if ($LASTEXITCODE -ne 0) { throw "تعذر إنشاء بيئة Python." }
  }
  & $Python -m pip install -r (Join-Path $Backend "requirements.txt")
  if ($LASTEXITCODE -ne 0) { throw "فشل تثبيت متطلبات Backend." }

  Push-Location $Frontend
  try {
    if (-not (Test-Path "node_modules")) { npm install }
    if ($LASTEXITCODE -ne 0) { throw "فشل تثبيت متطلبات Frontend." }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "فشل بناء واجهة React." }
  } finally { Pop-Location }

  Push-Location $Backend
  try {
    & $Python manage.py check --deploy
    if ($LASTEXITCODE -ne 0) { throw "فشل فحص إعدادات النشر. راجع التحذيرات والإعدادات." }
    & $Python manage.py migrate --noinput
    if ($LASTEXITCODE -ne 0) { throw "فشل تطبيق migrations." }
    & $Python manage.py seed_reference_data
    if ($LASTEXITCODE -ne 0) { throw "فشل تجهيز العملات المرجعية." }
    & $Python manage.py collectstatic --noinput
    if ($LASTEXITCODE -ne 0) { throw "فشل تجميع ملفات Django الثابتة." }
  } finally { Pop-Location }

  $Bind = "127.0.0.1:8000"
  if ($EnableLan) {
    $AllowedLine = (Select-String -Path $EnvFile -Pattern "^DJANGO_ALLOWED_HOSTS=" | Select-Object -First 1).Line
    $AllowedHosts = @()
    if ($AllowedLine) {
      $AllowedHosts = (($AllowedLine -split "=", 2)[1] -replace "[\"']", "").Split(",") | ForEach-Object { $_.Trim() }
    }
    $HasLanHost = $AllowedHosts | Where-Object { $_ -and $_ -notin @("localhost", "127.0.0.1", "[::1]") }
    if (-not $HasLanHost) {
      throw "قبل تفعيل الشبكة، أضف عنوان IP أو اسم جهاز الشبكة إلى DJANGO_ALLOWED_HOSTS داخل backend/.env."
    }
    $Bind = "0.0.0.0:8000"
    Write-Warning "الخادم سيستمع على الشبكة المحلية. استخدم جدار حماية Windows واسمح فقط لأجهزة الشبكة الموثوقة."
  }

  $SuperuserCount = & $Python (Join-Path $Backend "manage.py") shell -c "from django.contrib.auth import get_user_model; print(get_user_model().objects.filter(is_superuser=True).count())"
  if ($LASTEXITCODE -ne 0) { throw "تعذر التحقق من مستخدم الإدارة." }
  if ([int]($SuperuserCount | Select-Object -Last 1) -eq 0) {
    & $Python (Join-Path $Backend "manage.py") createsuperuser
    if ($LASTEXITCODE -ne 0) { throw "تعذر إنشاء مستخدم الإدارة." }
  }

  if (-not (Test-Path $Waitress)) { throw "لم يتم تثبيت Waitress داخل بيئة Backend." }
  Write-Host "Admrail ERP v2 Production mode: http://$Bind" -ForegroundColor Green
  & $Waitress "--listen=$Bind" "config.wsgi:application"
  if ($LASTEXITCODE -ne 0) { throw "توقف خادم Waitress بخطأ." }
} finally { Pop-Location }
