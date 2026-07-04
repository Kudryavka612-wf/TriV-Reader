"""官公庁フォームの「選択」UI を、本物のマウス/ホイール操作で検証する統合テスト。

QtTest で実ウィジェット（PageView + フォームオーバーレイ）に実イベントを流し、
「クリックで選択できる」「選択で勝手にスクロールしない」「中クリック/ホイールで
誤入力しない」「コンボ選択が確定し壊れない」を確認する。GUI を可視化しなくても
（QT_QPA_PLATFORM=offscreen）Qt の実イベントループで動く。

サンプル PDF（傷病手当金 6010）が要る。パスは環境変数 TRIV_GOVFORM_PDF か
第1引数で指定。見つからなければスキップ（CI では PDF を同梱しないため）。

  python govform_interact_test.py [path/to/6010.pdf]
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox

from viewer import theme
from viewer.document import PdfDocument
from viewer.page_view import PageView, _FormComboBox, _GovFormClickOverlay


def _find_pdf() -> str | None:
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        return sys.argv[1]
    env = os.environ.get("TRIV_GOVFORM_PDF", "")
    if env and os.path.exists(env):
        return env
    # リポジトリ同梱のフィクスチャ（CI でも使える）
    fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "tests", "fixtures", "6010_form_sample.pdf")
    if os.path.exists(fixture):
        return fixture
    guess = os.path.join(os.path.expanduser("~"), "Downloads",
                         "6010_健康保険傷病手当金支給申請書.pdf")
    return guess if os.path.exists(guess) else None


def main() -> int:
    pdf = _find_pdf()
    if not pdf:
        print("SKIP: govform サンプル PDF が無いのでスキップ "
              "(TRIV_GOVFORM_PDF で指定可)")
        return 0

    app = QApplication(sys.argv[:1])
    theme.apply_theme(app, dark=False)
    doc = PdfDocument()
    doc.open(pdf)
    pv = PageView()
    pv.resize(900, 700)
    pv.set_document(doc)
    pv.set_zoom(1.4)
    pv.show()
    app.processEvents()
    pv.set_form_mode(True)
    app.processEvents()

    results: list[bool] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        results.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")

    def disp(name: str, page: int) -> int | None:
        for w in doc._doc.load_page(page).widgets():
            if w.field_name == name:
                return int(w.field_display)
        return None

    def overlay_for(name: str, page: int):
        for w in doc._doc.load_page(page).widgets():
            if w.field_name == name:
                x0, y0, x1, y1 = doc.pdf_rect_to_label(page, w.rect, pv._zoom)
                cx, cy = int((x0 + x1) / 2), int((y0 + y1) / 2)
                for ov in pv._form_overlays:
                    if (isinstance(ov, _GovFormClickOverlay)
                            and ov.geometry().contains(cx, cy)):
                        return ov
        return None

    # --- 勤務ページ(=3ページ目)の日付〇選択 ---------------------------------
    pv.set_page(2)
    app.processEvents()
    pv.ensureWidgetVisible(pv._labels[2], 0, 0)
    app.processEvents()

    # 左クリックで選択・スクロールしない（Issue#3）
    ov = overlay_for("FRM6010-03-004_5_OFF", 2)
    if ov is None:
        check("setup: day5 overlay", False)
    else:
        sb0 = pv.verticalScrollBar().value()
        before = disp("FRM6010-03-004_5_01", 2)
        QTest.mouseClick(ov, Qt.MouseButton.LeftButton)
        app.processEvents()
        after = disp("FRM6010-03-004_5_01", 2)
        check("left-click selects the date", before == 1 and after == 0,
              f"(disp {before}->{after})")
        check("no auto-scroll on select (#3)",
              sb0 == pv.verticalScrollBar().value())

    # 中クリックでは選択しない（Issue#4）
    ov = overlay_for("FRM6010-03-004_8_OFF", 2)
    if ov is not None:
        b = disp("FRM6010-03-004_8_01", 2)
        QTest.mouseClick(ov, Qt.MouseButton.MiddleButton)
        app.processEvents()
        check("middle-click does NOT select (#4)",
              b == disp("FRM6010-03-004_8_01", 2) == 1)
        if pv._auto_active:
            pv._stop_autoscroll()

    # ホイールは飲み込まず素通し＝スクロールできる（Issue#4）
    ov = overlay_for("FRM6010-03-004_10_OFF", 2)
    if ov is not None:
        b = disp("FRM6010-03-004_10_01", 2)
        gc = ov.mapToGlobal(ov.rect().center())
        we = QWheelEvent(QPointF(ov.rect().center()), QPointF(gc),
                         QPoint(0, -120), QPoint(0, -120),
                         Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                         Qt.ScrollPhase.NoScrollPhase, False)
        we.accept()
        app.sendEvent(ov, we)
        app.processEvents()
        check("wheel over cell does NOT select (#4)",
              b == disp("FRM6010-03-004_10_01", 2) == 1)
        check("overlay passes wheel through (#4)", not we.isAccepted())

    # --- 1ページ目の都道府県コンボ（Issue#1）--------------------------------
    pv.set_page(0)
    app.processEvents()
    pv.ensureWidgetVisible(pv._labels[0], 0, 0)
    app.processEvents()
    pref = None
    for c in pv._form_overlays:
        if isinstance(c, QComboBox):
            if any("北海道" in c.itemText(i) for i in range(c.count())):
                pref = c
                break
    if pref is None:
        check("setup: prefecture combo", False)
    else:
        pref.setCurrentText("北海道")
        app.processEvents()
        alive = any(w is pref for w in pv._form_overlays)
        printv = [w.field_value for w in doc._doc.load_page(0).widgets()
                  if w.field_name == "FRM6010-01-011_PRINT"]
        check("combo SURVIVES the pick (#1)", alive)
        check("道 branch label shown for 北海道 (#1)",
              disp("FRM6010-01-012_道", 0) == 0)
        check("_PRINT mirrored (#1)", bool(printv and printv[0].strip()))

    # --- 見た目・フォーカス（Issue#2/#3）-----------------------------------
    pv.set_page(2)
    app.processEvents()
    ov = next((w for w in pv._form_overlays
               if isinstance(w, _GovFormClickOverlay)), None)
    ss = ov.styleSheet() if ov else ""
    check("click cells have fill+dashed border (#2)",
          "rgba(112, 150, 216, 45)" in ss and "dashed" in ss)
    check("overlays are NoFocus (#2/#3)",
          ov is not None and ov.focusPolicy() == Qt.FocusPolicy.NoFocus)

    # --- ホイールでコンボの値が勝手に変わらない（スクロール中の誤入力防止）---
    pv.set_page(0)
    app.processEvents()
    combo = next((w for w in pv._form_overlays
                  if isinstance(w, _FormComboBox) and w.count() > 3), None)
    if combo is None:
        check("setup: a combo overlay", False)
    else:
        combo.setCurrentIndex(1)
        app.processEvents()
        before = combo.currentText()
        gc = combo.mapToGlobal(combo.rect().center())
        we = QWheelEvent(QPointF(combo.rect().center()), QPointF(gc),
                         QPoint(0, -120), QPoint(0, -120),
                         Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                         Qt.ScrollPhase.NoScrollPhase, False)
        we.accept()
        app.sendEvent(combo, we)
        app.processEvents()
        check("wheel over combo does NOT change its value",
              combo.currentText() == before, f"({before!r})")
        check("combo passes wheel through (scrolls page)", not we.isAccepted())

    # --- ズームは overlay を全再構築せず即応（重さの回帰防止）----------------
    import time
    pv.set_form_mode(True)
    app.processEvents()
    n_before = len(pv._form_overlays)
    t0 = time.perf_counter()
    pv.set_zoom(1.7)
    dt = (time.perf_counter() - t0) * 1000
    right_after = len(pv._form_overlays)   # デバウンス: 即時は0
    app.processEvents()
    time.sleep(0.2)
    app.processEvents()
    check("zoom is responsive (no synchronous full overlay rebuild)",
          dt < 300 and right_after == 0 and n_before > 100, f"({dt:.0f}ms)")
    check("overlays rebuilt after zoom settles",
          len(pv._form_overlays) > 100)

    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
