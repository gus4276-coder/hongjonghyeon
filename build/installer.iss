; Inno Setup 6 — 관리자 권한 없이 사용자 폴더에 설치 (회사 PC 에서도 바로 설치 가능)
#define AppName "Hiplaza 회의록"
#define AppExe "HiplazaMeetingNotes.exe"
#ifndef AppVersion
  #define AppVersion "1.1.1"
#endif

[Setup]
AppId={{7E1B6A52-4C1D-4E7B-9A4E-5F2C1D8B9A31}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Hiplaza
DefaultDirName={localappdata}\Programs\HiplazaMeetingNotes
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=HiplazaMeetingNotes-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "추가 작업:"

[Files]
Source: "..\dist\HiplazaMeetingNotes\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 관리자 배포용: Setup.exe 와 같은 폴더에 defaults.json 을 두면 함께 설치됨 (기본 설정/용어집 일괄 배포)
Source: "{src}\defaults.json"; DestDir: "{app}"; Flags: external skipifsourcedoesntexist onlyifdoesntexist

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} 제거"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{#AppName} 실행"; Flags: nowait postinstall skipifsilent

[Code]
const WV2 = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasWebView2(): Boolean;
var v: String;
begin
  Result :=
    (RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WV2, 'pv', v) and (v <> '') and (v <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WV2, 'pv', v) and (v <> '') and (v <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\' + WV2, 'pv', v) and (v <> '') and (v <> '0.0.0.0'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var code: Integer;
begin
  if (CurStep = ssPostInstall) and (not HasWebView2()) then
  begin
    if MsgBox('화면 표시에 필요한 Microsoft Edge WebView2 런타임이 없습니다.' #13#10 +
              '다운로드 페이지를 열까요? (Windows 11 은 기본 설치되어 있습니다)', mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', 'https://developer.microsoft.com/microsoft-edge/webview2/#download-section', '', '', SW_SHOWNORMAL, ewNoWait, code);
  end;
end;
