@echo off
REM Build TGTrader.exe on Windows. Run from the project folder:  scripts\build_windows.bat
REM Needs Python 3.11+ from python.org (tick "Add to PATH" in the installer).
setlocal
cd /d %~dp0\..
if not exist .venv ( python -m venv .venv )
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean --windowed --name TGTrader ^
  --collect-submodules trader --hidden-import pyautogui --hidden-import mss --hidden-import PIL ^
  --add-data "trader\knowledge\seed;trader\knowledge\seed" ^
  --collect-all ccxt --hidden-import PySide6.QtSvg ^
  run.py
echo.
echo Done. The app is in dist\TGTrader\TGTrader.exe
echo Optional installer: install Inno Setup and run  iscc scripts\installer.iss
endlocal
