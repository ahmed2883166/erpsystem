$ErrorActionPreference = "Stop"
foreach ($command in @("docker", "py", "node", "npm")) {
  if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
    throw "المتطلب غير مثبت أو غير موجود في PATH: $command"
  }
}
& docker compose version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "يلزم تثبيت Docker Desktop مع Docker Compose." }
& py -3.12 --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "يلزم تثبيت Python 3.12 وإتاحته من خلال py launcher." }
& node --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "يلزم تثبيت Node.js 22 أو أحدث." }
& npm --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "تعذر تشغيل npm." }

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
Push-Location $Root
try {
  $ComposeEnv = Join-Path $Root ".env"
  if (-not (Test-Path $ComposeEnv)) {
    $DatabaseSecret = "nexus-db-" + [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
    @(
      "POSTGRES_DB=nexus_dev",
      "POSTGRES_USER=nexus_app",
      "POSTGRES_PASSWORD=$DatabaseSecret",
      "POSTGRES_PORT=5432"
    ) | Set-Content -LiteralPath $ComposeEnv -Encoding ASCII
  }
  $ComposeEnvText = Get-Content -LiteralPath $ComposeEnv -Raw
  $PasswordMatch = [regex]::Match($ComposeEnvText, "(?m)^POSTGRES_PASSWORD=(.+)$")
  if (-not $PasswordMatch.Success) {
    $DatabaseSecret = "nexus-db-" + [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
    Add-Content -LiteralPath $ComposeEnv -Value "POSTGRES_PASSWORD=$DatabaseSecret" -Encoding ASCII
  } else {
    $DatabaseSecret = $PasswordMatch.Groups[1].Value.Trim()
  }

  $EnvFile = Join-Path $Backend ".env"
  if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $Backend ".env.example") $EnvFile
    $LocalSecret = "nexus-local-" + [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
    $BackendEnvText = (Get-Content $EnvFile -Raw).Replace("replace-this-for-local-development", $LocalSecret)
    $BackendEnvText = $BackendEnvText.Replace("POSTGRES_PASSWORD=nexus_local_only", "POSTGRES_PASSWORD=$DatabaseSecret")
    Set-Content -LiteralPath $EnvFile -Value $BackendEnvText -NoNewline
  }

  docker compose -f compose.yaml up -d db
  if ($LASTEXITCODE -ne 0) { throw "تعذر تشغيل PostgreSQL. تأكد أن Docker Desktop يعمل." }
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
  Start-Process -FilePath $Python -WorkingDirectory $Backend -ArgumentList @("manage.py", "runserver", "127.0.0.1:8000")
  Start-Process -FilePath "cmd.exe" -WorkingDirectory $Frontend -ArgumentList @("/c", "npm run dev -- --host 127.0.0.1")
  $SuperuserCount = & $Python (Join-Path $Backend "manage.py") shell -c "from django.contrib.auth import get_user_model; print(get_user_model().objects.filter(is_superuser=True).count())"
  if ($LASTEXITCODE -ne 0) { throw "تعذر التحقق من مستخدم الإدارة." }
  if ([int]($SuperuserCount | Select-Object -Last 1) -eq 0) {
    Start-Process -FilePath $Python -WorkingDirectory $Backend -ArgumentList @("manage.py", "createsuperuser")
  }
  Write-Host "Admrail ERP v2 بدأ محليًا: http://127.0.0.1:5173" -ForegroundColor Green
} finally { Pop-Location }
