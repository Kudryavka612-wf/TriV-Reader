/**
 * TriV-Reader の「未対応フォーム機能」匿名テレメトリ受信スクリプト（Google Apps Script）。
 *
 * 使い方:
 *   1. Google スプレッドシートを新規作成する。
 *   2. 拡張機能 > Apps Script を開き、このファイルの内容を丸ごと貼り付けて保存。
 *   3. 右上の「デプロイ > 新しいデプロイ」→ 種類「ウェブアプリ」を選択。
 *        - 実行するユーザー: 自分
 *        - アクセスできるユーザー: 全員
 *      でデプロイし、発行された /exec の URL を viewer/version.py の
 *      TELEMETRY_URL に設定する。
 *   4. 初回デプロイ時に権限の承認を求められるので許可する
 *      （このスクリプト自身のスプレッドシートに書き込む権限のみ）。
 *
 * 受け取る JSON（アプリの viewer/telemetry.py build_payload と対応）:
 *   { schema, kind, app_version, pymupdf, form_fingerprint, helpers: [...],
 *     sources: { 関数名: ソース, ... } }
 * 個人情報・入力値・ファイル名は含まれない設計（アプリ側の telemetry.py 参照）。
 *
 * シートには1行1レポートで追記する。同じフォーム（form_fingerprint）から
 * 複数ユーザーが送ってきても構わない（「何人が同じ未対応フォームに当たったか」
 * が分かるほうがむしろ有用なため、サーバー側では重複排除しない）。
 */

const SHEET_NAME = "reports";
const HEADER = [
  "受信日時", "schema", "kind", "app_version", "pymupdf",
  "form_fingerprint", "helpers", "sources_json", "raw_json",
];

function doPost(e) {
  try {
    const body = (e && e.postData && e.postData.contents) || "";
    const data = JSON.parse(body);

    // 最低限の形チェック（意図しない送信元からのゴミ書き込みを減らす）
    if (!data || data.kind !== "govform_unsupported") {
      return _ack(false, "unexpected payload");
    }

    const sheet = _getSheet();
    const helpers = Array.isArray(data.helpers) ? data.helpers.join(", ") : "";
    // 1セルに収まるよう安全側で切り詰める（Sheets の1セル上限は5万文字）
    const sourcesJson = _truncate(JSON.stringify(data.sources || {}), 45000);
    const rawJson = _truncate(body, 45000);

    sheet.appendRow([
      new Date(),
      data.schema || "",
      data.kind || "",
      data.app_version || "",
      data.pymupdf || "",
      data.form_fingerprint || "",
      helpers,
      sourcesJson,
      rawJson,
    ]);

    return _ack(true, "ok");
  } catch (err) {
    return _ack(false, String(err));
  }
}

function _getSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
  }
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADER);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

function _truncate(s, max) {
  if (s.length <= max) return s;
  return s.slice(0, max) + "...(truncated)";
}

function _ack(ok, message) {
  return ContentService
    .createTextOutput(JSON.stringify({ ok: ok, message: message }))
    .setMimeType(ContentService.MimeType.JSON);
}

/** デプロイ後、GET でアクセスした時の簡易確認用（ブラウザで開いて疎通確認できる）。 */
function doGet(e) {
  return ContentService
    .createTextOutput("TriV-Reader telemetry endpoint: OK (POST only)")
    .setMimeType(ContentService.MimeType.TEXT);
}
