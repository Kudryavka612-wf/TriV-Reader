"""1コマンドでリリース: ビルド → zip/update.json 生成 → GitHub へ公開。

事前準備（最初の一度だけ）:
    1) GitHub CLI を導入:  winget install GitHub.cli
    2) ログイン:           gh auth login   （ブラウザで認証、Enter連打でOK）
    3) viewer/version.py の GITHUB_REPO を "オーナー/リポジトリ" に設定

毎回の手順:
    1) viewer/version.py の APP_VERSION を上げる（例 1.0.0 → 1.1.0）
    2) python release.py --notes "更新内容"
       （ポータブル版なら  python release.py --portable --notes "..."）

これだけで GitHub にリリースが作成され、利用者の「ヘルプ > 更新を確認」で
自動更新されます。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from viewer.version import APP_VERSION, GITHUB_REPO


def main() -> int:
    argv = sys.argv[1:]
    if "--local-publish" not in argv:
        print("release.py は使いません。viewer/version.py の APP_VERSION を上げてコミットし、\n"
              "注釈付きタグ（git tag -a vX.Y.Z -m ...）を push すると、GitHub Actions が\n"
              "テスト → ビルド → exe の起動確認 → 公開まで行います（BUILD.md を参照）。\n"
              "手元から公開したいときだけ --local-publish を付けてください。")
        return 2
    argv = [a for a in argv if a != "--local-publish"]
    notes = "更新"
    if "--notes" in argv:
        i = argv.index("--notes")
        if i + 1 < len(argv):
            notes = argv[i + 1]
    passthrough = [a for a in ("--portable", "--lite", "--onefile") if a in argv]
    lite = "--lite" in passthrough

    repo = GITHUB_REPO.strip().strip("/")
    if not repo or "/" not in repo:
        print("エラー: viewer/version.py の GITHUB_REPO を \"オーナー/リポジトリ\" に"
              "設定してください。", file=sys.stderr)
        return 1

    gh = shutil.which("gh")
    if not gh:
        print("GitHub CLI(gh) が見つかりません。次で導入してください:\n"
              "  winget install GitHub.cli\n  gh auth login\n"
              "（gh が無くても下のビルド/生成は実行し、ファイルは作成します）")

    py = sys.executable
    home = os.path.expanduser("~")
    name = "TriVReader_Lite" if lite else "TriVReader"
    out_root = os.path.join(home, "TriVReader_Lite_dist" if lite else "TriVReader_dist")
    folder = os.path.join(out_root, "dist", name)
    rel_dir = os.path.join(home, "TriVReader_release")
    zip_path = os.path.join(rel_dir, f"TriVReader-{APP_VERSION}.zip")
    manifest = os.path.join(rel_dir, "update.json")
    tag = f"v{APP_VERSION}"

    print(f"=== リリース {tag} を作成します（repo: {repo}）===")
    print("[1/3] ビルド...")
    subprocess.run([py, os.path.join(HERE, "build_exe.py"), *passthrough],
                   check=True, cwd=HERE)
    print("[2/3] zip / update.json 生成...")
    subprocess.run([py, os.path.join(HERE, "make_release.py"), folder, "--notes", notes],
                   check=True, cwd=HERE)

    if not gh:
        print("\ngh が無いため自動公開はスキップしました。")
        print("手動公開する場合は次の2ファイルを GitHub のリリース(タグ "
              f"{tag})に添付してください:\n  {zip_path}\n  {manifest}")
        return 0

    print(f"[3/3] GitHub へ公開（タグ {tag}）...")
    create = subprocess.run(
        [gh, "release", "create", tag, zip_path, manifest,
         "--repo", repo, "--title", tag, "--notes", notes],
    )
    if create.returncode != 0:
        # 既に同じタグのリリースがあれば、資産を上書きアップロード
        print("既存リリースへ上書きアップロードを試みます...")
        subprocess.run(
            [gh, "release", "upload", tag, zip_path, manifest,
             "--repo", repo, "--clobber"],
            check=True,
        )
    print(f"\n=== 完了 === https://github.com/{repo}/releases/tag/{tag}")
    print("利用者は「ヘルプ > 更新を確認」で自動更新されます。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
