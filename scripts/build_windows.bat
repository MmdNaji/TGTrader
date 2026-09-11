@echo off
REM Build TGTrader on this machine. Just double-click this file.
REM If Python is missing, the script tells you where to get it.
REM (Kept ASCII-only on purpose: cmd.exe reads .bat in the console code page.)
setlocal
cd /d "%~dp0\.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1" %*
echo.
pause
