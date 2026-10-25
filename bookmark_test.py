"""しおりまわりの確認。

- 1 件足すと、既存のしおり全部の飛び先の位置・倍率・色・太字が失われ、ページの左上へ
  飛ぶようになった（目次を simple=True で取り直して書き戻していた）
- 行き先の無いしおり（ページ番号 -1）を押すと、先頭ページへ飛んだ
- キーボード（↑↓ と Enter）でしおりに飛べなかった
- 目次の編集で、レベル 1 の次にいきなり 3 を入れると、保存の段階で例外になり入力が失われた
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from viewer.bookmarks import TocDock, TocEditDialog
from viewer.document import PdfDocument

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def main() -> int:
    app = QApplication(sys.argv)   # noqa: F841
    d = fitz.open()
    for i in range(4):
        d.new_page(width=400, height=600).insert_text((72, 100), f"p{i + 1}", fontsize=20)
    d.set_toc([[1, "章A", 2, {"kind": fitz.LINK_GOTO, "to": fitz.Point(0, 400), "zoom": 1.5,
                              "bold": True, "color": (1.0, 0.0, 0.0)}],
               [1, "章B", 3]])
    doc = PdfDocument()
    doc._doc = d
    doc.add_bookmark("新しい章", 4)
    full = doc._doc.get_toc(simple=False)
    a = full[0][3]
    check("adding a bookmark keeps the others' destination (position and zoom)",
          abs(a.get("to", fitz.Point(0, 0)).y - 400) < 1 and abs(a.get("zoom", 0) - 1.5) < 0.01,
          str(a))
    check("…and their colour / bold", a.get("bold") is True and a.get("color") == (1.0, 0.0, 0.0),
          str(a))
    check("…and the new one is added at the end",
          [t[1] for t in full] == ["章A", "章B", "新しい章"] and full[-1][2] == 4)
    doc._doc.close()

    dock = TocDock()
    got: list[int] = []
    dock.page_requested.connect(got.append)
    dock.load([[1, "行き先なし", -1], [1, "3ページ", 3]])
    dock.tree.setFocus()
    top = dock.tree.topLevelItem(0)
    dock._on_clicked(top)
    check("a bookmark with no destination does not jump anywhere", got == [], str(got))
    dock._on_clicked(dock.tree.topLevelItem(1))
    check("a normal bookmark jumps to its page", got == [2], str(got))
    got.clear()
    dock.tree.setCurrentItem(dock.tree.topLevelItem(1))
    QTest.keyClick(dock.tree, Qt.Key.Key_Return)
    check("Enter on a bookmark jumps (keyboard works)", got == [2], str(got))

    dlg = TocEditDialog([[1, "A", 1]], 4)
    dlg._add_row(1, "B", 2)
    dlg._add_row(3, "C", 3)    # 1 → 3 は不可
    try:
        dlg.result_toc()
        err = ""
    except ValueError as exc:
        err = str(exc)
    check("jumping from level 1 straight to 3 is rejected with a clear message",
          "3 行目" in err and "レベル" in err, err)
    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
