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
from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QContextMenuEvent, QCursor, QMouseEvent
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


def make_window(app: QApplication, ini: str) -> MainWindow:
    """設定をテスト用 INI に向けたウィンドウ（実環境の設定を汚さない）。"""
    w = MainWindow()
    w.settings = QSettings(ini, QSettings.Format.IniFormat)
    w.resize(1100, 800)
    w.show()
    app.processEvents()
    return w


def test_custom_assignment(app: QApplication, w: MainWindow) -> None:
    """割り当てを変えると、その操作が行われる。"""
    w.settings.setValue("gesture/up", "next_page")
    w.settings.setValue("gesture/down", "prev_page")
    w.page_view.set_page(1)
    app.processEvents()

    drag(app, label_of(w), QPoint(200, 300), QPoint(205, 180))   # ↑
    check(w.page_view.index == 2, f"↑ に割り当てた「次のページ」が効く（{w.page_view.index + 1}）")
    drag(app, label_of(w), QPoint(200, 180), QPoint(205, 300))   # ↓
    check(w.page_view.index == 1, f"↓ に割り当てた「前のページ」が効く（{w.page_view.index + 1}）")

    tabs = w.tabs.count()
    check(tabs == w.tabs.count(), f"↓ でタブは閉じない（{w.tabs.count()}）")


def test_none_does_nothing(app: QApplication, w: MainWindow) -> None:
    """「何もしない」を割り当てたら、そのジェスチャーは無視される。"""
    w.settings.setValue("gesture/left", "none")
    idx = w.tabs.currentIndex()
    page = w.page_view.index
    drag(app, label_of(w), QPoint(300, 200), QPoint(180, 205))   # ←
    check(w.tabs.currentIndex() == idx and w.page_view.index == page,
          "「何もしない」割り当てでは何も起きない")


def test_dialog_roundtrip(app: QApplication, w: MainWindow) -> None:
    """設定ダイアログから割り当てを変えると保存され、すぐ効く。"""
    from viewer.settings_dialog import SettingsDialog

    dlg = SettingsDialog(w)
    dlg.gesture_combos["right"].setCurrentIndex(
        dlg.gesture_combos["right"].findData("last_page"))
    dlg.apply()
    app.processEvents()
    check(prefs.value(w.settings, "gesture/right") == "last_page",
          "ダイアログで選んだ割り当てが保存される")

    w.page_view.set_page(0)
    app.processEvents()
    drag(app, label_of(w), QPoint(200, 200), QPoint(320, 205))   # →
    last = w.doc.page_count - 1
    check(w.page_view.index == last,
          f"→ に割り当てた「最終ページ」が効く（{w.page_view.index + 1}/{last + 1}）")

    # 既定へ戻して以降のテストに影響させない
    for key, action in (("up", "first_page"), ("down", "close_tab"),
                        ("left", "prev_tab"), ("right", "next_tab")):
        w.settings.setValue(f"gesture/{key}", action)


def test_disabled_greys_out_rows(w: MainWindow) -> None:
    """ジェスチャーを使わない設定なら、割り当ての選択も触れなくなる。"""
    from viewer.settings_dialog import SettingsDialog

    dlg = SettingsDialog(w)
    dlg.cb_gesture.setChecked(False)
    check(not dlg.gesture_box.isEnabled(), "オフにすると割り当て欄が無効になる")
    dlg.cb_gesture.setChecked(True)
    check(dlg.gesture_box.isEnabled(), "オンに戻すと選べる")


def test_default_assignment() -> None:
    """既定の割り当て。"""
    d = prefs.DEFAULTS
    check((d["gesture/up"], d["gesture/down"], d["gesture/left"],
           d["gesture/right"]) == ("first_page", "close_tab", "prev_tab", "next_tab"),
          "既定は ↑先頭 / ↓閉じる / ←前のタブ / →次のタブ")


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


def test_autoscroll_step(w: MainWindow) -> None:
    """デッドゾーンの内側では動かさず、外に出た分だけ比例して速くなる。"""
    pv = w.page_view
    check(pv._auto_step(0) == 0 and pv._auto_step(pv.AUTO_DEADZONE) == 0,
          "デッドゾーン内では動かない")
    far = pv._auto_step(pv.AUTO_DEADZONE + 80)
    near = pv._auto_step(pv.AUTO_DEADZONE + 8)
    check(far > near > 0, f"遠いほど速い（{near} → {far}）")
    check(pv._auto_step(-(pv.AUTO_DEADZONE + 80)) == -far, "逆向きも同じだけ動く")


def test_autoscroll_moves_both_axes(app: QApplication, w: MainWindow) -> None:
    """中クリックのスクロールが上下だけでなく左右にも効く。"""
    pv = w.page_view
    pv.set_zoom(3.0)          # 横にはみ出させる
    app.processEvents()
    hbar, vbar = pv.horizontalScrollBar(), pv.verticalScrollBar()
    check(hbar.maximum() > 0, f"（前提）横スクロールできる状態（max={hbar.maximum()}）")
    hbar.setValue(hbar.maximum() // 2)
    vbar.setValue(vbar.maximum() // 2)
    hx, vy = hbar.value(), vbar.value()

    # カーソルは動かせないので、起点を左上にずらして「右下へ引いた」状態を作る
    cur = pv.viewport().mapFromGlobal(QCursor.pos())
    pv._auto_active = True
    pv._auto_origin = QPoint(cur.x() - 120, cur.y() - 120)
    pv._auto_scroll_tick()
    check(vbar.value() > vy, f"下向きに動く（{vy} → {vbar.value()}）")
    check(hbar.value() > hx, f"右向きにも動く（{hx} → {hbar.value()}）")

    hx, vy = hbar.value(), vbar.value()
    pv._auto_origin = QPoint(cur.x() + 120, cur.y() + 120)   # 左上へ引いた状態
    pv._auto_scroll_tick()
    check(vbar.value() < vy, f"上向きに動く（{vy} → {vbar.value()}）")
    check(hbar.value() < hx, f"左向きにも動く（{hx} → {hbar.value()}）")

    pv._auto_active = False
    pv.fit_width()
    app.processEvents()


def test_autoscroll_toggle(app: QApplication, w: MainWindow) -> None:
    """中クリックで開始し、もう一度で止まる。"""
    pv = w.page_view
    lbl = label_of(w)
    for et in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        app.sendEvent(lbl, QMouseEvent(
            et, QPointF(QPoint(200, 200)), QPointF(lbl.mapToGlobal(QPoint(200, 200))),
            Qt.MouseButton.MiddleButton, Qt.MouseButton.MiddleButton,
            Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    check(pv._auto_active, "中クリックでオートスクロールが始まる")
    check(pv._auto_marker.isVisible(), "起点マーカーが出る")
    check(pv.viewport().cursor().shape() == Qt.CursorShape.SizeAllCursor,
          "カーソルが全方向になる")

    app.sendEvent(lbl, QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(QPoint(220, 240)),
        QPointF(lbl.mapToGlobal(QPoint(220, 240))),
        Qt.MouseButton.MiddleButton, Qt.MouseButton.MiddleButton,
        Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    check(not pv._auto_active, "もう一度クリックすると止まる")


def test_default_is_on(w: MainWindow) -> None:
    check(prefs.DEFAULTS["gesture/enabled"] is True, "既定でジェスチャーは有効")


def main() -> int:
    app = QApplication(sys.argv)
    root = tempfile.mkdtemp(prefix="trv_gest_")
    try:
        w = make_window(app, os.path.join(root, "TriVReader.ini"))

        test_default_is_on(w)
        test_default_assignment()
        test_next_and_prev_tab(app, w, root)
        test_up_goes_to_first_page(app, w)
        test_down_closes_tab(app, w)
        test_small_move_is_not_a_gesture(app, w)
        test_context_menu_suppressed_after_gesture(app, w)
        test_custom_assignment(app, w)
        test_none_does_nothing(app, w)
        test_dialog_roundtrip(app, w)
        test_disabled_greys_out_rows(w)
        test_disabled_by_setting(app, w)
        test_autoscroll_step(w)
        test_autoscroll_moves_both_axes(app, w)
        test_autoscroll_toggle(app, w)

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
