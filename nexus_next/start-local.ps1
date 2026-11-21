$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
Push-Location $Root
try {
  docker compose -f compose.yaml up -d db
  if ($LASTEXITCODE -ne 0) { throw "تعذر تشغيل PostgreSQL. تأكد أن Docker Desktop يعمل." }
  if (-not (Test-Path (Join-Path $Backend ".env"))) { Copy-Item (Join-Path $Backend ".env.example") (Join-Path $Backend ".env") }
  $Python = Join-Path $Backend ".venv\Scripts\python.exe"
  if (-not (Test-Path $Python)) { py -3.12 -m venv (Join-Path $Backend ".venv") }
  if ($LASTEXITCODE -ne 0) { throw "تعذر إنشاء بيئة Python 3.12." }
  & $Python -m pip install -r (Join-Path $Backend "requirements.txt")
  if ($LASTEXITCODE -ne 0) { throw "فشل تثبيت متطلبات Backend." }
  Push-Location $Backend
  try {
    & $Python manage.py migrate
    if ($LASTEXITCODE -ne 0) { throw "فشل تطبيق migrations على PostgreSQL المحلي." }
    & $Python manage.py seed_reference_data
    if ($LASTEXITCODE -ne 0) { throw "فشل تجهيز العملات المرجعية." }
  } finally { Pop-Location }
  if (-not (Test-Path (Join-Path $Frontend "node_modules"))) { npm --prefix $Frontend install }
  if ($LASTEXITCODE -ne 0) { throw "فشل تثبيت متطلبات Frontend." }
  Start-Process powershell.exe -WorkingDirectory $Backend -ArgumentList @("-NoExit", "-Command", ".\.venv\Scripts\Activate.ps1; python manage.py runserver 127.0.0.1:8000")
  Start-Process powershell.exe -WorkingDirectory $Frontend -ArgumentList @("-NoExit", "-Command", "npm run dev -- --host 127.0.0.1")
  Write-Host "NEXUS Next بدأ محليًا: http://127.0.0.1:5173" -ForegroundColor Green
  Write-Host "لأول تشغيل، أنشئ مستخدم مدير من نافذة Backend: python manage.py createsuperuser"
} finally { Pop-Location }
