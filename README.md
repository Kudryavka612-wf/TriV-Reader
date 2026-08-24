# TriV-Reader

PySide6 + PyMuPDF 製の自作 PDF ビューワー / エディタ（Windows）。

閲覧・連続スクロール・回転・注釈（ハイライト/ペン/図形/テキスト/付箋/下線/取消線/墨消し）・
ページ操作（分割/統合/抽出/削除/並べ替え/白紙挿入/複製/トリミング）・しおり・検索・
ページ番号・透かし・ヘッダー/フッター・OCR・画像変換・パスワード保護・タブ表示・
ダーク/ライト・ポータブル対応・自動更新 などに対応。

- 設定ダイアログ（`Ctrl+,` / リボンの「設定」）に全般・表示・注釈・保存・更新・
  詳細をまとめてあります。起動時の復元、注釈の既定スタイルと作成者名、保存時の
  バックアップ、自動保存などもここから。
- 印刷はベクター出力が既定です。プリンタの解像度でそのまま出るので文字がくっきり
  します（図版が崩れる PDF 向けに画像化 300/600dpi も選べます）。
- マウスジェスチャー: ページの上で右ボタンを押しながら
  ↓ タブを閉じる / ← 前のタブ / → 次のタブ / ↑ 先頭ページ。
- 文字選択はダブルクリックで語、トリプルクリックで行を選べます。

## 実行（ソースから）
```powershell
pip install -r requirements.txt
python main.py
```

## ビルド / 配布 / 自動更新
[BUILD.md](BUILD.md) を参照。

- 通常ビルド: `python build_exe.py`
- 軽量版（OCRなし）: `python build_exe.py --lite`
- ポータブル版: `python build_exe.py --portable`
- 1コマンド公開（要 GitHub CLI）: `python release.py --notes "更新内容"`

## 自動リリース（GitHub Actions）
`viewer/version.py` の `APP_VERSION` を上げてコミットし、`vX.Y.Z` タグを push すると
クラウドで自動ビルドされ、GitHub Releases に公開されます（`.github/workflows/release.yml`）。
