"""印刷ダイアログの「どのページを刷るか」まわりのテスト。

奇数/偶数のみ・逆順・ページ指定の自動選択と検査・プレビューの追従拡大を、
offscreen の実ダイアログで検証する。プリンターは要らない。
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtWidgets import QApplication

from viewer.document import PdfDocument
from viewer.mw_print import select_pages
from viewer.print_dialog import PrintDialog
from viewer.storage import make_settings

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def main() -> int:
    # --- select_pages: 純粋なロジック --------------------------------------
    check("all", select_pages({"page_mode": "all"}, 6, 2) == [0, 1, 2, 3, 4, 5])
    check("current", select_pages({"page_mode": "current"}, 6, 2) == [2])
    check("range", select_pages({"page_mode": "range", "range_text": "2-3, 6"}, 6, 0) == [1, 2, 5])
    check("odd = 1,3,5 ページ",
          select_pages({"page_mode": "all", "subset": "odd"}, 6, 0) == [0, 2, 4])
    check("even = 2,4,6 ページ",
          select_pages({"page_mode": "all", "subset": "even"}, 6, 0) == [1, 3, 5])
    check("reverse", select_pages({"page_mode": "all", "reverse": True}, 4, 0) == [3, 2, 1, 0])
    check("even + reverse（手差し両面の裏面）",
          select_pages({"page_mode": "all", "subset": "even", "reverse": True}, 6, 0) == [5, 3, 1])
    check("range + odd",
          select_pages({"page_mode": "range", "range_text": "1-4", "subset": "odd"}, 6, 0) == [0, 2])
    check("current + even → 空（現在が奇数ページ）",
          select_pages({"page_mode": "current", "subset": "even"}, 6, 0) == [])
    try:
        select_pages({"page_mode": "range", "range_text": "abc"}, 6, 0)
        check("range の誤りは ValueError", False)
    except ValueError:
        check("range の誤りは ValueError", True)

    # --- ダイアログ ----------------------------------------------------------
    tmp = tempfile.gettempdir()
    src = os.path.join(tmp, "_print_pages.pdf")
    d = fitz.open()
    for n in range(5):
        d.new_page(width=595, height=842).insert_text((72, 100), f"p{n + 1}", fontsize=20)
    d.save(src)
    d.close()

    app = QApplication(sys.argv[:1])
    doc = PdfDocument()
    doc.open(src)
    st = make_settings()
    dlg = PrintDialog(doc, 2, st)
    dlg.show()
    app.processEvents()

    check("現在のページに番号が付く", "（3）" in dlg.rb_current.text(), dlg.rb_current.text())
    check("キャンセルが日本語", any(b.text() == "キャンセル" for b in dlg.findChildren(type(dlg.print_button))))
    opt = dlg.options()
    check("options に subset/reverse", opt["subset"] == "all" and opt["reverse"] is False)
    dlg.subset_combo.setCurrentIndex(dlg.subset_combo.findData("odd"))
    dlg.reverse_check.setChecked(True)
    opt = dlg.options()
    check("options: odd + reverse", opt["subset"] == "odd" and opt["reverse"] is True)
    check("select_pages(options)", select_pages(opt, 5, 2) == [4, 2, 0])

    # ページ指定欄に打ち始めると、そのラジオが選ばれる
    dlg.rb_all.setChecked(True)
    dlg.range_edit.clear()
    dlg.range_edit.setFocus()
    dlg.range_edit.insert("1-2")          # ユーザー入力（textEdited が出る）
    app.processEvents()
    check("range: 打つとラジオが切り替わる", dlg.rb_range.isChecked())
    check("range: 正しい指定なら印刷できる", dlg.print_button.isEnabled())
    dlg.range_edit.clear()
    dlg.range_edit.insert("9-")
    app.processEvents()
    check("range: 誤りなら印刷ボタンが無効", not dlg.print_button.isEnabled(), dlg.range_error())
    check("range: 誤りの理由が出る", bool(dlg.range_error()))
    dlg.range_edit.clear()
    dlg.range_edit.insert("99")
    app.processEvents()
    check("range: 範囲外だけなら無効", not dlg.print_button.isEnabled(), dlg.range_error())
    dlg.rb_all.setChecked(True)
    app.processEvents()
    check("range: 『すべて』に戻せば有効", dlg.print_button.isEnabled())

    # プレビューはダイアログを広げると大きくなる
    dlg.resize(700, 600)
    app.processEvents()
    dlg._refresh()
    small = dlg.preview.pixmap().width()
    dlg.resize(1200, 900)
    app.processEvents()
    dlg._refresh()
    big = dlg.preview.pixmap().width()
    check("preview: 広げると大きくなる", big > small + 100, f"{small} -> {big}")
    check("preview: ラベルからはみ出さない",
          big <= dlg.preview.width() and dlg.preview.pixmap().height() <= dlg.preview.height(),
          f"{big}x{dlg.preview.pixmap().height()} in {dlg.preview.width()}x{dlg.preview.height()}")
    ratio = dlg.preview.pixmap().height() / big
    check("preview: 用紙の縦横比", 1.2 < ratio < 1.6, f"{ratio:.2f}")
    dlg.reject()
    doc.close()

    ok = all(results)
    print("ALL PASS" if ok else f"{results.count(False)} FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
