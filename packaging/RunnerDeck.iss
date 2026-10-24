; RunnerDeck per-user installer (docs/m4-design.md §9): %LOCALAPPDATA%\Programs\RunnerDeck,
; no administrator rights. Built by scripts/build.ps1 after PyInstaller:
;   ISCC.exe /DAppVersion=1.0.0 /DSourceDir=<repo>\dist\RunnerDeck /DOutputDir=<repo>\dist RunnerDeck.iss
; Uninstalling removes the launch-on-login entry only if it points here, keeps the user's data (settings, runner directories, the PAT in Credential
; Manager) and never touches service-mode runners: they do not depend on RunnerDeck.

#ifndef AppVersion
  #error Pass the version: ISCC /DAppVersion=x.y.z RunnerDeck.iss
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\RunnerDeck"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef FileVersion
  ; VersionInfoVersion must be numeric: build.ps1 passes x.y.z without a pre-release suffix
  #define FileVersion AppVersion
#endif

[Setup]
; Never change AppId: upgrades and the uninstall entry are keyed on it.
AppId={{8F2B6C1E-5D3A-4B7E-9C21-6A4E2D9F0B13}
AppName=RunnerDeck
AppVersion={#AppVersion}
AppVerName=RunnerDeck {#AppVersion}
AppPublisher=nablejohne
AppPublisherURL=https://github.com/nablejohne/RunnerDeck
AppSupportURL=https://github.com/nablejohne/RunnerDeck/issues
AppUpdatesURL=https://github.com/nablejohne/RunnerDeck/releases
VersionInfoVersion={#FileVersion}
PrivilegesRequired=lowest
DefaultDirName={userpf}\RunnerDeck
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; RunnerDeck.exe holds this mutex while it runs (app/single_instance.py): setup and the
; uninstaller ask the user to quit it first. Not the Restart Manager: closing RunnerDeck that
; way would stop the user's runners without asking.
AppMutex=RunnerDeckAppMutex
CloseApplications=no
OutputDir={#OutputDir}
OutputBaseFilename=RunnerDeck-{#AppVersion}-setup
SetupIconFile=..\src\runnerdeck\ui\resources\runnerdeck.ico
UninstallDisplayIcon={app}\RunnerDeck.exe
UninstallDisplayName=RunnerDeck
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=auto

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
; jrsoftware/issrc Files/Languages/Unofficial/ChineseSimplified.isl (is-6_7_1), with a UTF-8 BOM
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[CustomMessages]
english.KeptData=RunnerDeck has been removed. Your settings, runner directories and the saved PAT are kept, and runners installed as Windows services keep running. To remove a runner, reinstall RunnerDeck and use "Remove" there.
chinesesimplified.KeptData=RunnerDeck 已卸载。你的设置、runner 目录和保存的 PAT 都保留着，以 Windows 服务运行的 runner 也仍在运行。要移除 runner，请重新安装 RunnerDeck 后在里面使用“移除”。

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\RunnerDeck"; Filename: "{app}\RunnerDeck.exe"
Name: "{userdesktop}\RunnerDeck"; Filename: "{app}\RunnerDeck.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\RunnerDeck.exe"; Description: "{cm:LaunchProgram,RunnerDeck}"; Flags: nowait postinstall skipifsilent

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

{ RunnerDeck writes HKCU\...\Run\RunnerDeck itself (launch on login). Remove it only when it
  starts this installation: a zip copy elsewhere keeps its own entry. }
procedure RemoveOwnRunValue();
var
  Command: String;
begin
  if RegQueryStringValue(HKCU, RunKey, 'RunnerDeck', Command) and
     (Pos(Lowercase(ExpandConstant('{app}\RunnerDeck.exe')), Lowercase(Command)) > 0) then
    RegDeleteValue(HKCU, RunKey, 'RunnerDeck');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveOwnRunValue();
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
    MsgBox(CustomMessage('KeptData'), mbInformation, MB_OK);
end;
