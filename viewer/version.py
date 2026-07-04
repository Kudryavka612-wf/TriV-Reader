"""アプリのバージョンと更新元の設定。リリースのたびに APP_VERSION を上げる。"""

APP_VERSION = "1.2.8"

# ── GitHub Releases を使う場合（推奨）─────────────────────────────
# ここに "オーナー名/リポジトリ名" を設定するだけで更新元URLが決まります。
# 例: GITHUB_REPO = "zhelangsenxia720/trivreader"
# （リポジトリは public 推奨。private はリリース資産のDLに認証が必要で非対応）
GITHUB_REPO = "zhelangsenxia720-source/TriV-Reader"

# 任意: 上の自動URLを使わず、update.json のURLを直接指定したい場合に設定。
# 例: "https://example.com/trivreader/update.json"
UPDATE_MANIFEST_URL = ""


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
#   "sha256": "<zip の SHA256（任意・あれば検証）>",
#   "notes": "不具合修正と高速化"
# }
