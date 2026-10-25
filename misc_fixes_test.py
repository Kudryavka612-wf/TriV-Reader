"""細かい不具合の回帰テスト。

- エラーのダイアログは「この実行中に 3 回」で打ち止めだった。常駐で何日も動くので、累計 3 回の
  あとは何も表示されず、ボタンが効かないように見えた
- 最近使ったファイルを窓が出る前に全部 exists で確かめていた。つながっていない共有では 1 件あたり
  数十秒止まる
- 一括処理の出力名が重なると、確認なしに上書きして先に処理した分が消えた。画像・分割の書き出しも
  同名のファイルを確認なしに上書きした。書き出しの保存先が、隠れたデータフォルダから始まった
- 全画面のままテキストを入力して Esc を押すと、入力は確定されず、全画面だけが解除された
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import viewer.page_view as PV
from viewer import errorlog
from viewer.main_window import MainWindow
from viewer.util import exists_fast, unique_path

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def main() -> int:
    app = QApplication(sys.argv)
    tmp = tempfile.mkdtemp(prefix="trv_misc_")
    try:
        # --- エラーのダイアログ: 時間の窓で数える ----------------------------------
        shown = []
        orig_exec = QMessageBox.exec
        QMessageBox.exec = lambda self: shown.append(self.informativeText()) or 0
        errorlog._shown.clear()
        try:
            for i in range(5):
                errorlog._notify("log.txt", f"error {i}")
            for _ in range(10):
                app.processEvents()
            check("a burst of errors shows only 3 dialogs", len(shown) == 3, f"({len(shown)})")
            real = time.monotonic
            time.monotonic = lambda: real() + errorlog.WINDOW_SEC + 5
            try:
                errorlog._notify("log.txt", "much later")
                for _ in range(10):
                    app.processEvents()
            finally:
                time.monotonic = real
            check("…but an error long after the burst is shown again (not capped for the "
                  "whole run)", len(shown) == 4, f"({len(shown)})")
        finally:
            QMessageBox.exec = orig_exec
            errorlog._shown.clear()

        # --- ネットワーク上のパスは、確かめずに先へ進む ------------------------------
        t0 = time.monotonic()
        quick = exists_fast("\\\\10.255.255.1\\share\\a.pdf")
        check("a network path is not probed (no tens-of-seconds stall at start-up)",
              quick is True and time.monotonic() - t0 < 0.5, f"({time.monotonic() - t0:.3f}s)")
        check("local paths are still checked",
              exists_fast(__file__) and not exists_fast(os.path.join(tmp, "nope.pdf")))

        # --- 重ならない名前 ----------------------------------------------------------
        a = os.path.join(tmp, "a.pdf")
        check("an unused name is kept", unique_path(a) == a)
        open(a, "w").close()
        check("a used name gets ' (2)'", unique_path(a) == os.path.join(tmp, "a (2).pdf"))
        open(os.path.join(tmp, "a (2).pdf"), "w").close()
        check("…and ' (3)' when that is used too", unique_path(a) == os.path.join(tmp, "a (3).pdf"))

        # --- 書き出し: 保存先と上書き確認 --------------------------------------------
        folder = os.path.join(tmp, "work")
        os.makedirs(folder)
        pdf = os.path.join(folder, "doc.pdf")
        d = fitz.open()
        for i in range(3):
            d.new_page().insert_text((72, 100), f"p{i + 1}", fontsize=20)
        d.save(pdf)
        d.close()
        w = MainWindow()
        w.resize(1100, 800)
        w.show()
        app.processEvents()
        w.open_path(pdf)
        for _ in range(20):
            app.processEvents()
        check("export dialogs start in the opened file's folder",
              os.path.normcase(w._export_dir()) == os.path.normcase(folder), w._export_dir())

        asked = []
        orig_q = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *args, **k: asked.append(args[2]) or QMessageBox.StandardButton.Cancel)
        try:
            free = [os.path.join(folder, "nothing.png")]
            check("no clash: no question asked", w._confirm_overwrite(free) and not asked)
            names = w.doc.split_output_names(1, folder, "doc")
            check("split would write doc_part01..03",
                  [os.path.basename(n) for n in names]
                  == ["doc_part01.pdf", "doc_part02.pdf", "doc_part03.pdf"], str(names))
            open(names[1], "w").close()
            ok = w._confirm_overwrite(names)
            check("an existing output file makes it ask first, and Cancel keeps it",
                  ok is False and len(asked) == 1 and "doc_part02.pdf" in asked[0], str(asked))
            check("page images are named the same way for the check and the write",
                  w.doc.image_output_name(folder, "doc", 0, "jpeg").endswith("doc_001.jpg"))
        finally:
            QMessageBox.question = orig_q

        # --- 全画面で入力中に Esc: 入力を確定し、全画面は保つ -------------------------
        w.act_fullscreen.setChecked(True)
        for _ in range(10):
            app.processEvents()
        pv = w.page_view
        pv.set_tool(PV.TOOL_TEXT)
        lab = pv._labels[0]
        pt = QPoint(150, 200)
        for et in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            btn = Qt.MouseButton.LeftButton if et == QEvent.Type.MouseButtonPress \
                else Qt.MouseButton.NoButton
            app.sendEvent(lab, QMouseEvent(et, QPointF(pt), QPointF(lab.mapToGlobal(pt)),
                                           Qt.MouseButton.LeftButton, btn,
                                           Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        ed = pv._text_editor
        QTest.keyClicks(ed, "memo")
        app.processEvents()
        QTest.keyClick(ed, Qt.Key.Key_Escape)
        for _ in range(20):
            app.processEvents()
        pg = w.doc._doc[0]
        notes = [a for a in pg.annots() if a.type[1] == "FreeText"]
        check("Esc while typing in full screen commits the text", len(notes) == 1
              and pv._text_editor is None, f"({len(notes)} notes)")
        check("…and does not leave full screen", w.isFullScreen() and w.act_fullscreen.isChecked())
        w.act_fullscreen.setChecked(False)
        w.doc.modified = False
        w._force_quit = True
        w.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
