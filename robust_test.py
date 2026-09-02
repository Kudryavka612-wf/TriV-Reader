"""実運用で起きる失敗ケースの検証。

- 上書き保存の置き換えに失敗しても、開いている文書と未保存の編集を失わない
  （他アプリがファイルを掴んでいる、OneDrive が同期中、などは Windows では
  珍しくない）
- 失敗した理由が日本語で分かる（英語の内部エラーをそのまま出さない）
- 一時ファイル(.tmp_save)を残さない
- サムネイルは見えているぶんだけ描く（大きな PDF で CPU とメモリを食わない）
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(__file__))
from viewer.document import PdfDocument
from viewer.thumbnail_list import ThumbnailList

TMP = tempfile.gettempdir()


def build(path: str, pages: int = 3) -> None:
    doc = fitz.open()
    for n in range(pages):
        p = doc.new_page(width=595, height=842)
        p.insert_text((72, 100), f"PAGE {n + 1}", fontsize=20)
        p.draw_rect(fitz.Rect(60, 200, 500, 700))
    doc.save(path)
    doc.close()


def main() -> int:  # noqa: C901
    app = QApplication(sys.argv)

    # --- 上書き保存の置き換えに失敗しても文書を壊さない ----------------
    src = os.path.join(TMP, "_rb_lock.pdf")
    build(src)
    doc = PdfDocument()
    doc.open(src)
    doc.rotate_page(0, 90)                 # 未保存の編集
    holder = open(src, "rb+")              # 他アプリが掴んでいる状態
    try:
        try:
            doc.save_as(src)
            raise AssertionError("掴まれているのに保存が成功してしまった")
        except RuntimeError as exc:
            msg = str(exc)
        assert "ほかのアプリ" in msg, msg          # 日本語で理由が分かる
        assert "WinError" not in msg, msg          # 内部エラーを見せない
        assert doc.is_open and doc.page_count == 3, "文書が使えなくなった"
        assert doc.rotation(0) == 90, "未保存の編集が消えた"
        assert doc.modified, "未保存フラグまで落ちている"
        assert not os.path.exists(src + ".tmp_save"), "一時ファイルが残っている"
        # ここが本題: 失敗しても「別の場所に名前を付けて保存」に逃げられる
        # （main_window の上書き失敗時のフォールバック経路）。
        rescue = os.path.join(TMP, "_rb_rescue.pdf")
        doc.save_as(rescue)
        with fitz.open(rescue) as chk:
            assert chk.load_page(0).rotation == 90, "逃がした先に編集が入っていない"
        os.remove(rescue)
        doc.path = src        # 元のファイルに戻して続きを確認する
        doc.modified = True
    finally:
        holder.close()
    # 掴んでいたアプリが閉じれば、そのまま保存し直せる
    doc.save_as(src)
    assert not doc.modified and doc.rotation(0) == 90
    doc.close()
    print("OK: 上書き保存が失敗しても文書と編集は残る（理由も日本語）")

    # --- 圧縮でも同じ ---------------------------------------------------
    doc = PdfDocument(); doc.open(src)
    holder = open(src, "rb+")
    try:
        try:
            doc.compress_to_advanced(src, max_dpi=150)
            raise AssertionError("掴まれているのに圧縮保存が成功してしまった")
        except RuntimeError as exc:
            assert "ほかのアプリ" in str(exc), exc
        assert doc.is_open and doc.page_count == 3
        assert not os.path.exists(src + ".tmp_save")
    finally:
        holder.close()
    doc.close()
    print("OK: 圧縮の上書きが失敗しても文書は無事")

    # --- 開けないファイルの理由が日本語で出る --------------------------
    bad = os.path.join(TMP, "_rb_bad.pdf")
    with open(bad, "wb") as f:
        f.write(b"%PDF-1.7 this is not a pdf")
    try:
        PdfDocument().open(bad)
        raise AssertionError("壊れた PDF が開けてしまった")
    except RuntimeError as exc:
        assert "PDF として読み込めません" in str(exc), exc

    empty = os.path.join(TMP, "_rb_empty.pdf")
    open(empty, "wb").close()
    try:
        PdfDocument().open(empty)
        raise AssertionError("空ファイルが開けてしまった")
    except RuntimeError as exc:
        assert "空です" in str(exc), exc

    try:
        PdfDocument().open(os.path.join(TMP, "_rb_nope.pdf"))
        raise AssertionError("存在しないファイルが開けてしまった")
    except RuntimeError as exc:
        assert "見つかりません" in str(exc), exc

    doc = PdfDocument(); doc.open(src)
    try:
        doc.save_as(os.path.join(TMP, "_rb_no_dir", "out.pdf"))
        raise AssertionError("存在しないフォルダに保存できてしまった")
    except RuntimeError as exc:
        assert "フォルダ" in str(exc), exc
    doc.close()
    print("OK: 壊れた/空/無いファイル・保存先なしを日本語で説明する")

    # --- サムネイルは見えているぶんだけ描く ----------------------------
    big = os.path.join(TMP, "_rb_big.pdf")
    build(big, pages=80)
    doc = PdfDocument(); doc.open(big)
    tl = ThumbnailList()
    tl.resize(190, 620)
    tl.show()
    app.processEvents()
    tl.load(doc)
    for _ in range(60):
        app.processEvents()
    drawn = len(tl._rendered)
    assert tl.count() == 80
    assert 0 < drawn < 40, f"見えていないページまで描いている: {drawn}/80"
    assert 0 in tl._rendered, "先頭が描かれていない"
    assert 79 not in tl._rendered, "末尾まで描いている"
    print(f"OK: 80 ページ中 {drawn} 枚だけ描画（見えている範囲＋前後）")

    # 下までスクロールすれば、その周辺が描かれる
    tl.scrollToBottom()
    app.processEvents()
    tl._queue_visible()
    for _ in range(80):
        app.processEvents()
    assert 79 in tl._rendered, "スクロール先が描かれない"
    print(f"OK: スクロールした先も描く（保持 {len(tl._rendered)} 枚）")

    # 上限を超えたら遠い行から捨てる
    tl.MAX_RENDERED = 8
    tl._trim_rendered()
    assert len(tl._rendered) <= 8, len(tl._rendered)
    print(f"OK: 上限を超えたら遠い行の画像を捨てる（{len(tl._rendered)} 枚）")

    tl.close()
    doc.close()
    for f in (src, bad, empty, big):
        if os.path.exists(f):
            os.remove(f)
    print("ALL OK: 実運用の失敗ケース / サムネイルの軽量化")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
