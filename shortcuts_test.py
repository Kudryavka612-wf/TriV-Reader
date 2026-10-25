"""キーボードショートカットが、リボンのどのタブにいても・全画面でも効くことの確認。

Qt はアクションのショートカットを、そのアクションを持つウィジェットが画面に出て
いるときだけ有効にする。以前は、開いているリボンのタブに無いボタンのショートカット
（Ctrl+S / Ctrl+O / Ctrl+P / Ctrl+Z / PgUp など）が効かなかった。メニューバーは画面に
出ない作りなので、リボンのタブを変えるだけで効く・効かないが変わり、全画面では
大半が効かなかった。やり直しの Ctrl+Y / Ctrl+Shift+Z はどこでも効かなかった。
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from viewer.main_window import MainWindow

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def main() -> int:
    app = QApplication(sys.argv)
    pdf = os.path.join(tempfile.mkdtemp(prefix="trv_sc_"), "s.pdf")
    d = fitz.open()
    for i in range(3):
        d.new_page().insert_text((72, 100), f"page {i + 1}", fontsize=20)
    d.save(pdf)
    d.close()

    w = MainWindow()
    w.resize(1180, 820)
    w.show()
    app.processEvents()
    w.open_path(pdf)
    app.processEvents()
    w.activateWindow()
    w.page_view.setFocus()
    app.processEvents()

    acts = {k: v for k, v in vars(w).items()
            if isinstance(v, QAction) and v.shortcuts()}
    hits: dict[str, int] = {}
    for k, a in acts.items():
        try:
            a.triggered.disconnect()
        except Exception:  # noqa: BLE001
            pass
        a.setEnabled(True)
        a.triggered.connect(lambda *_x, k=k: hits.__setitem__(k, hits.get(k, 0) + 1))

    def not_firing() -> list[str]:
        bad = []
        for k, a in acts.items():
            ok = False
            for seq in a.shortcuts():
                hits.clear()
                a.setEnabled(True)
                w.activateWindow()
                w.page_view.setFocus()
                app.processEvents()
                QTest.keySequence(w, QKeySequence(seq))
                app.processEvents()
                if hits.get(k) == 1:
                    ok = True
            if not ok:
                bad.append(f"{k}{[s.toString() for s in a.shortcuts()]}")
        return bad

    rb = w.ribbon
    rb.set_collapsed(False)
    names = [rb.tabText(i) for i in range(rb.count())]
    for i in range(rb.count()):
        rb.setCurrentIndex(i)
        app.processEvents()
        bad = not_firing()
        check(f"every shortcut works on the ribbon tab '{names[i]}'", not bad, str(bad))
    rb.setCurrentIndex(0)
    rb.set_collapsed(True)
    app.processEvents()
    bad = not_firing()
    check("…with the ribbon collapsed", not bad, str(bad))
    rb.set_collapsed(False)
    w._set_fullscreen(True)
    app.processEvents()
    bad = not_firing()
    check("…and in full screen (ribbon hidden)", not bad, str(bad))
    w._set_fullscreen(False)
    app.processEvents()

    redo = [s.toString() for s in w.act_redo.shortcuts()]
    check("redo has two different keys (Ctrl+Y and Ctrl+Shift+Z)",
          sorted(redo) == ["Ctrl+Shift+Z", "Ctrl+Y"], str(redo))

    w.doc.modified = False
    w.doc.close()
    w._force_quit = True
    w.close()

    # 実際に効く: 「ページ」タブにいても、Ctrl+Z で戻り、Ctrl+Y / Ctrl+Shift+Z でやり直せる
    w2 = MainWindow()
    w2.resize(1180, 820)
    w2.show()
    app.processEvents()
    w2.open_path(pdf)
    app.processEvents()
    w2.activateWindow()
    w2.page_view.setFocus()
    w2.doc.add_rect(0, fitz.Rect(10, 10, 80, 80))
    w2.page_view.refresh_page(0)
    w2._update_undo_actions()
    for i in range(w2.ribbon.count()):
        if w2.ribbon.tabText(i) == "ページ":
            w2.ribbon.setCurrentIndex(i)
    app.processEvents()

    def count() -> int:
        return len(list(w2.doc._doc[0].annots()))

    def press(seq: str) -> None:
        w2.activateWindow()
        w2.page_view.setFocus()
        QTest.keySequence(w2, QKeySequence(seq))
        app.processEvents()

    check("(前提) an annotation was added", count() == 1)
    press("Ctrl+Z")
    check("Ctrl+Z undoes it while the Page tab is open", count() == 0)
    press("Ctrl+Y")
    check("Ctrl+Y redoes it", count() == 1)
    press("Ctrl+Z")
    press("Ctrl+Shift+Z")
    check("Ctrl+Shift+Z redoes it too", count() == 1)
    w2.doc.modified = False
    w2._force_quit = True
    w2.close()
    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
