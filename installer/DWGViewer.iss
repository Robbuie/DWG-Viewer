; Inno Setup script for DWG Viewer
;
; Per-user install (%LOCALAPPDATA%\Programs\DWG Viewer) on purpose:
; the in-app updater runs this installer silently, and a Program Files
; install would raise a UAC prompt on every update.
;
; Build:  ISCC.exe /DAppVersion=1.0.0 installer\DWGViewer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName    "DWG Viewer"
#define AppExeName "DWG Viewer.exe"
#define AppPublisher "Encore"

[Setup]
; Keep this GUID forever — it is how Windows and the updater recognise
; an existing install and upgrade it in place instead of duplicating it.
AppId={{7C4E1F2A-9B3D-4A6E-8D51-2F8A6C4B1E93}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist_installer
OutputBaseFilename=DWGViewer-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; Let the silent updater shut the running app down and restart it.
CloseApplications=yes
RestartApplications=yes
SetupLogging=yes
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "..\dist\DWG Viewer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";              Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}";    Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";        Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; Only offered in interactive installs; silent update runs use
; /RESTARTAPPLICATIONS instead.
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Thumbnail cache — regenerable, no reason to leave it behind.
Type: filesandordirs; Name: "{localappdata}\DWGViewer"
