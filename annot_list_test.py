"""注釈一覧パネルの統合テスト（MainWindow 経由）。

注釈を描くと一覧に載る／一覧クリックでビューが選択する／ビュー選択が一覧に
反映される／一覧の削除→Undo が効くことを、実ウィジェットで確認する。
合成 PDF を使うので外部フィクスチャ不要（CI で常に走る）。
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 利用者本人の設定（レジストリ）を書き換えないよう、使い捨ての INI で動かす
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")

import fitz
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from viewer import page_view as PV
from viewer import theme


def main() -> int:
    app = QApplication(sys.argv[:1])
    theme.apply_theme(app, dark=False)
    from viewer.main_window import MainWindow

    tmp = tempfile.gettempdir()
    src = os.path.join(tmp, "_annotlist.pdf")
    d = fitz.open()
    d.new_page(width=300, height=300)
    d.new_page(width=300, height=300)
    d.save(src)
    d.close()

    win = MainWindow()
    win.show()
    win.open_path(src)
    app.processEvents()
    pv = win.page_view
    lbl0 = pv._labels[0]

    def drag(p0, p1):
        for et, pt in ((QEvent.Type.MouseButtonPress, p0),
                       (QEvent.Type.MouseMove, p1),
                       (QEvent.Type.MouseButtonRelease, p1)):
            app.sendEvent(lbl0, QMouseEvent(
                et, QPointF(pt), QPointF(lbl0.mapToGlobal(pt)),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier))
        app.processEvents()

    results: list[bool] = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")

    pv.set_tool(PV.TOOL_RECT)
    drag(QPoint(30, 30), QPoint(120, 90))
    pv.set_tool(PV.TOOL_TEXT)
    drag(QPoint(30, 120), QPoint(200, 150))
    pv._text_editor.setPlainText("メモA")
    pv._text_editor._finish()
    app.processEvents()

    lst = win.annot_dock.list
    check("annotation list shows both annots", lst.count() == 2, f"(count={lst.count()})")
    labels = [lst.item(i).text() for i in range(lst.count())]
    check("entries carry page + type + text",
          any("メモA" in t for t in labels) and any("四角" in t for t in labels),
          str(labels))

    memo_item = next(lst.item(i) for i in range(lst.count())
                     if "メモA" in lst.item(i).text())
    win.annot_dock._on_clicked(memo_item)
    app.processEvents()
    check("clicking a row selects the annot in the view",
          pv.current_annot_ref() is not None)
    check("view selection highlights the row",
          lst.currentItem() is not None and "メモA" in lst.currentItem().text())

    ref = pv.current_annot_ref()
    win._on_annot_list_delete(ref[0], ref[1])
    app.processEvents()
    check("deleting from the list removes it", lst.count() == 1)
    pv.undo_annot()
    app.processEvents()
    win._refresh_annot_list()
    check("undo restores it in the list", lst.count() == 2)

    if os.path.exists(src):
        os.remove(src)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
