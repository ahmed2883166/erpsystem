#requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$script:AppRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:StateRoot = Join-Path $env:LOCALAPPDATA "Admrail\ControlCenter"
New-Item -ItemType Directory -Force -Path $script:StateRoot | Out-Null
$script:StateFile = Join-Path $script:StateRoot "lan-service.json"
$script:StdoutLog = Join-Path $script:StateRoot "lan-service.out.log"
$script:StderrLog = Join-Path $script:StateRoot "lan-service.err.log"
$script:Runner = Join-Path $script:AppRoot "run-admrail-lan.ps1"
$script:Toolkit = Join-Path $script:AppRoot "Admrail-Owner-Toolkit\Start-Owner-Toolkit.cmd"
$script:PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

function Get-LanHost {
    $envFile = Join-Path $script:AppRoot "backend\.env"
    if (-not (Test-Path -LiteralPath $envFile)) { return $null }
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match '^\s*ADMRAIL_LAN_HOST\s*=\s*(.*?)\s*$') {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return $null
}
function Get-TrackedProcess {
    if (-not (Test-Path -LiteralPath $script:StateFile)) { return $null }
    try { $state = Get-Content -LiteralPath $script:StateFile -Raw | ConvertFrom-Json }
    catch { throw "ملف حالة التشغيل تالف. لن يتم إيقاف أي عملية تلقائيًا؛ راجع $script:StateFile" }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$state.pid)" -ErrorAction SilentlyContinue
    if (-not $proc) {
        Remove-Item -LiteralPath $script:StateFile -Force -ErrorAction SilentlyContinue
        return $null
    }
    $expected = [IO.Path]::GetFullPath($script:Runner)
    if (-not $proc.CommandLine -or $proc.CommandLine.IndexOf($expected, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw "رقم العملية المحفوظ لم يعد يطابق مشغل Admrail. لن يتم إيقاف عملية غير معروفة."
    }
    return $proc
}
function Test-ListeningPort([int]$Port) {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1)
}
function Get-PostgresStatus {
    $services = @(Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue)
    if ($services.Count -eq 0) { return "غير مكتشف" }
    if (@($services | Where-Object Status -eq "Running").Count -gt 0) { return "يعمل" }
    return "متوقف"
}
function Get-LogText {
    $parts = @()
    foreach ($file in @($script:StdoutLog, $script:StderrLog)) {
        if (Test-Path -LiteralPath $file) {
            $tail = @(Get-Content -LiteralPath $file -Tail 12 -ErrorAction SilentlyContinue)
            if ($tail.Count -gt 0) { $parts += $tail }
        }
    }
    if ($parts.Count -eq 0) { return "لا توجد رسائل تشغيل حتى الآن." }
    return ($parts -join [Environment]::NewLine)
}
function Refresh-Status {
    try {
        $tracked = Get-TrackedProcess
        $running = [bool]$tracked
        $pg = Get-PostgresStatus
        $port8000 = Test-ListeningPort 8000
        $port443 = Test-ListeningPort 443
        if ($running) {
            $statusLabel.Text = "حالة Admrail: مشغل الشبكة يعمل (PID $($tracked.ProcessId))"
            $statusLabel.ForeColor = [Drawing.Color]::DarkGreen
        } elseif ($port8000 -or $port443) {
            $statusLabel.Text = "حالة Admrail: يوجد برنامج يستمع على منفذ التطبيق، لكنه ليس مشغلًا بدأته هذه الأداة"
            $statusLabel.ForeColor = [Drawing.Color]::DarkOrange
        } else {
            $statusLabel.Text = "حالة Admrail: متوقف"
            $statusLabel.ForeColor = [Drawing.Color]::Firebrick
        }
        $detailsLabel.Text = "PostgreSQL: $pg     |     Backend 8000: $(if ($port8000) {'مفتوح محليًا'} else {'مغلق'})     |     HTTPS 443: $(if ($port443) {'يستمع'} else {'لا يستمع'})"
        $url = Get-LanHost
        if ($url) { $urlLabel.Text = "عنوان الشبكة: https://$url" } else { $urlLabel.Text = "عنوان الشبكة: اضبط ADMRAIL_LAN_HOST في backend\.env" }
        $logBox.Text = Get-LogText
        $stopButton.Enabled = $running
        $startButton.Enabled = -not $running
        $openButton.Enabled = $running -and [bool]$url
    } catch {
        $statusLabel.Text = "تعذر قراءة الحالة: $($_.Exception.Message)"
        $statusLabel.ForeColor = [Drawing.Color]::Firebrick
        $stopButton.Enabled = $false
        $startButton.Enabled = $false
    }
}
function Start-LanService {
    if (-not (Test-Path -LiteralPath $script:Runner -PathType Leaf)) { throw "ملف تشغيل LAN غير موجود: $script:Runner" }
    if (-not (Test-Path -LiteralPath (Join-Path $script:AppRoot "backend\.env") -PathType Leaf)) { throw "ملف backend\.env غير موجود. شغّل التهيئة أولًا." }
    if (Get-TrackedProcess) { throw "Admrail يعمل بالفعل من خلال هذه الأداة." }
    if (Test-ListeningPort 8000 -or Test-ListeningPort 443) { throw "المنفذ 8000 أو 443 مستخدم بالفعل. لن يتم تشغيل نسخة ثانية أو إيقاف الخدمة الأخرى." }
    if ((Get-PostgresStatus) -ne "يعمل") { throw "خدمة PostgreSQL متوقفة. شغّل PostgreSQL ثم أعد المحاولة." }
    $hostName = Get-LanHost
    if (-not $hostName) { throw "اضبط ADMRAIL_LAN_HOST في backend\.env أولًا." }
    if (Test-Path -LiteralPath $script:StdoutLog) { Remove-Item -LiteralPath $script:StdoutLog -Force }
    if (Test-Path -LiteralPath $script:StderrLog) { Remove-Item -LiteralPath $script:StderrLog -Force }
    $arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + $script:Runner + '"'
    $proc = Start-Process -FilePath $script:PowerShell -ArgumentList $arguments -WorkingDirectory $script:AppRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput $script:StdoutLog -RedirectStandardError $script:StderrLog
    $state = [ordered]@{ pid = $proc.Id; started = (Get-Date).ToString("o"); runner = [IO.Path]::GetFullPath($script:Runner) }
    $state | ConvertTo-Json | Set-Content -LiteralPath $script:StateFile -Encoding ASCII
    Start-Sleep -Seconds 3
    $proc.Refresh()
    if ($proc.HasExited) {
        Remove-Item -LiteralPath $script:StateFile -Force -ErrorAction SilentlyContinue
        throw "لم يبدأ خادم LAN. راجع سجل التشغيل أسفل النافذة."
    }
    Refresh-Status
}
function Stop-LanService {
    $root = Get-TrackedProcess
    if (-not $root) { throw "لا يوجد تشغيل مسجل لإيقافه. لن يتم إيقاف أي خدمة خارجية." }
    $all = @(Get-CimInstance Win32_Process)
    $ids = New-Object 'System.Collections.Generic.HashSet[int]'
    [void]$ids.Add([int]$root.ProcessId)
    $ordered = New-Object 'System.Collections.Generic.List[int]'
    $ordered.Add([int]$root.ProcessId)
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($item in $all) {
            $parent = [int]$item.ParentProcessId
            $child = [int]$item.ProcessId
            if ($ids.Contains($parent) -and -not $ids.Contains($child)) {
                [void]$ids.Add($child)
                $ordered.Add($child)
                $changed = $true
            }
        }
    }
    for ($i = $ordered.Count - 1; $i -ge 0; $i--) {
        Stop-Process -Id $ordered[$i] -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    Remove-Item -LiteralPath $script:StateFile -Force -ErrorAction SilentlyContinue
    Refresh-Status
}
function Open-OwnerToolkit {
    if (-not (Test-Path -LiteralPath $script:Toolkit -PathType Leaf)) { throw "أداة المالك غير موجودة: $script:Toolkit" }
    Start-Process -FilePath $script:Toolkit -WorkingDirectory $script:AppRoot
}

$form = New-Object Windows.Forms.Form
$form.Text = "Admrail ERP | مركز التحكم"
$form.StartPosition = "CenterScreen"
$form.Size = New-Object Drawing.Size(900, 650)
$form.MinimumSize = New-Object Drawing.Size(760, 560)
$form.Font = New-Object Drawing.Font("Segoe UI", 10)
$form.RightToLeft = [Windows.Forms.RightToLeft]::Yes
$form.RightToLeftLayout = $true
$form.BackColor = [Drawing.Color]::WhiteSmoke

$header = New-Object Windows.Forms.Label
$header.Text = "مركز التحكم في Admrail ERP"
$header.Font = New-Object Drawing.Font("Segoe UI", 19, [Drawing.FontStyle]::Bold)
$header.Location = New-Object Drawing.Point(25, 18)
$header.Size = New-Object Drawing.Size(820, 42)
$form.Controls.Add($header)

$statusLabel = New-Object Windows.Forms.Label
$statusLabel.Location = New-Object Drawing.Point(30, 78)
$statusLabel.Size = New-Object Drawing.Size(820, 32)
$statusLabel.Font = New-Object Drawing.Font("Segoe UI", 12, [Drawing.FontStyle]::Bold)
$form.Controls.Add($statusLabel)

$detailsLabel = New-Object Windows.Forms.Label
$detailsLabel.Location = New-Object Drawing.Point(30, 116)
$detailsLabel.Size = New-Object Drawing.Size(820, 28)
$form.Controls.Add($detailsLabel)

$urlLabel = New-Object Windows.Forms.Label
$urlLabel.Location = New-Object Drawing.Point(30, 148)
$urlLabel.Size = New-Object Drawing.Size(820, 28)
$urlLabel.ForeColor = [Drawing.Color]::MidnightBlue
$form.Controls.Add($urlLabel)

$startButton = New-Object Windows.Forms.Button
$startButton.Text = "تشغيل خدمة الشبكة"
$startButton.Location = New-Object Drawing.Point(30, 195)
$startButton.Size = New-Object Drawing.Size(190, 42)
$startButton.BackColor = [Drawing.Color]::Honeydew
$startButton.Add_Click({
    try { Start-LanService; [Windows.Forms.MessageBox]::Show("تم إرسال أمر التشغيل. تابع الحالة والسجل.", "Admrail", "OK", "Information") | Out-Null }
    catch { [Windows.Forms.MessageBox]::Show($_.Exception.Message, "تعذر التشغيل", "OK", "Warning") | Out-Null; Refresh-Status }
})
$form.Controls.Add($startButton)

$stopButton = New-Object Windows.Forms.Button
$stopButton.Text = "إيقاف خدمة الشبكة"
$stopButton.Location = New-Object Drawing.Point(235, 195)
$stopButton.Size = New-Object Drawing.Size(190, 42)
$stopButton.BackColor = [Drawing.Color]::MistyRose
$stopButton.Add_Click({
    if ([Windows.Forms.MessageBox]::Show("سيتم إيقاف خدمة Admrail التي بدأها مركز التحكم فقط. هل تريد المتابعة؟", "تأكيد الإيقاف", "YesNo", "Warning") -eq "Yes") {
        try { Stop-LanService } catch { [Windows.Forms.MessageBox]::Show($_.Exception.Message, "تعذر الإيقاف", "OK", "Warning") | Out-Null }
    }
})
$form.Controls.Add($stopButton)

$openButton = New-Object Windows.Forms.Button
$openButton.Text = "فتح Admrail في المتصفح"
$openButton.Location = New-Object Drawing.Point(440, 195)
$openButton.Size = New-Object Drawing.Size(205, 42)
$openButton.Add_Click({
    $hostName = Get-LanHost
    if ($hostName) { Start-Process ("https://" + $hostName) }
})
$form.Controls.Add($openButton)

$ownerButton = New-Object Windows.Forms.Button
$ownerButton.Text = "أدوات المالك وقواعد البيانات"
$ownerButton.Location = New-Object Drawing.Point(660, 195)
$ownerButton.Size = New-Object Drawing.Size(190, 42)
$ownerButton.Add_Click({
    try { Open-OwnerToolkit } catch { [Windows.Forms.MessageBox]::Show($_.Exception.Message, "تعذر فتح الأدوات", "OK", "Warning") | Out-Null }
})
$form.Controls.Add($ownerButton)

$refreshButton = New-Object Windows.Forms.Button
$refreshButton.Text = "تحديث الحالة"
$refreshButton.Location = New-Object Drawing.Point(30, 250)
$refreshButton.Size = New-Object Drawing.Size(150, 36)
$refreshButton.Add_Click({ Refresh-Status })
$form.Controls.Add($refreshButton)

$logTitle = New-Object Windows.Forms.Label
$logTitle.Text = "آخر رسائل التشغيل والأخطاء"
$logTitle.Location = New-Object Drawing.Point(30, 305)
$logTitle.Size = New-Object Drawing.Size(820, 28)
$logTitle.Font = New-Object Drawing.Font("Segoe UI", 10, [Drawing.FontStyle]::Bold)
$form.Controls.Add($logTitle)

$logBox = New-Object Windows.Forms.TextBox
$logBox.Location = New-Object Drawing.Point(30, 338)
$logBox.Size = New-Object Drawing.Size(820, 245)
$logBox.Multiline = $true
$logBox.ReadOnly = $true
$logBox.ScrollBars = "Vertical"
$logBox.WordWrap = $false
$logBox.Font = New-Object Drawing.Font("Consolas", 9)
$logBox.RightToLeft = [Windows.Forms.RightToLeft]::No
$form.Controls.Add($logBox)

$footer = New-Object Windows.Forms.Label
$footer.Text = "إدارة قواعد البيانات والحذف والاستعادة متاحة من أدوات المالك بعد التحقق من PIN."
$footer.Location = New-Object Drawing.Point(30, 590)
$footer.Size = New-Object Drawing.Size(820, 24)
$footer.ForeColor = [Drawing.Color]::DimGray
$form.Controls.Add($footer)

$timer = New-Object Windows.Forms.Timer
$timer.Interval = 4000
$timer.Add_Tick({ Refresh-Status })
$form.Add_Shown({ Refresh-Status; $timer.Start() })
$form.Add_FormClosed({ $timer.Stop() })
[void]$form.ShowDialog()
