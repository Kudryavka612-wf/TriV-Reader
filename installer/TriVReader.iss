; TriV-Reader — Inno Setup インストーラ スクリプト
;
; 通常は make_installer.py から呼ばれ、バージョン・ソースフォルダ・アイコンを
; /D 定義で受け取ります。単体で Inno Setup IDE から開く場合に備えて、
; 未定義のときは既定値（ビルド出力の標準パス）を使います。
;
; 方針: 管理者不要の「ユーザーインストール」。インストール先は
;   %LOCALAPPDATA%\Programs\TriV-Reader（PrivilegesRequired=lowest 時の {autopf}）。
; PDF 関連付けは associate_pdf.py と同じく HKCU に登録します。

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

; onedir ビルドの中身（TriVReader.exe と依存一式）が入ったフォルダ
#ifndef SourceDir
  #define SourceDir GetEnv("USERPROFILE") + "\TriVReader_dist\dist\TriVReader"
#endif

; セットアップ実行ファイルのアイコン（任意）
#ifndef AppIcon
  #define AppIcon GetEnv("USERPROFILE") + "\TriVReader_dist\_build\trivreader.ico"
#endif

; Setup.exe の出力先
#ifndef OutputDir
  #define OutputDir GetEnv("USERPROFILE") + "\TriVReader_release"
#endif

#ifndef MyAppName
  #define MyAppName "TriV-Reader"
#endif
#define MyAppPublisher "TriV-Reader"

; 次の 7 つ（MyAppName 〜 RunningMutexPrefix）は、試験用に別の値でコンパイルできるようにしてある（本物のインストール・
; 関連付け・設定を巻き込まずに、入れる→消すを通しで試すため）。配布版は既定値。
#ifndef MyAppGuid
  #define MyAppGuid "B7E9C3A1-5F2D-4E8B-9A47-2C1D6F0A8E33"
#endif
#ifndef MyAppExeName
  #define MyAppExeName "TriVReader.exe"
#endif
#ifndef MyProgId
  #define MyProgId "TriVReader.Document"
#endif
; アプリが実行時に作る場所: %LOCALAPPDATA%\<DataDirName> と HKCU\Software\<RegOrgKey>
#ifndef DataDirName
  #define DataDirName "trivreader"
#endif
#ifndef RegOrgKey
  #define RegOrgKey "trivreader"
#endif
; 起動中の目印（main.py の _SERVER_NAME。利用者名は実行時に足す）
#ifndef RunningMutexPrefix
  #define RunningMutexPrefix "TriVReader-SingleInstance-"
#endif

[Setup]
; AppId はアプリを一意に識別する固定値。更新時も変えないこと。
AppId={{{#MyAppGuid}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\TriV-Reader
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 管理者権限を要求しない（UAC なし・ユーザー領域へインストール）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutputDir}
OutputBaseFilename=TriVReader-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile={#AppIcon}
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "デスクトップにショートカットを作成する"; GroupDescription: "ショートカット:"
Name: "associate"; Description: ".pdf ファイルを TriV-Reader に関連付ける"; GroupDescription: "ファイルの関連付け:"

[Files]
; onedir ビルド一式をインストール先へコピー
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} をアンインストール"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; ── PDF 関連付け（HKCU・associate_pdf.py と同じ構成）──────────────
; ProgID（このアプリの「ファイルの種類」）。ファイルのアイコンは書類アイコン(pdf_doc.ico)
Root: HKCU; Subkey: "Software\Classes\{#MyProgId}"; ValueType: string; ValueData: "PDF ドキュメント"; Flags: uninsdeletekey; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\{#MyProgId}\DefaultIcon"; ValueType: string; ValueData: "{app}\pdf_doc.ico,0"; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\{#MyProgId}\shell\open\command"; ValueType: string; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: associate
; 「プログラムから開く」候補に追加
Root: HKCU; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; ValueName: "{#MyProgId}"; ValueData: ""; Flags: uninsdeletevalue; Tasks: associate
; Applications にも登録（「別のプログラムを選択」「既定のアプリ」に表示）。
; FriendlyAppName が無いと Windows は実行ファイル名をそのまま表示するため、
; 「TriVReader.exe」ではなく「TriV-Reader」と表示されるよう明示する。
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#MyAppName}"; Flags: uninsdeletekey; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\shell\open\command"; ValueType: string; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Flags: uninsdeletekey; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\DefaultIcon"; ValueType: string; ValueData: "{app}\{#MyAppExeName},0"; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".pdf"; ValueData: ""; Tasks: associate

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "TriV-Reader を起動する"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 自動更新で後から増えた部品は、インストーラーの記録に無いので放っておくと残る。
; PyInstaller の置き場（_internal）はこのアプリの物だけなので丸ごと消す。
; {app} そのものは、利用者が自分のファイルを置いているかもしれないので消さない
; （空なら Inno Setup が消す）。
Type: filesandordirs; Name: "{app}\_internal"
; 自動更新が控えた「前の版」（アプリ 1 つぶんの大きさ）と、起動の記録
Type: filesandordirs; Name: "{localappdata}\{#DataDirName}\previous_version"
Type: files; Name: "{localappdata}\{#DataDirName}\previous_version.json"
Type: files; Name: "{localappdata}\{#DataDirName}\startup_pending.json"
Type: files; Name: "{localappdata}\{#DataDirName}\skip_update.txt"
Type: dirifempty; Name: "{localappdata}\{#DataDirName}"

[Code]
const
  SHCNE_ASSOCCHANGED = $08000000;
  SHCNF_IDLIST = $0000;

procedure SHChangeNotify(wEventId: Integer; uFlags: Cardinal; dwItem1, dwItem2: Cardinal);
  external 'SHChangeNotify@shell32.dll stdcall';

procedure CurStepChanged(CurStep: TSetupStep);
begin
  // インストール完了後、シェルに関連付け変更を通知（即座に反映させる）
  if CurStep = ssPostInstall then
    SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, 0, 0);
end;

var
  RemoveUserData: Boolean;

function RunningMutexName(): String;
begin
  Result := '{#RunningMutexPrefix}' + GetUserNameString();
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  // 起動したまま（トレイに隠れている場合を含む）消すと、使用中のファイルが残る。
  // 静かな実行（/SILENT）では聞けないので、そのまま進める。
  if UninstallSilent() or not CheckForMutexes(RunningMutexName()) then
    Exit;
  while CheckForMutexes(RunningMutexName()) do
  begin
    if MsgBox('TriV-Reader が起動しています。' + #13#10 +
              '画面右下の通知領域（トレイ）にあるアイコンを右クリックして［終了］を選んでから、' +
              '［OK］を押してください。', mbInformation, MB_OKCANCEL) = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

// 実行中の画面から「既定のPDFアプリに設定」で足された関連付けは、インストーラーの
// 記録に無いので、消さないとアンインストールの後も「アプリの選択」に残る。
// 別の場所の TriV-Reader を指しているものは、こちらの物ではないので消さない。
function PointsHere(const CommandKey: String): Boolean;
var
  Cmd: String;
begin
  Result := RegQueryStringValue(HKCU, CommandKey, '', Cmd) and
            (Pos(Lowercase(ExpandConstant('{app}')), Lowercase(Cmd)) > 0);
end;

procedure RemoveRuntimeAssociation();
begin
  if PointsHere('Software\Classes\{#MyProgId}\shell\open\command') then
  begin
    RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Classes\{#MyProgId}');
    RegDeleteValue(HKCU, 'Software\Classes\.pdf\OpenWithProgids', '{#MyProgId}');
  end;
  if PointsHere('Software\Classes\Applications\{#MyAppExeName}\shell\open\command') then
    RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Classes\Applications\{#MyAppExeName}');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    // 設定や入力履歴は、入れ直したときに引き継げるよう、既定では残す
    RemoveUserData := False;
    if not UninstallSilent() then
      RemoveUserData := MsgBox('TriV-Reader の設定・入力履歴・OCR の言語データ' + #13#10 +
        '（この PC のこの利用者の分）も削除しますか？' + #13#10#13#10 +
        'あとで入れ直すときに引き継ぎたい場合は［いいえ］を選んでください。',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    RemoveRuntimeAssociation();
    if RemoveUserData then
    begin
      DelTree(ExpandConstant('{localappdata}\{#DataDirName}'), True, True, True);
      RegDeleteKeyIncludingSubkeys(HKCU, 'Software\{#RegOrgKey}');
    end;
    SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, 0, 0);
  end;
end;
