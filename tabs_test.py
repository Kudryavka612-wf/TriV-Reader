"""文書タブの表示・操作テスト（長い名前の省略、× ボタン、中ボタンで閉じる）。

ダークテーマで Fusion 標準の × とスクロール矢印が背景に溶けて見えなくなる
不具合の再発防止。offscreen の実 MainWindow で検証する。
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QTabBar

from viewer.main_window import MainWindow

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def make_pdf(path: str) -> None:
    d = fitz.open()
    d.new_page()
    d.save(path)
    d.close()


def send(app, w, et, pt, button):
    app.sendEvent(w, QMouseEvent(et, QPointF(pt), QPointF(w.mapToGlobal(pt)),
                                 button, button, Qt.KeyboardModifier.NoModifier))
    app.processEvents()


def main() -> int:
    tmp = tempfile.gettempdir()
    long_name = "令和8年度_労働保険年度更新申告書_控え_株式会社サンプル商事（本社）_最終版.pdf"
    paths = [os.path.join(tmp, n) for n in
             (long_name, "_tabs_b.pdf", "_tabs_c.pdf", "_tabs_d.pdf")]
    for p in paths:
        make_pdf(p)

    app = QApplication(sys.argv[:1])
    win = MainWindow()
    win.resize(900, 600)
    win.show()
    app.processEvents()
    for p in paths:
        win.open_path(p)
    app.processEvents()
    bar = win.tabs.tabBar()

    check("elide: 中央省略", win.tabs.elideMode() == Qt.TextElideMode.ElideMiddle)
    r0 = bar.tabRect(0)
    check("long name: タブ幅が上限内", r0.width() <= 270, str(r0.width()))
    btn0 = bar.tabButton(0, QTabBar.ButtonPosition.RightSide)
    check("close: 自前の × がある", btn0 is not None and btn0.objectName() == "tabClose")
    check("close: × がタブの枠内に収まる",
          btn0 is not None and r0.contains(btn0.geometry()), f"{r0} vs {btn0.geometry() if btn0 else None}")
    check("close: 全タブに × がある",
          all(bar.tabButton(i, QTabBar.ButtonPosition.RightSide) is not None
              for i in range(bar.count())))

    # ダークテーマでも × の文字色が背景と区別できる（見えない不具合の再発防止）
    win.act_dark.setChecked(True)
    app.processEvents()
    fg = btn0.palette().color(btn0.foregroundRole())
    pm = btn0.grab().toImage()
    dark_bg = pm.pixelColor(1, 1)
    bright = [pm.pixelColor(x, y).lightness() for x in range(pm.width()) for y in range(pm.height())]
    check("close(dark): ✕ が背景より明るい画素を持つ",
          max(bright) - dark_bg.lightness() > 60, f"max={max(bright)} bg={dark_bg.lightness()}")
    win.act_dark.setChecked(False)
    app.processEvents()

    # × を押すとそのタブが閉じる（並べ替え後でも正しいタブ）
    win.tabs.tabBar().moveTab(0, 2)
    app.processEvents()
    name_at2 = win.tabs.tabText(2)
    btn = bar.tabButton(2, QTabBar.ButtonPosition.RightSide)
    btn.click()
    app.processEvents()
    check("close: × で該当タブが閉じる", win.tabs.count() == 3
          and all(win.tabs.tabText(i) != name_at2 for i in range(3)), name_at2)

    # 中ボタンで閉じる
    n = win.tabs.count()
    rect = bar.tabRect(1)
    c = rect.center()
    send(app, bar, QEvent.Type.MouseButtonPress, c, Qt.MouseButton.MiddleButton)
    send(app, bar, QEvent.Type.MouseButtonRelease, c, Qt.MouseButton.MiddleButton)
    check("middle click: タブが閉じる", win.tabs.count() == n - 1, str(win.tabs.count()))
    # 押した場所と離した場所が違えば閉じない
    n = win.tabs.count()
    send(app, bar, QEvent.Type.MouseButtonPress, bar.tabRect(0).center(), Qt.MouseButton.MiddleButton)
    send(app, bar, QEvent.Type.MouseButtonRelease, bar.tabRect(1).center(), Qt.MouseButton.MiddleButton)
    check("middle click: ずれたら閉じない", win.tabs.count() == n)

    # タブが溢れたときのスクロール矢印が、ダークでも見える（枠だけにならない）
    win.act_dark.setChecked(True)
    for i in range(12):
        p = os.path.join(tmp, f"_tabs_ovf_{i:02d}.pdf")
        make_pdf(p)
        win.open_path(p)
    app.processEvents()
    from PySide6.QtWidgets import QToolButton
    arrows = [b for b in bar.findChildren(QToolButton)
              if b.objectName() in ("ScrollLeftButton", "ScrollRightButton")]
    check("scroller: 左右ボタンが出る", len(arrows) == 2 and all(b.isVisible() for b in arrows))
    left = next(b for b in arrows if b.objectName() == "ScrollLeftButton")
    img = left.grab().toImage()
    bg = img.pixelColor(2, 2).lightness()
    center = [img.pixelColor(x, y).lightness()
              for x in range(img.width() // 2 - 5, img.width() // 2 + 5)
              for y in range(img.height() // 2 - 6, img.height() // 2 + 6)]
    check("scroller(dark): 矢印が背景より明るい", max(center) - bg > 60,
          f"max={max(center)} bg={bg}")
    win.act_dark.setChecked(False)
    app.processEvents()
    win.close_other_tabs(win.tabs.currentIndex())
    app.processEvents()

    # 未保存マークがタブ名に付く
    win.doc.rotate_page(0, 90)
    win.doc.modified = True
    win._update_title()
    check("modified: タブ名に * が付く", win.tabs.tabText(win.tabs.currentIndex()).startswith("*"))
    win.doc.modified = False
    win.close_all_tabs()

    ok = all(results)
    print("ALL PASS" if ok else f"{results.count(False)} FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
