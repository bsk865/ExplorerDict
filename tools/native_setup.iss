; Native-only preview installer. User data is never included in the file manifest.
#ifndef PackageDir
  #error PackageDir must point to the audited native distribution
#endif
#ifndef OutputDir
  #error OutputDir must be specified
#endif
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{4EF8E5E1-C970-4BE0-9FA5-627AD8E30E66}
AppName=探索词典原生版
AppVersion={#AppVersion}
AppPublisher=ExplorerDict
AppPublisherURL=https://github.com/bsk865/ExplorerDict
DefaultDirName={localappdata}\Programs\ExplorerDictNative
DefaultGroupName=探索词典原生版
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=ExplorerDict-Native-{#AppVersion}-Setup
UninstallDisplayIcon={app}\探索词典-原生版.exe
SetupIconFile=..\assets\app.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no
Uninstallable=yes
CreateUninstallRegKey=not IsolatedCheck

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked

[Dirs]
Name: "{app}\data"; Flags: uninsneveruninstall

[Files]
Source: "{#PackageDir}\探索词典-原生版.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PackageDir}\探索词典.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PackageDir}\_native_runtime\*"; DestDir: "{app}\_native_runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PackageDir}\_runtime\*"; DestDir: "{app}\_runtime"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\探索词典原生版"; Filename: "{app}\探索词典-原生版.exe"; WorkingDir: "{app}"; Check: not IsolatedCheck
Name: "{userdesktop}\探索词典原生版"; Filename: "{app}\探索词典-原生版.exe"; WorkingDir: "{app}"; Tasks: desktopicon; Check: not IsolatedCheck

[Run]
Filename: "{app}\探索词典-原生版.exe"; Description: "打开探索词典原生版"; Flags: nowait postinstall skipifsilent unchecked

[Code]
function IsolatedCheck: Boolean;
begin
  Result := ExpandConstant('{param:ISOLATEDCHECK|0}') = '1';
end;
