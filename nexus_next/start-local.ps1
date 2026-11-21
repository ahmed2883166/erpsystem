$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
Push-Location $Root
try {
  docker compose -f compose.yaml up -d db
  if ($LASTEXITCODE -ne 0) { throw "تعذر تشغيل PostgreSQL. تأكد أن Docker Desktop يعمل." }
  $EnvFile = Join-Path $Backend ".env"
  if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $Backend ".env.example") $EnvFile
    $LocalSecret = "nexus-local-" + [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
    (Get-Content $EnvFile -Raw).Replace("replace-this-for-local-development", $LocalSecret) | Set-Content $EnvFile -NoNewline
  }
  $DatabaseReady = $false
  for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
    docker compose -f compose.yaml exec -T db pg_isready *> $null
    if ($LASTEXITCODE -eq 0) { $DatabaseReady = $true; break }
    Start-Sleep -Seconds 2
  }
  if (-not $DatabaseReady) { throw "PostgreSQL لم يصبح جاهزًا. راجع Docker Desktop." }
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
  $SuperuserCount = & $Python (Join-Path $Backend "manage.py") shell -c "from django.contrib.auth import get_user_model; print(get_user_model().objects.filter(is_superuser=True).count())"
  if ($LASTEXITCODE -ne 0) { throw "تعذر التحقق من مستخدم الإدارة." }
  if ([int]($SuperuserCount | Select-Object -Last 1) -eq 0) {
    Start-Process powershell.exe -WorkingDirectory $Backend -ArgumentList @("-NoExit", "-Command", ".\.venv\Scripts\Activate.ps1; python manage.py createsuperuser")
  }
  Write-Host "NEXUS Next بدأ محليًا: http://127.0.0.1:5173" -ForegroundColor Green
} finally { Pop-Location }
