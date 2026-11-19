"""アプリのバージョンと更新元の設定。リリースのたびに APP_VERSION を上げる。"""

APP_VERSION = "1.12.2"

# ── GitHub Releases を使う場合（推奨）─────────────────────────────
# ここに "オーナー名/リポジトリ名" を設定するだけで更新元URLが決まります。
# 例: GITHUB_REPO = "zhelangsenxia720/trivreader"
# （リポジトリは public 推奨。private はリリース資産のDLに認証が必要で非対応）
GITHUB_REPO = "Kudryavka612-wf/TriV-Reader"

# 任意: 上の自動URLを使わず、update.json のURLを直接指定したい場合に設定。
# 例: "https://example.com/trivreader/update.json"（https のみ。署名は同じく要る）
UPDATE_MANIFEST_URL = ""

# ── 更新の署名を確かめる公開鍵（Ed25519・32 バイトを base64 にした 44 文字）────
# ★★ 仮の値（未設定）。tools/make_signing_key.py が表示する 1 行をそのまま貼ること ★★
# 空のあいだは署名を確かめない（これまでどおりの動き。最初の署名つきの版を配るため）。
# 貼ったあとは、配布元の秘密鍵（GitHub の Secrets の UPDATE_SIGNING_KEY）で署名した
# update.json しか信じない（署名が無い・合わない更新は入れない。updater.check）。
# 形の崩れた値を貼ると、どの更新も入れなくなる（安全側）。CI の署名の工程が、
# Secrets の秘密鍵とここの公開鍵が対になっているかを確かめる（tools/sign_update.py）。
UPDATE_PUBLIC_KEY = "S8TOBMZ53UCTyNeFv6CF1GAtgDNAnXtgPmlTwpCQIFY="

# ── 匿名テレメトリ（未対応フォーム機能の収集）の送信先 ────────────────
# 空 = 送信は一切行わず、同意ダイアログも出さない（事務所の決定: 事務所の外へは
# 何も送らない。設定の telemetry_url でも上書きできない。mw_update._telemetry_url）。
TELEMETRY_URL = ""


def github_manifest_url() -> str:
    """GITHUB_REPO から update.json の『最新』固定URLを組み立てる。"""
    repo = GITHUB_REPO.strip().strip("/")
    if not repo or "/" not in repo:
        return ""
    return f"https://github.com/{repo}/releases/latest/download/update.json"


# update.json の形式（例）:
# {
#   "version": "1.1.0",
#   "url": "https://github.com/<repo>/releases/download/v1.1.0/TriVReader-1.1.0.zip",
#   "sha256": "<zip の SHA256（UPDATE_PUBLIC_KEY が入っているときは必須）>",
#   "notes": "不具合修正と高速化"
# }
# 同じリリースに update.json.sig（update.json のバイト列への Ed25519 署名・base64）を置く。
