; Inno Setup script for the EspSync Windows installer.
;
; Build the app first (pyinstaller packaging/espsync.spec), then:
;   iscc /DMyAppVersion=0.2.0 /DMyNumericVersion=0.2.0 packaging\installer.iss
; The installer is written to dist\EspSync-Setup-<version>.exe.

#define MyAppName "EspSync"
#define MyAppExe "EspSync.exe"
#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif
#ifndef MyNumericVersion
  #define MyNumericVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir AddBackslash(SourcePath) + "..\dist\EspSync"
#endif

[Setup]
; Never change AppId: it is how upgrades find the existing installation.
AppId={{70B4A519-FC75-4D17-8C39-674B00CAF0C4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=EspSync
AppPublisherURL=https://github.com/Alli1223/EspSync
AppSupportURL=https://github.com/Alli1223/EspSync/issues
VersionInfoVersion={#MyNumericVersion}
VersionInfoProductName={#MyAppName}
VersionInfoDescription={#MyAppName} installer
; Per-user install by default (no admin prompt); the wizard offers all users.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#SourcePath}\..\dist
OutputBaseFilename=EspSync-Setup-{#MyAppVersion}
SetupIconFile={#SourcePath}\espsync.ico
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Close a running EspSync (it restores the lights on exit) before replacing files.
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "Start EspSync when I sign in (in the system tray)"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Clear the previous version's bundled runtime so stale libraries never linger.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Parameters: "--minimized"; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent runasoriginaluser

; Settings in %APPDATA%\EspSync are kept on uninstall so a reinstall keeps your room layout.
