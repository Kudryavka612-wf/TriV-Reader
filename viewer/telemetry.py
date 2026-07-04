"""未対応フォーム機能の「匿名・オプトイン」テレメトリ（送信はクライアント側のみ）。

送るのは A（未対応の関数名＋フォーム側テンプレ定義＋アプリ/ライブラリ版＋
フォーム指紋）だけ。利用者が入力した値・氏名・住所・口座番号や、ファイル名
などの個人情報は一切含めない（build_payload を参照）。

前提（すべて満たす時だけ送る）:
  1. 配布者が version.TELEMETRY_URL に受け口 URL を設定している
  2. ユーザーが明示的に同意している（既定オフ）
  3. 実際に未対応フォームを開いた
送信は非ブロッキング（デーモンスレッド）で、失敗しても無害に握りつぶす。
同一フォーム（指紋一致）の重複送信はしない。
"""
from __future__ import annotations

import hashlib
import json
import threading
import urllib.request

# QSettings のキー
CONSENT_KEY = "telemetry_consent"       # "yes" / "no" / 未設定=未決定
SENT_FINGERPRINTS_KEY = "telemetry_sent"  # 送信済みフォーム指紋のリスト
_SEND_TIMEOUT_S = 6
_MAX_REMEMBERED = 200  # 重複判定用に覚えておく指紋の上限


def fingerprint(doc_scripts: dict[str, str]) -> str:
    """文書レベル JS 全体から、フォームを一意に表す（個人情報を含まない）指紋。"""
    combined = "\n".join(f"{k}\n{v}" for k, v in sorted(doc_scripts.items()))
    return hashlib.sha256(combined.encode("utf-8", "replace")).hexdigest()


def build_payload(doc_scripts: dict[str, str], helper_names: list[str],
                  helper_sources: dict[str, str], app_version: str,
                  pymupdf_version: str) -> dict:
    """送信する匿名ペイロードを組み立てる。

    含めるのはフォーム由来のテンプレ定義と版・指紋のみ。ファイル名・入力値・
    氏名等の個人情報は一切含めない（呼び出し側もそれ以外を渡さないこと）。
    """
    return {
        "schema": 1,
        "kind": "govform_unsupported",
        "app_version": app_version or "",
        "pymupdf": pymupdf_version or "",
        "form_fingerprint": fingerprint(doc_scripts),
        "helpers": sorted(helper_names),
        # 未対応関数の“定義だけ”（実装追加の手がかり。入力値は含まれない）
        "sources": {n: helper_sources.get(n, "") for n in helper_names},
    }


def consent_state(settings) -> str:
    """"yes" / "no" / "" (未決定) を返す。"""
    return settings.value(CONSENT_KEY, "", str) or ""


def set_consent(settings, agreed: bool) -> None:
    settings.setValue(CONSENT_KEY, "yes" if agreed else "no")


def already_sent(settings, fp: str) -> bool:
    sent = settings.value(SENT_FINGERPRINTS_KEY, [], list) or []
    return fp in sent


def mark_sent(settings, fp: str) -> None:
    sent = list(settings.value(SENT_FINGERPRINTS_KEY, [], list) or [])
    if fp in sent:
        return
    sent.append(fp)
    if len(sent) > _MAX_REMEMBERED:
        sent = sent[-_MAX_REMEMBERED:]
    settings.setValue(SENT_FINGERPRINTS_KEY, sent)


def send_async(url: str, payload: dict) -> None:
    """payload を JSON で url に POST する（別スレッド・失敗は無害に無視）。"""
    if not url:
        return
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _worker() -> None:
        try:
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "User-Agent": "TriVReader-Telemetry"})
            urllib.request.urlopen(req, timeout=_SEND_TIMEOUT_S).close()
        except Exception:  # noqa: BLE001  - テレメトリは絶対にアプリを妨げない
            pass

    threading.Thread(target=_worker, daemon=True).start()
