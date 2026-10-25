"""PDF を開いても、窓を狭められること（最小幅が跳ね上がらない）の確認。

文書タブは「閲覧」と「ページ整理」を重ねて持つ（QStackedLayout）。重ね合わせは
隠れているほうの最小幅も数えるので、ページ整理の操作ボタン 12 個と説明文が 1 行に
並ぶ幅（約 1100px）が、PDF を開いた途端に窓の最小幅になっていた。ノート PC の画面
（拡大率 125% で実質 1092px）や、画面の半分に並べる配置に収まらず、窓が画面の外に
はみ出した。

ボタンの列は狭いとき横にスクロールし、説明文は折り返す。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication

from viewer.main_window import MainWindow

SAMPLE = os.path.join(ROOT, "tests", "fixtures", "6010_form_sample.pdf")
results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def settle(app: QApplication, n: int = 40) -> None:
    for _ in range(n):
        app.processEvents()


def main() -> int:
    app = QApplication(sys.argv)
    w = MainWindow()
    w.resize(700, 520)
    w.show()
    settle(app)
    empty = w.minimumSizeHint().width()
    w.open_path(SAMPLE)
    settle(app)
    opened = w.minimumSizeHint().width()
    check("opening a PDF does not raise the window's minimum width",
          opened <= empty + 20, f"(empty {empty}px -> opened {opened}px)")
    check("the window can stay narrow after opening a PDF (fits a laptop / half screen)",
          w.width() <= 700 and opened <= 700, f"(width {w.width()}px)")

    w.act_organize.trigger()   # ページ整理
    settle(app)
    org = w.organizer
    check("the page organizer does not widen the window either",
          w.minimumSizeHint().width() <= 700, f"({w.minimumSizeHint().width()}px)")
    bar = org._bar_scroll
    check("its buttons scroll sideways when narrow, so none is unreachable",
          bar.horizontalScrollBar().maximum() > 0,
          f"(scroll range {bar.horizontalScrollBar().maximum()}px)")
    last = org._btn_security
    bar.horizontalScrollBar().setValue(bar.horizontalScrollBar().maximum())
    settle(app, 5)
    vis = last.mapTo(bar.viewport(), last.rect().bottomRight()).x() <= bar.viewport().width()
    check("scrolled to the end, the last button (セキュリティ) is fully visible", vis)
    check("…and the buttons are not cut off vertically",
          bar.height() >= org._btn_insert.sizeHint().height()
          + bar.horizontalScrollBar().sizeHint().height())

    w.resize(1500, 700)   # 広い窓では、ボタンは全部並び、スクロールバーは要らない
    settle(app)
    check("on a wide window all the buttons fit without a scrollbar",
          bar.horizontalScrollBar().maximum() == 0)

    w.doc.modified = False
    w._force_quit = True
    w.close()
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
