"""開いた時の表示倍率が安定するかのテスト。

「PDF によって極小になったり幅合わせになったり安定しない」不具合の回帰テスト。
原因は (1) 非表示のまま開いてビューポート幅が確定していない、(2)「幅に合わせる」
が選んだ瞬間の一度きりで追従しない、の2点だった。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtWidgets import QApplication

from viewer.main_window import MainWindow

ROOT = os.path.dirname(os.path.abspath(__file__))
results: list[bool] = []

SIZES = {
    "a4p": (595, 842),
    "a4l": (842, 595),
    "letter": (612, 792),
    "postcard": (283, 420),
    "poster": (2384, 3370),
}


def check(cond: bool, label: str) -> bool:
    print(("OK  : " if cond else "NG  : ") + label)
    results.append(bool(cond))
    return bool(cond)


def build(path: str, w: float, h: float, pages: int = 2) -> None:
    doc = fitz.open()
    for n in range(pages):
        p = doc.new_page(width=w, height=h)
        p.insert_text((40, 80), f"{int(w)}x{int(h)} page {n + 1}", fontsize=20)
    doc.save(path)
    doc.close()


def shown_width(w: MainWindow, page_w: float) -> float:
    return page_w * w.page_view._zoom


def test_same_width_for_any_page_size(app: QApplication, w: MainWindow,
                                      root: str) -> None:
    """ページの大きさが違っても「幅に合わせる」なら表示幅は揃う。"""
    w.settings.setValue("default_zoom_mode", "fit")
    widths = []
    for name, (pw, ph) in SIZES.items():
        path = os.path.join(root, f"{name}.pdf")
        build(path, pw, ph)
        w.open_path(path)
        app.processEvents()
        widths.append(round(shown_width(w, pw)))
    spread = max(widths) - min(widths)
    check(spread <= 2, f"ページの大小によらず表示幅が揃う（{widths}）")
    vw = w.page_view.viewport().width()
    check(abs(widths[0] - vw) < 80, f"表示幅がビューポートに収まる（{widths[0]} / {vw}）")


def test_fit_follows_resize(app: QApplication, w: MainWindow, root: str) -> None:
    """ウィンドウ幅を変えると「幅に合わせる」が追従する。"""
    path = os.path.join(root, "a4p.pdf")
    w.open_path(path)
    app.processEvents()
    before = w.page_view._zoom
    w.resize(1600, 900)
    app.processEvents()
    after = w.page_view._zoom
    check(after > before, f"広げると倍率が上がる（{before:.3f} → {after:.3f}）")
    vw = w.page_view.viewport().width()
    check(abs(595 * after - vw) < 80,
          f"広げた後もビューポート幅に合う（{595 * after:.0f} / {vw}）")

    w.resize(1000, 800)
    app.processEvents()
    narrowed = w.page_view._zoom
    check(narrowed < after, f"狭めると倍率が下がる（{after:.3f} → {narrowed:.3f}）")


def test_manual_zoom_stops_following(app: QApplication, w: MainWindow) -> None:
    """手動で倍率を決めたら、その後のリサイズでは変えない。"""
    w.page_view.set_zoom(1.25)
    app.processEvents()
    w.resize(1500, 850)
    app.processEvents()
    check(abs(w.page_view._zoom - 1.25) < 0.001,
          f"手動指定の倍率が保たれる（{w.page_view._zoom:.3f}）")


def test_fit_height_follows(app: QApplication, w: MainWindow) -> None:
    """「縦に合わせる」も高さの変化に追従する。"""
    w.page_view.fit_height()
    app.processEvents()
    before = w.page_view._zoom
    w.resize(1500, 1100)
    app.processEvents()
    check(w.page_view._zoom > before,
          f"高さを増やすと倍率が上がる（{before:.3f} → {w.page_view._zoom:.3f}）")


def test_open_while_hidden(app: QApplication, root: str) -> None:
    """非表示（トレイ常駐）から開いても、表示後に妥当な倍率になる。"""
    w = MainWindow()
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    w.hide()
    app.processEvents()

    path = os.path.join(root, "a4p.pdf")
    w.activate_and_open(path)   # トレイからの復帰と同じ経路
    app.processEvents()
    check(w.isVisible(), "開くときにウィンドウが表示される")
    vw = w.page_view.viewport().width()
    shown = 595 * w.page_view._zoom
    check(abs(shown - vw) < 80,
          f"非表示から開いても幅が合う（表示幅{shown:.0f} / ビューポート{vw}）")
    for i in range(w.tabs.count()):
        t = w.tabs.widget(i)
        if hasattr(t, "doc"):
            t.doc.close()


def main() -> int:
    app = QApplication(sys.argv)
    root = tempfile.mkdtemp(prefix="trv_zoom_")
    try:
        w = MainWindow()
        w.resize(1200, 800)
        w.show()
        app.processEvents()
        test_same_width_for_any_page_size(app, w, root)
        test_fit_follows_resize(app, w, root)
        test_manual_zoom_stops_following(app, w)
        test_fit_height_follows(app, w)
        test_open_while_hidden(app, root)
        for i in range(w.tabs.count()):
            t = w.tabs.widget(i)
            if hasattr(t, "doc"):
                t.doc.close()
    finally:
        os.chdir(ROOT)
        shutil.rmtree(root, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    raise SystemExit(main())
