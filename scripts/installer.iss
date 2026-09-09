; Inno Setup script - builds TGTrader-Setup.exe from dist\TGTrader.
; Build: iscc /DAppVersion=0.1.0 scripts\installer.iss   (run scripts\build_windows.bat first)
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6F2B1C5E-7E1A-4B0B-9C2D-TGTRADER0001}
AppName=TGTrader
AppVersion={#AppVersion}
AppVerName=TGTrader {#AppVersion}
AppPublisher=TGTrader
DefaultDirName={autopf}\TGTrader
DefaultGroupName=TGTrader
OutputDir=..\dist
OutputBaseFilename=TGTrader-Setup
Compression=lzma2/max
SolidCompression=yes
; per-user install: no admin rights needed, settings live in %APPDATA%\TGTrader
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
; needed for silent in-app updates: close the running copy, replace, start again
CloseApplications=yes
RestartApplications=yes
UninstallDisplayIcon={app}\TGTrader.exe
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\TGTrader\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\TGTrader"; Filename: "{app}\TGTrader.exe"
Name: "{group}\Uninstall TGTrader"; Filename: "{uninstallexe}"
Name: "{autodesktop}\TGTrader"; Filename: "{app}\TGTrader.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\TGTrader.exe"; Description: "Run TGTrader"; Flags: nowait postinstall skipifsilent
