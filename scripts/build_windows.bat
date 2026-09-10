@echo off
REM ساخت TGTrader روی همین کامپیوتر. فقط روی این فایل دوبار کلیک کن.
REM اگر پایتون نصب نیست، اسکریپت خودش می‌گوید از کجا بگیری.
setlocal
cd /d "%~dp0\.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1" %*
echo.
pause
