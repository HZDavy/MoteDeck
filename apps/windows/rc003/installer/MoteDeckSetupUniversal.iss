; MoteDeck 万能安装包（救援/恢复用途）。
; 与正常安装包 MoteDeckSetup.iss 的区别：
;   - 不做旧版备份/恢复、不做启动校验、不做管理员账号检查、
;     不因旧进程未退出或上次安装残留而中止；
;   - 安装前先请求旧进程正常退出（沿用 stop-app.ps1 的退出契约），
;     随后按精确文件名强制结束残留进程（不含通配旧版主程序时
;     会先确认安装器自身文件名不会命中）；
;   - 直接覆盖安装，并清掉上次安装留下的升级残留
;     （.installing-previous 备份目录等，即用户卡在
;     "无法恢复上次安装保留的旧版" 提示的根源）。
; 代价：正在运行的 MoteDeck 会被结束，未保存的界面状态丢失；
; 覆盖安装失败时没有旧版可回滚，重跑一次本安装包即可。

#define AppName "MoteDeck"
#define AppPublisher "MoteDeck项目"
#define VersionFileHandle FileOpen(AddBackslash(SourcePath) + "..\src\motedeck\VERSION")
#if !VersionFileHandle
  #error "Cannot read src\motedeck\VERSION"
#endif
#define AppVersion Trim(FileRead(VersionFileHandle))
#expr FileClose(VersionFileHandle)
; 主程序文件名固定为 MoteDeck.exe，与正常安装包一致（含相同 AppId），
; 升级/救援互相覆盖时 Windows 托盘图标位置不会丢失。
#define AppExeName AppName + ".exe"
#define AppFolder "MoteDeck"
#define DistDir "..\dist\MoteDeck"

[Setup]
AppId={{B6E8B6F0-7B9B-4B7C-9E7E-3B7B2C6B0F5C}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\MoteDeck
DefaultGroupName=MoteDeck
UsePreviousGroup=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
SetupIconFile=..\src\motedeck\assets\icons\remote-mic.ico
WizardSmallImageFile=wizard-small.png
OutputBaseFilename=MoteDeckSetup-{#AppVersion}-universal
OutputDir=..\dist\installer
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
CloseApplications=no
RestartApplications=no

[Languages]
; 简体中文界面：与正常安装包使用同一份官方语言文件。
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\..\..\..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; DestName: "THIRD_PARTY_NOTICES.md"; Flags: ignoreversion
Source: "..\..\..\..\THIRD_PARTY_SOURCE.md"; DestDir: "{app}"; DestName: "THIRD_PARTY_SOURCE.md"; Flags: ignoreversion
Source: "..\..\..\..\THIRD_PARTY_LICENSES\*"; DestDir: "{app}\THIRD_PARTY_LICENSES"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\..\..\..\LICENSE.md"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\..\..\..\COPYRIGHT.md"; DestDir: "{app}"; DestName: "COPYRIGHT.txt"; Flags: ignoreversion
Source: "..\ATTRIBUTION.md"; DestDir: "{app}"; DestName: "ATTRIBUTION.txt"; Flags: ignoreversion
; stop-app.ps1 与退出契约标记与正常安装包保持一致：
; 卸载流程和"停止"快捷方式依赖 {app}\stop-app.ps1 常驻。
Source: "stop-app.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "stop-app.ps1"; DestDir: "{tmp}"; Flags: dontcopy
Source: "application-exit-contract-v1.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\src\motedeck\assets\icons\remote-mic.ico"; DestDir: "{app}"; DestName: "MoteDeck.ico"; Flags: ignoreversion

[InstallDelete]
; 万能包不做备份，直接清掉上次安装留下的全部升级残留
; （含正常安装包用于自救的备份目录），不再有可恢复对象。
Type: filesandordirs; Name: "{app}\.installing-previous"
Type: files; Name: "{app}\.installing-previous.state"
Type: files; Name: "{app}\{#AppExeName}.installing-previous"
; 主程序文件名固定化之前遗留的带版本号旧主程序，升级时统一清理。
Type: files; Name: "{app}\MoteDeck *.exe"
Type: files; Name: "{app}\MoteDeck *.exe.installing-previous"
Type: files; Name: "{app}\MiOpention* *.exe"
Type: files; Name: "{app}\RemoteMicRC003.exe"

[UninstallDelete]
; 卸载时兜底清理升级残留。
Type: filesandordirs; Name: "{app}\.installing-previous"
Type: files; Name: "{app}\.installing-previous.state"
Type: files; Name: "{app}\{#AppExeName}.installing-previous"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"
; 与正常安装包一致：不提供"开机自启"任务，由应用内选项控制。

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\MoteDeck.ico"
Name: "{group}\停止 {#AppName}"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\stop-app.ps1"" -AppPath ""{app}"" -ConfigRoot ""{localappdata}\{#AppFolder}"""; WorkingDir: "{app}"; Flags: runminimized
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\MoteDeck.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "--settings"; Description: "立即运行 {#AppName}"; Flags: postinstall nowait skipifsilent

[Code]
const
  ForceKillDelayMilliseconds = 500;

procedure RemoveOwnedLoginStartupValue;
var
  AppExecutable: String;
  CurrentCommand: String;
  ExpectedCommand: String;
  ExpectedQuotedCommand: String;
begin
  if not RegQueryStringValue(
    HKCU,
    'Software\Microsoft\Windows\CurrentVersion\Run',
    'RemoteMicRC003',
    CurrentCommand
  ) then
    exit;

  AppExecutable := ExpandConstant('{app}\{#AppExeName}');
  ExpectedCommand := AppExecutable + ' --background';
  ExpectedQuotedCommand := '"' + AppExecutable + '" --background';
  CurrentCommand := Trim(CurrentCommand);
  if (CompareText(CurrentCommand, ExpectedCommand) = 0) or
     (CompareText(CurrentCommand, ExpectedQuotedCommand) = 0) then
    RegDeleteValue(
      HKCU,
      'Software\Microsoft\Windows\CurrentVersion\Run',
      'RemoteMicRC003'
    );
end;

{ 请求旧进程按退出契约正常退出；不检查退出码，万能包不因此中止。 }
procedure RunStopScriptBestEffort(const ScriptPath: String);
var
  Parameters: String;
  PowerShellPath: String;
  ResultCode: Integer;
begin
  if not FileExists(ScriptPath) then
    exit;
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  Parameters :=
    '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ScriptPath + '" -AppPath "' + ExpandConstant('{app}') +
    '" -ConfigRoot "' +
    ExpandConstant('{localappdata}\{#AppFolder}') + '"';
  Exec(
    PowerShellPath,
    Parameters,
    ExpandConstant('{app}'),
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
end;

procedure KillImage(const ImageName, OwnImageName: String);
var
  ResultCode: Integer;
begin
  if CompareText(OwnImageName, ImageName) = 0 then
    exit;
  Exec(
    ExpandConstant('{sys}\taskkill.exe'),
    '/F /IM "' + ImageName + '"',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
end;

{ 兜底强制结束残留进程。全部使用精确映像名（或不会命中安装器自身
  文件名的模式），且跳过与安装器自身同名的匹配，避免自杀。 }
procedure ForceKillApplicationProcesses;
var
  OwnImageName: String;
begin
  OwnImageName := LowerCase(ExtractFileName(ExpandConstant('{srcexe}')));
  KillImage('{#AppExeName}', OwnImageName);
  KillImage('MoteDeckHidHelper.exe', OwnImageName);
  KillImage('RemoteMicRC003.exe', OwnImageName);
  KillImage('RemoteMicRC003HidHelper.exe', OwnImageName);
  { 带版本号的旧主程序（如 "MoteDeck 0.2.2.exe"）：模式带空格，
    命中要求文件名以 "MoteDeck " 开头；安装器文件名无空格。 }
  if Pos('motedeck ', OwnImageName) <> 1 then
    KillImage('MoteDeck *.exe', OwnImageName);
  if Pos('miopention', OwnImageName) <> 1 then
    KillImage('MiOpention*.exe', OwnImageName);
  Sleep(ForceKillDelayMilliseconds);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  ExtractTemporaryFile('stop-app.ps1');
  RunStopScriptBestEffort(ExpandConstant('{tmp}\stop-app.ps1'));
  ForceKillApplicationProcesses;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then
    exit;
  { 管理员按键组件尽力启用，失败不提示：可稍后在程序内“修复权限”重试。 }
  Exec(
    ExpandConstant('{app}\{#AppExeName}'),
    '--install-hid-helper',
    ExpandConstant('{app}'),
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
end;

function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  RunStopScriptBestEffort(ExpandConstant('{app}\stop-app.ps1'));
  ForceKillApplicationProcesses;
  if FileExists(ExpandConstant('{app}\{#AppExeName}')) then
    Exec(
      ExpandConstant('{app}\{#AppExeName}'),
      '--uninstall-hid-helper',
      ExpandConstant('{app}'),
      SW_HIDE,
      ewWaitUntilTerminated,
      ResultCode
    );
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveOwnedLoginStartupValue;
end;
