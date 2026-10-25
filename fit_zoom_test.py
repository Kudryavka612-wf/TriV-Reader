"""「幅に合わせる」が止まらず振動しないこと、倍率を変えても見ていた位置を保つことの確認。

1 ページだけの PDF を「幅に合わせる」で表示中、ウィンドウの高さがある狭い帯に入ると、
縦スクロールバーが出たり消えたりして viewport の幅が変わり、倍率が往復し続けた
（1 秒に数千回。窓の大きさを変えるまで止まらず、CPU を使い続ける）。官公庁の 1 枚ものが
当てはまる。また、倍率を変えるたびに現在のページの先頭へ飛び、読んでいた所や入力中の欄を
見失っていた。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz
from PySide6.QtWidgets import QApplication

from viewer.main_window import MainWindow

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def make_pdf(path: str, pages: int) -> str:
    d = fitz.open()
    for i in range(pages):
        d.new_page(width=595, height=842).insert_text((72, 100), f"page {i + 1}", fontsize=20)
    d.save(path)
    d.close()
    return path


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.002)


def main() -> int:
    app = QApplication(sys.argv)
    tmp = tempfile.mkdtemp(prefix="trv_fit_")
    one = make_pdf(os.path.join(tmp, "one.pdf"), 1)
    three = make_pdf(os.path.join(tmp, "three.pdf"), 3)

    # --- 1 ページの文書で、窓の大きさをいろいろ変えても倍率が落ち着く ---------------
    w = MainWindow()
    w.resize(1100, 900)
    w.show()
    pump(app, 0.3)
    w.open_path(one)
    pump(app, 0.3)
    pv = w.page_view
    count = {"n": 0}
    pv.zoom_changed.connect(lambda _z: count.__setitem__("n", count["n"] + 1))
    worst = (0, None)
    for width in (700, 760, 820, 900, 1000):
        for height in range(780, 1100, 11):
            w.resize(width, height)
            pump(app, 0.06)       # 窓の大きさが決まったあとの反応を見る
            count["n"] = 0
            pump(app, 0.12)
            if count["n"] > worst[0]:
                worst = (count["n"], (width, height))
    check("fit-to-width settles for every window size (no endless zoom flip-flop)",
          worst[0] <= 2, f"(worst: {worst[0]} zoom changes in 0.12s at {worst[1]})")
    w.doc.modified = False
    w._force_quit = True
    w.close()

    # --- 倍率を変えても、ページの中で見ていた所を保つ ------------------------------
    w = MainWindow()
    w.resize(1100, 800)
    w.show()
    pump(app, 0.3)
    w.open_path(three)
    pump(app, 0.3)
    pv = w.page_view
    pv.set_zoom(1.0)
    pump(app, 0.2)
    lab = pv._labels[1]
    bar = pv.verticalScrollBar()
    bar.setValue(lab.y() - 30 + lab.height() // 2)    # 2 ページ目の真ん中が画面の上端
    pump(app, 0.2)
    idx = pv.index
    before = (bar.value() - (pv._labels[idx].y() - 30)) / pv._labels[idx].height()
    pv.set_zoom(1.5)
    pump(app, 0.2)
    lab2 = pv._labels[idx]
    after = (bar.value() - (lab2.y() - 30)) / lab2.height()
    check("zooming keeps the same spot of the page at the top of the screen",
          abs(after - before) < 0.03, f"(before {before:.2f}, after {after:.2f})")
    n0 = pv.zoom
    changed = {"n": 0}
    pv.zoom_changed.connect(lambda _z: changed.__setitem__("n", changed["n"] + 1))
    pv.set_zoom(n0)
    check("setting the zoom it already has does nothing", changed["n"] == 0)

    # ズームのスライダーは、ドラッグ以外（キー・溝のクリック）でも効く
    w.zoom_slider.setValue(200)
    pump(app, 0.1)
    check("the zoom slider works for any change (key / click on the groove), not only a drag",
          abs(pv.zoom - 2.0) < 0.01, f"(zoom {pv.zoom})")
    w.doc.modified = False
    w._force_quit = True
    w.close()

    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
