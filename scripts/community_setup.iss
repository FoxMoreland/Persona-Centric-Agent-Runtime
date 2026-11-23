#ifdef FieldTest
  #define DisplayName "PCAR 社区测试版"
  #define ProgramFolder "PCAR Community Test"
  #define CandidateName "PCAR-Community-v1.1.1-Windows-x64-Test-Setup"
  #define ProductVersion "1.1.1 Community"
  #define FileVersion "1.1.1.0"
#else
  #define DisplayName "PCAR 社区版"
  #define ProgramFolder "PCAR Community"
  #define CandidateName "PCAR-Community-v1.1.1-Windows-x64-Setup"
  #define ProductVersion "1.1.1"
  #define FileVersion "1.1.1.0"
#endif

[Setup]
#ifdef FieldTest
AppId={{BB15DDF3-7034-40E7-9725-2F1C97E1B988}
#else
AppId={{24615D1B-5CCF-4F72-AE9B-1FFB9D1D9677}
#endif
AppName={#DisplayName}
AppVersion={#ProductVersion}
VersionInfoVersion={#FileVersion}
DefaultDirName={localappdata}\Programs\{#ProgramFolder}
DefaultGroupName={#DisplayName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
SetupArchitecture=x64
MinVersion=10.0.22000
OutputBaseFilename={#CandidateName}
Compression=lzma2/fast
SolidCompression=no
UninstallDisplayIcon={app}\PCAR.exe

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#Payload}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#WebView2}"; DestName: "WebView2Runtime.exe"; Flags: dontcopy nocompression

[Icons]
Name: "{group}\{#DisplayName}"; Filename: "{app}\PCAR.exe"

[Run]
Filename: "{app}\PCAR.exe"; Description: "打开 {#DisplayName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
#ifndef FieldTest
Filename: "{app}\python\python.exe"; Parameters: "-E -s -B -m persona_runtime.deepseek_browser_bridge unregister community"; WorkingDir: "{app}"; Flags: runhidden skipifdoesntexist; RunOnceId: "PCARBrowserBridge"
#endif

#ifdef FieldTest
[UninstallDelete]
Type: files; Name: "{app}\field-test-install.txt"
#endif

[Code]
#ifdef FieldTest
var
  WebViewInstallState: String;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SaveStringToFile(ExpandConstant('{app}\field-test-install.txt'), WebViewInstallState, False);
end;
#endif

function ValidPv(RootKey: Integer): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(RootKey,
    'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version)
    and (Version <> '') and (Version <> '0.0.0.0');
end;

function WebView2Installed(): Boolean;
begin
  Result := ValidPv(HKLM32) or ValidPv(HKCU) or ValidPv(HKLM64);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
#ifdef FieldTest
  WebViewInstallState := 'existing';
#endif
  if WebView2Installed() then
    Exit;
  ExtractTemporaryFile('WebView2Runtime.exe');
  if not Exec(ExpandConstant('{tmp}\WebView2Runtime.exe'), '/silent /install', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode) then begin
    Result := '无法启动随包附带的 Microsoft Edge WebView2 Runtime 安装。';
    Exit;
  end;
  if ResultCode = 3010 then begin
    NeedsRestart := True;
#ifdef FieldTest
    WebViewInstallState := 'restart';
#endif
  end else if not WebView2Installed() then
    Result := 'Microsoft Edge WebView2 Runtime 安装未完成。退出码 ' + IntToStr(ResultCode)
#ifdef FieldTest
  else
    WebViewInstallState := 'bundled'
#endif
  ;
end;
