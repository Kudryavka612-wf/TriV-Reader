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

#define MyAppName "TriV-Reader"
#define MyAppPublisher "TriV-Reader"
#define MyAppExeName "TriVReader.exe"
#define MyProgId "TriVReader.Document"

[Setup]
; AppId はアプリを一意に識別する固定値。更新時も変えないこと。
AppId={{B7E9C3A1-5F2D-4E8B-9A47-2C1D6F0A8E33}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\TriV-Reader
DefaultGroupName=TriV-Reader
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
Name: "{group}\TriV-Reader"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\TriV-Reader をアンインストール"; Filename: "{uninstallexe}"
Name: "{autodesktop}\TriV-Reader"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

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
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#MyAppName}"; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\shell\open\command"; ValueType: string; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Flags: uninsdeletekey; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\DefaultIcon"; ValueType: string; ValueData: "{app}\{#MyAppExeName},0"; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".pdf"; ValueData: ""; Tasks: associate

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "TriV-Reader を起動する"; Flags: nowait postinstall skipifsilent

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

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, 0, 0);
end;
