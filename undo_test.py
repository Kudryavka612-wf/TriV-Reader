"""ページ操作の取り消し（Undo/Redo）の検証。

注釈だけでなく、削除・並べ替え・回転・トリミングなどページ構成を変える操作も
Ctrl+Z で戻せること、注釈と混ざっても時系列どおりに戻ること、
まとめて行った操作が 1 回で戻ることを見る。
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
from viewer.main_window import MainWindow

TMP = tempfile.gettempdir()


def build(path: str, pages: int = 6) -> None:
    doc = fitz.open()
    for n in range(pages):
        p = doc.new_page(width=595, height=842)
        p.insert_text((72, 100), f"PAGE {n + 1}", fontsize=24)
    doc.save(path)
    doc.close()


def page_texts(doc: PdfDocument) -> list[str]:
    return [doc._doc.load_page(i).get_text().strip() for i in range(doc.page_count)]


def main() -> int:  # noqa: C901
    app = QApplication(sys.argv)
    src = os.path.join(TMP, "_undo_src.pdf")
    build(src)

    # --- ページ削除 -----------------------------------------------------
    doc = PdfDocument()
    doc.open(src)
    assert not doc.can_undo(), "開いた直後に履歴がある"
    before = page_texts(doc)
    doc.delete_page(2)
    assert doc.page_count == 5 and doc.undo_label() == "ページの削除"
    kind, index, label = doc.undo()
    assert kind == "doc" and index == 2 and label == "ページの削除"
    assert page_texts(doc) == before, "削除したページが戻っていない"
    assert doc.can_redo()
    doc.redo()
    assert doc.page_count == 5, "やり直しで削除されない"
    doc.undo()
    assert doc.page_count == 6
    print("OK: ページ削除を取り消し／やり直しできる")

    # --- 並べ替え -------------------------------------------------------
    doc.reorder([5, 4, 3, 2, 1, 0])
    assert page_texts(doc)[0] == "PAGE 6"
    doc.undo()
    assert page_texts(doc) == before, "並べ替えが戻らない"
    print("OK: 並べ替えを取り消せる")

    # --- まとめて回転は 1 回で戻る --------------------------------------
    with doc.undo_group("ページの回転"):
        for i in range(doc.page_count):
            doc.rotate_page(i, 90)
    assert all(doc.rotation(i) == 90 for i in range(doc.page_count))
    n_hist = len(doc._undo)
    doc.undo()
    assert all(doc.rotation(i) == 0 for i in range(doc.page_count)), "全部戻らない"
    assert len(doc._undo) == n_hist - 1, "まとめた操作が複数に分かれている"
    print("OK: まとめた回転は Undo 1 回で全ページ戻る")

    # --- 注釈とページ操作が混ざっても時系列で戻る ------------------------
    doc.add_highlight(0, [fitz.Rect(70, 90, 200, 115)])
    doc.rotate_page(1, 90)
    doc.delete_page(3)
    assert [e[0] for e in doc._undo][-3:] == ["annot", "doc", "doc"]
    doc.undo()                      # 削除を戻す
    assert doc.page_count == 6
    doc.undo()                      # 回転を戻す
    assert doc.rotation(1) == 0
    doc.undo()                      # ハイライトを戻す
    assert len(doc._doc.load_page(0).annots() and
               list(doc._doc.load_page(0).annots())) == 0, "注釈が残っている"
    print("OK: 注釈とページ操作が混ざっても順番どおり戻る")

    # --- トリミング / 白紙挿入 / 複製 ------------------------------------
    r = doc._doc.load_page(0).rect
    doc.set_crop(0, fitz.Rect(50, 50, r.width - 50, r.height - 50))
    cropped = doc._doc.load_page(0).rect.width
    doc.undo()
    assert doc._doc.load_page(0).rect.width != cropped, "トリミングが戻らない"

    doc.insert_blank_page(1)
    assert doc.page_count == 7
    doc.undo()
    assert doc.page_count == 6

    doc.duplicate_page(0)
    assert doc.page_count == 7
    doc.undo()
    assert doc.page_count == 6
    print("OK: トリミング・白紙挿入・複製も戻せる")

    # --- 上限を超えたら古いものから捨てる --------------------------------
    doc._clear_history()
    doc._UNDO_DOC_MAX = 2
    for i in range(4):
        doc.rotate_page(0, 90)
    assert len([e for e in doc._undo if e[0] == "doc"]) <= 2, doc._undo
    assert doc._undo_bytes > 0
    doc._clear_history()
    assert doc._undo_bytes == 0 and not doc.can_undo()
    print("OK: 履歴は件数の上限で古いものから捨てる")

    doc.close()

    # --- UI から（Ctrl+Z に割り当てられた操作）--------------------------
    w = MainWindow()
    w.resize(1000, 760)
    w.show()
    app.processEvents()
    w.open_path(src)
    app.processEvents()
    assert not w.act_undo.isEnabled(), "開いた直後に元に戻すが有効"

    w._delete_page(1)
    app.processEvents()
    assert w.doc.page_count == 5
    assert w.act_undo.isEnabled(), "ページ削除の後に元に戻すが使えない"
    assert "ページの削除" in w.act_undo.toolTip(), w.act_undo.toolTip()
    assert w.thumbnails.count() == 5

    w._undo_annot()      # ＝ Ctrl+Z
    app.processEvents()
    assert w.doc.page_count == 6, "UI から戻せない"
    assert w.thumbnails.count() == 6, "サムネイルが戻っていない"
    assert w.act_redo.isEnabled()
    w._redo_annot()
    app.processEvents()
    assert w.doc.page_count == 5
    print("OK: リボン/Ctrl+Z からページ削除を戻せる（サムネイルも追従）")

    # 未保存のまま閉じると確認ダイアログで止まるので、片付けてから閉じる
    w.doc.modified = False
    w._force_quit = True
    w.close()
    if os.path.exists(src):
        os.remove(src)
    print("ALL OK: ページ操作の Undo/Redo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
