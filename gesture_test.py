"""マウスジェスチャー（右ボタンを押しながら動かす）のテスト。

↓ タブを閉じる / ← 前のタブ / → 次のタブ / ↑ 先頭ページ。
少ししか動かさなかったときは、これまでどおり右クリックメニューに任せる。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from viewer import settings_dialog as prefs
from viewer.main_window import MainWindow

ROOT = os.path.dirname(os.path.abspath(__file__))
results: list[bool] = []


def check(cond: bool, label: str) -> bool:
    print(("OK  : " if cond else "NG  : ") + label)
    results.append(bool(cond))
    return bool(cond)


def build(path: str, pages: int = 5) -> None:
    doc = fitz.open()
    for n in range(pages):
        p = doc.new_page(width=595, height=842)
        p.insert_text((72, 144), f"page {n + 1}", fontsize=24)
    doc.save(path)
    doc.close()


def drag(app: QApplication, label, start: QPoint, end: QPoint,
         button=Qt.MouseButton.RightButton) -> None:
    """右ボタンで start → end へドラッグする。"""
    steps = [
        (QEvent.Type.MouseButtonPress, start),
        (QEvent.Type.MouseMove, QPoint((start.x() + end.x()) // 2,
                                       (start.y() + end.y()) // 2)),
        (QEvent.Type.MouseButtonRelease, end),
    ]
    for et, pt in steps:
        app.sendEvent(label, QMouseEvent(
            et, QPointF(pt), QPointF(label.mapToGlobal(pt)),
            button, button, Qt.KeyboardModifier.NoModifier))
    app.processEvents()


def label_of(w: MainWindow):
    return w.page_view._labels[w.page_view.index]


def test_next_and_prev_tab(app: QApplication, w: MainWindow, root: str) -> None:
    """→ で次のタブ、← で前のタブへ移る。"""
    for n in range(3):
        p = os.path.join(root, f"tab{n}.pdf")
        build(p)
        w.open_path(p)
        app.processEvents()
    check(w.tabs.count() == 3, f"（前提）3タブ開いている（{w.tabs.count()}）")
    w.tabs.setCurrentIndex(0)
    app.processEvents()

    drag(app, label_of(w), QPoint(200, 200), QPoint(300, 205))   # →
    check(w.tabs.currentIndex() == 1, f"→ で次のタブ（{w.tabs.currentIndex()}）")

    drag(app, label_of(w), QPoint(300, 200), QPoint(200, 195))   # ←
    check(w.tabs.currentIndex() == 0, f"← で前のタブ（{w.tabs.currentIndex()}）")


def test_up_goes_to_first_page(app: QApplication, w: MainWindow) -> None:
    """↑ で先頭ページへ戻る。"""
    w.page_view.set_page(3)
    app.processEvents()
    check(w.page_view.index == 3, f"（前提）4ページ目にいる（{w.page_view.index + 1}）")
    drag(app, label_of(w), QPoint(200, 300), QPoint(205, 180))   # ↑
    check(w.page_view.index == 0, f"↑ で先頭ページ（{w.page_view.index + 1}）")


def test_down_closes_tab(app: QApplication, w: MainWindow) -> None:
    """↓ で今のタブを閉じる。"""
    before = w.tabs.count()
    drag(app, label_of(w), QPoint(200, 180), QPoint(205, 300))   # ↓
    check(w.tabs.count() == before - 1,
          f"↓ でタブが閉じる（{before} → {w.tabs.count()}）")


def test_small_move_is_not_a_gesture(app: QApplication, w: MainWindow) -> None:
    """わずかな動きはジェスチャーにしない（右クリックメニューを邪魔しない）。"""
    before = w.tabs.count()
    lbl = label_of(w)
    lbl._skip_context_menu = False   # 直前のテストの状態を持ち込まない
    drag(app, lbl, QPoint(200, 200), QPoint(212, 205))   # 12px だけ
    check(w.tabs.count() == before, f"わずかな右ドラッグでは何も起きない（{w.tabs.count()}）")
    check(not lbl._skip_context_menu, "右クリックメニューは抑制されない")


def test_context_menu_suppressed_after_gesture(app: QApplication,
                                               w: MainWindow) -> None:
    """ジェスチャーの直後はメニューを出さない。"""
    lbl = label_of(w)
    idx = w.tabs.currentIndex()
    drag(app, lbl, QPoint(200, 200), QPoint(300, 200))   # → タブ移動
    check(lbl._skip_context_menu, "ジェスチャー後はメニューを抑制する")
    app.sendEvent(lbl, QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, QPoint(300, 200)))
    app.processEvents()
    check(not lbl._skip_context_menu, "抑制は1回だけ（次の右クリックでは出る）")
    check(w.tabs.currentIndex() != idx or w.tabs.count() == 1, "タブ移動は行われた")


def test_disabled_by_setting(app: QApplication, w: MainWindow) -> None:
    """設定で切ると、ジェスチャーは働かない。"""
    w.settings.setValue("gesture/enabled", False)
    w.apply_preferences()
    app.processEvents()
    check(not w.page_view.gesture_enabled(), "設定オフが各タブに伝わる")

    before = w.tabs.count()
    drag(app, label_of(w), QPoint(200, 180), QPoint(205, 320))   # ↓
    check(w.tabs.count() == before, f"オフならタブは閉じない（{w.tabs.count()}）")

    w.settings.setValue("gesture/enabled", True)
    w.apply_preferences()
    app.processEvents()
    check(w.page_view.gesture_enabled(), "設定を戻せば再び効く")


def test_default_is_on(w: MainWindow) -> None:
    check(prefs.DEFAULTS["gesture/enabled"] is True, "既定でジェスチャーは有効")


def main() -> int:
    app = QApplication(sys.argv)
    root = tempfile.mkdtemp(prefix="trv_gest_")
    try:
        w = MainWindow()
        w.resize(1100, 800)
        w.show()
        app.processEvents()

        test_default_is_on(w)
        test_next_and_prev_tab(app, w, root)
        test_up_goes_to_first_page(app, w)
        test_down_closes_tab(app, w)
        test_small_move_is_not_a_gesture(app, w)
        test_context_menu_suppressed_after_gesture(app, w)
        test_disabled_by_setting(app, w)

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
