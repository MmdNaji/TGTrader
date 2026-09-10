<#
  ساخت TGTrader روی ویندوز خودت - بدون GitHub.

  اجرا: راست‌کلیک روی build_windows.bat و «Run»
        یا در PowerShell:  .\scripts\build_windows.ps1

  گزینه‌ها:
    -SkipTests      تست‌ها را اجرا نکن (سریع‌تر، ولی کورتر)
    -NoInstaller    فقط پوشه‌ی قابل‌حمل بساز، نصب‌کننده نه
    -Version 0.5.9  شماره‌ی نسخه را در برنامه بنویس
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$NoInstaller,
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Say  ([string]$m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Ok   ([string]$m) { Write-Host "    $m" -ForegroundColor Green }
function Warn ([string]$m) { Write-Host "    $m" -ForegroundColor Yellow }
function Die  ([string]$m) { Write-Host ""; Write-Host "!!! $m" -ForegroundColor Red; Write-Host ""; exit 1 }

# ---------------------------------------------------------------- 1. پایتون
Say "پایتون"
# Asked with -V rather than a python one-liner: the one-liner needed quotes inside quotes,
# which is one escaping layer away from breaking in a way nobody would notice until it silently
# picked the wrong interpreter.
$py = $null
foreach ($c in @("py -3.12", "py -3.11", "python", "python3")) {
    $parts = $c.Split(" ")
    $exe = $parts[0]
    $arg = if ($parts.Count -gt 1) { $parts[1] } else { $null }
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $out = if ($arg) { & $exe $arg -V 2>&1 } else { & $exe -V 2>&1 }
        if ($LASTEXITCODE -ne 0) { continue }
        if ("$out" -match "(\d+)\.(\d+)") {
            $maj = [int]$Matches[1]; $min = [int]$Matches[2]
            if ($maj -eq 3 -and $min -ge 11) { $py = $c; break }
        }
    } catch { }
}
if (-not $py) {
    Die @"
پایتون ۳.۱۱ یا بالاتر پیدا نشد.

از اینجا بگیر و نصب کن:  https://www.python.org/downloads/
هنگام نصب حتماً تیک "Add python.exe to PATH" را بزن، بعد این پنجره را ببند و دوباره اجرا کن.
"@
}
Ok "پیدا شد: $py"

# ---------------------------------------------------------------- 2. محیط مجازی
Say "کتابخانه‌ها"
if (-not (Test-Path ".venv")) {
    $exe, $arg = $py.Split(" ", 2)
    if ($arg) { & $exe $arg -m venv .venv } else { & $exe -m venv .venv }
    if ($LASTEXITCODE -ne 0) { Die "ساخت محیط مجازی (.venv) شکست خورد." }
    Ok "محیط مجازی ساخته شد"
} else {
    Ok "محیط مجازی از قبل هست"
}
$vpy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) { Die "‏.venv خراب است. پوشه‌ی .venv را پاک کن و دوباره اجرا کن." }

& $vpy -m pip install --upgrade pip --quiet
& $vpy -m pip install -r requirements.txt pyinstaller pytest --quiet
if ($LASTEXITCODE -ne 0) { Die "نصب کتابخانه‌ها شکست خورد. اینترنت/پروکسی را چک کن." }
Ok "همه نصب شدند"

# ---------------------------------------------------------------- 3. شماره‌ی نسخه
if ($Version) {
    Say "نسخه"
    $init = "trader\__init__.py"
    (Get-Content $init -Raw) -replace '(?m)^__version__ = .*', "__version__ = `"$Version`"" |
        Set-Content $init -NoNewline -Encoding utf8
    Ok "نسخه روی $Version تنظیم شد"
}
$ver = (& $vpy -c "import trader;print(trader.__version__)").Trim()
Ok "در حال ساخت نسخه‌ی $ver"

# ---------------------------------------------------------------- 4. تست‌ها
if (-not $SkipTests) {
    Say "تست‌ها"
    & $vpy -m pytest -q tests
    if ($LASTEXITCODE -ne 0) {
        Die "تست‌ها رد شدند. با -SkipTests می‌توانی رد شوی، ولی یعنی نسخه‌ای می‌سازی که خودش می‌گوید خراب است."
    }
    Ok "همه پاس شدند"
} else {
    Warn "تست‌ها رد شدند (-SkipTests)"
}

# ---------------------------------------------------------------- 5. ساخت exe
Say "ساخت exe  (چند دقیقه طول می‌کشد)"
Remove-Item -Recurse -Force "dist\TGTrader" -ErrorAction SilentlyContinue
& $vpy -m PyInstaller --noconfirm --clean --windowed --name TGTrader `
    --add-data "trader\knowledge\seed;trader\knowledge\seed" `
    --collect-all ccxt --collect-submodules trader `
    --hidden-import PySide6.QtSvg --hidden-import pyautogui --hidden-import mss --hidden-import PIL `
    run.py
if ($LASTEXITCODE -ne 0) { Die "PyInstaller شکست خورد." }

$exe = "dist\TGTrader\TGTrader.exe"
if (-not (Test-Path $exe)) { Die "‏PyInstaller تمام شد ولی $exe ساخته نشد." }
$mb = [math]::Round((Get-ChildItem "dist\TGTrader" -Recurse | Measure-Object Length -Sum).Sum / 1MB)
Ok "ساخته شد: $exe  ($mb مگابایت)"

# ---------------------------------------------------------------- 6. تست باز شدن
# این همان مرحله‌ای است که نسخه‌ی 0.4.0 را گرفت: همه چیز کامپایل شده بود و برنامه
# موقع باز شدن کرش می‌کرد. اگر پنجره ۸ ثانیه دوام بیاورد، یعنی واقعاً بالا آمده.
Say "تست باز شدن برنامه"
$p = Start-Process -FilePath (Resolve-Path $exe) -PassThru
Start-Sleep -Seconds 8
if ($p.HasExited) {
    Die "برنامه ساخته شد ولی موقع باز شدن بسته شد (کد خروج $($p.ExitCode)). این نسخه را پخش نکن."
}
Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
Ok "باز شد و سرِ پا ماند"

# ---------------------------------------------------------------- 7. نصب‌کننده
if (-not $NoInstaller) {
    Say "نصب‌کننده"
    $iscc = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($iscc) {
        & $iscc "/DAppVersion=$ver" "scripts\installer.iss"
        if ($LASTEXITCODE -ne 0) { Die "‏Inno Setup شکست خورد." }
        Ok "ساخته شد: dist\TGTrader-Setup.exe"
    } else {
        Warn "‏Inno Setup نصب نیست، پس نصب‌کننده ساخته نشد."
        Warn "اگر می‌خواهی: https://jrsoftware.org/isdl.php  (بعد دوباره این اسکریپت را بزن)"
    }
}

# ---------------------------------------------------------------- 8. خلاصه
Write-Host ""
Write-Host "───────────────────────────────────────────" -ForegroundColor DarkGray
Ok "نسخه‌ی $ver آماده است"
Write-Host ""
Write-Host "  برنامه (قابل‌حمل، بدون نصب):" -ForegroundColor Gray
Write-Host "     $root\dist\TGTrader\TGTrader.exe"
if (Test-Path "dist\TGTrader-Setup.exe") {
    Write-Host ""
    Write-Host "  نصب‌کننده:" -ForegroundColor Gray
    Write-Host "     $root\dist\TGTrader-Setup.exe"
}
Write-Host ""
Write-Host "  دفعه‌ی بعد سریع‌تر:  .\scripts\build_windows.ps1 -SkipTests" -ForegroundColor DarkGray
Write-Host "───────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""
