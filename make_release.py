"""配布用リリース（zip ＋ update.json）を作成する。

ビルド済みの onedir フォルダを zip 化し、自動更新用の update.json を出力する。
出力した 2 ファイルを GitHub Releases などに公開し、その zip の公開URLを
update.json の "url" に、update.json の公開URLをアプリの「更新元URL」に設定する。

使い方:
    python make_release.py [onedirフォルダ] [--base-url https://.../download]
                          [--notes "説明" | --notes-file notes.txt]
    省略時は %USERPROFILE%\\TriVReader_dist\\dist\\TriVReader を対象にする。
"""
from __future__ import annotations

import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from viewer.version import APP_VERSION, GITHUB_REPO


def _sha256(path: str) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    args = sys.argv[1:]
    base_url = ""
    notes = ""
    folder = None
    i = 0
    while i < len(args):
        if args[i] == "--base-url" and i + 1 < len(args):
            base_url = args[i + 1].rstrip("/"); i += 2
        elif args[i] == "--notes" and i + 1 < len(args):
            notes = args[i + 1]; i += 2
        elif args[i] == "--notes-file" and i + 1 < len(args):
            # 複数行のリリースノートはファイル渡しにする。コマンドラインに
            # 改行入りの文字列を通すと、シェルの引用でいくらでも壊れるため。
            with open(args[i + 1], encoding="utf-8") as f:
                notes = f.read().strip()
            i += 2
        else:
            folder = args[i]; i += 1

    if folder is None:
        folder = os.path.join(os.path.expanduser("~"), "TriVReader_dist", "dist", "TriVReader")
    if not os.path.isdir(folder):
        print("onedir フォルダが見つかりません:", folder, file=sys.stderr)
        print("先に build_exe.py でビルドするか、フォルダパスを指定してください。", file=sys.stderr)
        return 1

    out_dir = os.path.join(os.path.expanduser("~"), "TriVReader_release")
    os.makedirs(out_dir, exist_ok=True)
    zip_name = f"TriVReader-{APP_VERSION}.zip"
    zip_path = os.path.join(out_dir, zip_name)

    print("zip 作成中:", zip_path)
    base = os.path.dirname(folder)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                full = os.path.join(root, name)
                z.write(full, os.path.relpath(full, base))  # 先頭に TriVReader/ を含める

    digest = _sha256(zip_path)
    tag = f"v{APP_VERSION}"
    # base_url 未指定なら GITHUB_REPO からリリースの download URL を推定
    if not base_url and GITHUB_REPO.strip().strip("/"):
        repo = GITHUB_REPO.strip().strip("/")
        base_url = f"https://github.com/{repo}/releases/download/{tag}"
    manifest = {
        "version": APP_VERSION,
        "url": (f"{base_url}/{zip_name}" if base_url else f"<ここに {zip_name} の公開URL>"),
        "sha256": digest,
        "notes": notes,
    }
    mpath = os.path.join(out_dir, "update.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("=== 完了 ===")
    print("zip        :", zip_path)
    print("update.json:", mpath)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"""
GitHub Releases 公開手順:
  1. タグ {tag} でリリースを作成
  2. このリリースに次の2ファイルを添付:
       - {zip_name}
       - update.json
  3. version.py の GITHUB_REPO を "オーナー/リポジトリ" に設定（アプリ側の更新元）
  ※ アプリは https://github.com/<repo>/releases/latest/download/update.json を見ます
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
