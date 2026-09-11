<#
  ================================================================================
  THIS FILE MUST BE SAVED AS UTF-8 *WITH* A BOM. DO NOT STRIP THE BOM.

  It contains Persian text, and Windows PowerShell 5.1 - which is what
  build_windows.bat launches - decodes a BOM-less file as the system ANSI code
  page, not UTF-8. The Persian turns to mojibake and the script fails to run at
  all. PowerShell 7 does not care, which is exactly why this cannot be caught on
  a Linux box: pwsh 7 is the only PowerShell there.

  Editing on Linux? `sed`, `>` and most editors will quietly drop the three bytes.
  Check with:  head -c 3 scripts/build_windows.ps1 | xxd    ->  efbbbf
  ================================================================================
#>
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

# Run an external program without letting its stderr become a terminating error.
#
# PyInstaller writes its ordinary INFO progress to stderr. In Windows PowerShell 5.1 a native
# program's stderr becomes a NativeCommandError when it is MERGED into the output stream, and
# under $ErrorActionPreference = "Stop" such an error is terminating.
#
# What is actually established, and what is not:
#   - Redirecting from OUTSIDE this script (powershell -File build_windows.ps1 *>&1) does NOT
#     break it. Measured on Windows 11 / PS 5.1: the full build ran and exited 0. The records
#     are created in the caller's scope, which has its own preference.
#   - The risk is a redirect INSIDE this file, or a wrapper that runs it in the same scope with
#     "Stop" in force. That case has not been reproduced here; this guard removes it as a
#     question rather than proving it first.
#
# It costs nothing either way: exit codes are what decide, and every caller checks
# $LASTEXITCODE. Do not read this comment as "the build was dying" - it was not.
function Invoke-Native {
    param([Parameter(Mandatory)][scriptblock]$Cmd)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Cmd } finally { $ErrorActionPreference = $prev }
}

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

Invoke-Native { & $vpy -m pip install --upgrade pip --quiet }
Invoke-Native { & $vpy -m pip install -r requirements.txt pyinstaller pytest --quiet }
if ($LASTEXITCODE -ne 0) { Die "نصب کتابخانه‌ها شکست خورد. اینترنت/پروکسی را چک کن." }
Ok "همه نصب شدند"

# ---------------------------------------------------------------- 3. شماره‌ی نسخه
if ($Version) {
    Say "نسخه"
    $init = "trader\__init__.py"
    # NOT Set-Content -Encoding utf8: in Windows PowerShell 5.1 - which is what the .bat
    # launches - "utf8" means utf8 WITH a BOM, and this writes a Python source file. Writing
    # the file through .NET with an explicit BOM-less encoding behaves the same on 5.1 and 7.
    $text = (Get-Content $init -Raw) -replace '(?m)^__version__ = .*', "__version__ = `"$Version`""
    [System.IO.File]::WriteAllText($init, $text, (New-Object System.Text.UTF8Encoding $false))
    Ok "نسخه روی $Version تنظیم شد"
}
$ver = (& $vpy -c "import trader;print(trader.__version__)").Trim()
Ok "در حال ساخت نسخه‌ی $ver"

# ---------------------------------------------------------------- 4. تست‌ها
if (-not $SkipTests) {
    Say "تست‌ها"
    Invoke-Native { & $vpy -m pytest -q tests }
    if ($LASTEXITCODE -ne 0) {
        Die "تست‌ها رد شدند. با -SkipTests می‌توانی رد شوی، ولی یعنی نسخه‌ای می‌سازی که خودش می‌گوید خراب است."
    }
    Ok "همه پاس شدند"
} else {
    Warn "تست‌ها رد شدند (-SkipTests)"
}

# ---------------------------------------------------------------- 5. ساخت exe
Say "ساخت exe  (چند دقیقه طول می‌کشد)"
# Built into a staging folder and moved into place only once PyInstaller has SUCCEEDED.
# The previous version deleted dist\TGTrader first, so anything that stopped the script
# between that line and the end of the build destroyed a perfectly good previous build:
# a cancelled run, a closed window, a power cut. It cost one on 2026-09-11. Nothing here
# touches the last good build until there is a new one to replace it with.
$staging = "dist\.staging"
Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
# PyInstaller prints its ordinary progress to stderr - see Invoke-Native above.
Invoke-Native {
    & $vpy -m PyInstaller --noconfirm --clean --windowed --name TGTrader --distpath $staging `
        --add-data "trader\knowledge\seed;trader\knowledge\seed" `
        --collect-all ccxt --collect-submodules trader `
        --hidden-import PySide6.QtSvg --hidden-import pyautogui --hidden-import mss --hidden-import PIL `
        run.py
}
if ($LASTEXITCODE -ne 0) { Die "PyInstaller شکست خورد." }

$staged = "$staging\TGTrader\TGTrader.exe"
if (-not (Test-Path $staged)) { Die "‏PyInstaller تمام شد ولی $staged ساخته نشد." }
# Only now is the previous build touched - and it is RENAMED aside, not deleted, so that a
# move that fails (a file held open by an antivirus scan is the usual one) leaves the machine
# with the old build rather than with none.
$previous = "dist\.previous"
Remove-Item -Recurse -Force $previous -ErrorAction SilentlyContinue
if (Test-Path "dist\TGTrader") { Move-Item "dist\TGTrader" $previous }
try {
    Move-Item "$staging\TGTrader" "dist\TGTrader" -ErrorAction Stop
} catch {
    if (Test-Path $previous) { Move-Item $previous "dist\TGTrader" }
    Die "‏جابه‌جایی بیلد تازه انجام نشد؛ بیلد قبلی سر جایش برگشت. $_"
}
Remove-Item -Recurse -Force $previous, $staging -ErrorAction SilentlyContinue
$exe = "dist\TGTrader\TGTrader.exe"
if (-not (Test-Path $exe)) { Die "‏جابه‌جایی از $staging به dist\TGTrader انجام نشد." }
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
    # Two hard-coded paths were not enough: winget installs Inno Setup per-user by default,
    # which is neither of them. Ask the registry and PATH as well before giving up.
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    foreach ($hive in @("HKLM:", "HKCU:")) {
        foreach ($wow in @("\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                           "\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")) {
            $key = "$hive$wow\Inno Setup 6_is1"
            try {
                $loc = (Get-ItemProperty -Path $key -ErrorAction Stop).InstallLocation
                if ($loc) { $candidates += (Join-Path $loc "ISCC.exe") }
            } catch { }
        }
    }
    $onPath = (Get-Command iscc -ErrorAction SilentlyContinue)
    if ($onPath) { $candidates += $onPath.Source }
    $iscc = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if ($iscc) {
        Invoke-Native { & $iscc "/DAppVersion=$ver" "scripts\installer.iss" }
        if ($LASTEXITCODE -ne 0) { Die "‏Inno Setup شکست خورد." }
        Ok "ساخته شد: dist\TGTrader-Setup.exe"
        # dist\TGTrader-Setup.exe is overwritten by every build, so keep a stamped copy.
        # A flat file rather than a folder per build: they sort by name and sit next to
        # each other. Only the installer - the portable folder is ~240 MB a time.
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $buildsDir = Join-Path $root "builds"
        New-Item -ItemType Directory -Path $buildsDir -Force | Out-Null
        $archiveFile = Join-Path $buildsDir "TGTrader-$ver-$stamp-Setup.exe"
        Copy-Item "dist\TGTrader-Setup.exe" $archiveFile -Force
        Ok "آرشیو شد: builds\TGTrader-$ver-$stamp-Setup.exe"
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
