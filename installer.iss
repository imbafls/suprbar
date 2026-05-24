; Inno Setup script for supr.bar — wraps the PyInstaller folder bundle
; into a single suprbar-setup.exe.
;
; Build:
;   1. pyinstaller --clean suprbar.spec   (produces dist/suprbar/)
;   2. iscc installer.iss                  (produces dist/suprbar-setup-X.Y.Z.exe)

#define MyAppName      "supr.bar"
#define MyAppVersion   "0.5.1"
#define MyAppPublisher "Omer Taji"
#define MyAppURL       "https://github.com/imbafls/suprbar"
#define MyAppExeName   "suprbar.exe"

[Setup]
AppId={{8E7C42F1-3A1D-4F0E-9C72-7B3A6E1E5C2D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=LICENSE
OutputDir=dist
OutputBaseFilename=suprbar-setup-{#MyAppVersion}
SetupIconFile=suprbar\static\brand\suprbar.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
VersionInfoCompany={#MyAppPublisher}
VersionInfoProductName={#MyAppName}
VersionInfoVersion={#MyAppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startup";  Description: "Launch supr.bar when I sign in to Windows"; GroupDescription: "Additional options:"; Flags: unchecked
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional options:"; Flags: unchecked

[Files]
Source: "dist\suprbar\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "suprbar"; \
  ValueData: """{app}\{#MyAppExeName}"""; \
  Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch supr.bar"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove autorun key (handled by uninsdeletevalue above) but leave user
; config + session retros at %APPDATA%\suprbar and %USERPROFILE%\.suprbar
; intact — uninstall should not destroy user data.
