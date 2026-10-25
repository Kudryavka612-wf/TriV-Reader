"""挿入した図形・テキスト・スタンプの「置く・選ぶ・動かす」の確認（実マウスイベント）。

利用者の困りごと:
- 挿入したあと、小さい注釈（1 文字のテキストなど）を掴んで動かそうとすると、枠の中ほぼ
  全部が拡大縮小のハンドルに当たり、動かさずに大きさが変わった
- 数 px だけずらしたくても、OS のドラッグ開始距離（約 10px）まで動かせず、動かし始めに飛んだ
- ドラッグ中の図形のプレビューが、倍率が 1 でないと確定後より細い（確定すると 1.5〜2 倍太い）。
  四角・円は確定すると枠線が内側に収まるのに、プレビューは線の中心がドラッグの線だった
- スタンプは、クリックした点が箱の左上で、置く前の見本もなく、一度で思った位置に置けなかった
- 動かしている最中は枠だけで、離したあとの見え方が分からなかった
- 矢印キーで微調整できなかった
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import viewer.page_view as PV
from viewer import theme
from viewer.document import PdfDocument
from viewer.page_view import PageView

results: list[bool] = []
app = QApplication(sys.argv)
theme.apply_theme(app, dark=False)


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def pump(n: int = 6) -> None:
    for _ in range(n):
        app.processEvents()


def settle(ms: int = 100) -> None:
    QTest.qWait(ms)
    app.processEvents()


def mouse(w, et, pt, buttons=None):
    button = Qt.MouseButton.LeftButton
    if buttons is None:
        buttons = button if et != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    app.sendEvent(w, QMouseEvent(et, QPointF(pt), QPointF(w.mapToGlobal(pt)), button, buttons,
                                 Qt.KeyboardModifier.NoModifier))


def move_to(w, pt):
    app.sendEvent(w, QMouseEvent(QEvent.Type.MouseMove, QPointF(pt), QPointF(w.mapToGlobal(pt)),
                                 Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                 Qt.KeyboardModifier.NoModifier))


def click(w, pt):
    mouse(w, QEvent.Type.MouseButtonPress, pt)
    mouse(w, QEvent.Type.MouseButtonRelease, pt)
    pump()


def drag(w, p0, p1, steps=6, hold=False):
    mouse(w, QEvent.Type.MouseButtonPress, p0)
    for i in range(1, steps + 1):
        p = p0 + (p1 - p0) * i / steps
        app.sendEvent(w, QMouseEvent(QEvent.Type.MouseMove, QPointF(p), QPointF(w.mapToGlobal(p)),
                                     Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                                     Qt.KeyboardModifier.NoModifier))
    if not hold:
        mouse(w, QEvent.Type.MouseButtonRelease, p1)
        pump()


def reddish(c: QColor) -> bool:
    return c.red() > 170 and c.green() < 120 and c.blue() < 120


def ink(img, pred, region=None):
    xs, ys = [], []
    x0, y0, x1, y1 = region or (0, 0, img.width(), img.height())
    for y in range(max(0, y0), min(img.height(), y1)):
        for x in range(max(0, x0), min(img.width(), x1)):
            if pred(img.pixelColor(x, y)):
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def fresh(doc: PdfDocument, pv: PageView) -> None:
    doc.clear_annots(0)
    pv.clear_selection()
    pv.refresh_page(0)
    pump()
    settle(120)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="trv_insux_")
    path = os.path.join(tmp, "p.pdf")
    d = fitz.open()
    d.new_page(width=595, height=842)
    d.save(path)
    d.close()
    doc = PdfDocument()
    doc.open(path)
    pv = PageView()
    pv.resize(1000, 1000)
    pv.set_document(doc)
    pv.show()
    pump()
    settle(150)
    lab = pv._labels[0]

    # --- 小さい注釈を掴んで動かす -------------------------------------------
    pv.set_zoom(1.0)
    settle(100)
    for text, rect in (("A", fitz.Rect(100, 500, 112, 515)),
                       ("あ", fitz.Rect(100, 500, 114, 516))):
        fresh(doc, pv)
        doc.add_freetext(0, rect, text, fontsize=12)
        pv.refresh_page(0)
        pump()
        settle(100)
        pv.set_tool(PV.TOOL_NONE)
        c = QPoint(int((rect.x0 + rect.x1) / 2), int((rect.y0 + rect.y1) / 2))
        click(lab, c)                       # まず選ぶ
        xr = pv._sel_xref
        before = fitz.Rect(doc.annot_rect(0, xr)) if xr else None
        drag(lab, c, c + QPoint(40, 30))    # 選んだ状態で、中を掴んで動かす
        after_x = pv._sel_xref
        after = fitz.Rect(doc.annot_rect(0, after_x)) if after_x else None
        ok = (before is not None and after is not None
              and abs(after.x0 - before.x0 - 40) < 1.5 and abs(after.y0 - before.y0 - 30) < 1.5
              and abs(after.width - before.width) < 0.5 and abs(after.height - before.height) < 0.5)
        check(f"a tiny one-character note ('{text}') moves by the mouse distance, not resized",
              ok, f"({before} -> {after})" if before and after else "(lost)")

    # 小さい枠でも四隅のハンドルは残る。大きい枠では 8 つ全部
    handles, radius = PV._active_handles((0, 0, 12, 15))
    check("a tiny box has no edge handles and a small hit radius",
          handles == {} or (set(handles) <= {0, 1, 2, 3} and radius <= 6), f"({handles}, {radius})")
    handles, radius = PV._active_handles((0, 0, 30, 20))
    check("a small box keeps only corner handles", set(handles) == {0, 1, 2, 3}, str(set(handles)))
    handles, radius = PV._active_handles((0, 0, 200, 120))
    check("a normal box keeps all eight handles and the normal hit radius",
          len(handles) == 8 and radius == PV.HANDLE_HIT)

    # --- 数 px の移動が効く -------------------------------------------------
    fresh(doc, pv)
    doc.add_freetext(0, fitz.Rect(100, 300, 220, 322), "テストの文字", fontsize=14)
    pv.refresh_page(0)
    pump()
    settle(100)
    c = QPoint(160, 311)
    click(lab, c)
    xr = pv._sel_xref
    b0 = fitz.Rect(doc.annot_rect(0, xr))
    drag(lab, c, c + QPoint(6, 0))
    a0 = fitz.Rect(doc.annot_rect(0, pv._sel_xref))
    check("a 6px drag moves the annotation by 6px (no dead zone, no jump)",
          abs(a0.x0 - b0.x0 - 6) < 1.0, f"(moved {a0.x0 - b0.x0:.1f})")

    # --- 矢印キーで微調整 ---------------------------------------------------
    x_before = fitz.Rect(doc.annot_rect(0, pv._sel_xref)).x0
    y_before = fitz.Rect(doc.annot_rect(0, pv._sel_xref)).y0
    pv.setFocus()
    QTest.keyClick(pv, Qt.Key.Key_Right)
    x1 = fitz.Rect(doc.annot_rect(0, pv._sel_xref)).x0
    QTest.keyClick(pv, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    r2 = fitz.Rect(doc.annot_rect(0, pv._sel_xref))
    check("the arrow key nudges the selected annotation by 1px, Shift by 10px",
          abs(x1 - x_before - 1.0) < 0.3 and abs(r2.y0 - y_before - 10.0) < 0.5,
          f"(x +{x1 - x_before:.1f}, y +{r2.y0 - y_before:.1f})")
    page_before = pv.index
    pv.clear_selection()
    QTest.keyClick(pv, Qt.Key.Key_Right)
    check("with nothing selected the arrow keys still turn pages (unchanged)",
          pv.index >= page_before)

    # --- 動かしている最中に、注釈の絵が動く先に見える ---------------------------
    fresh(doc, pv)
    doc.add_rect(0, fitz.Rect(100, 600, 200, 650), color=(1, 0, 0), width=3, fill=(1, 0, 0))
    pv.refresh_page(0)
    pump()
    settle(120)
    pv.set_tool(PV.TOOL_NONE)
    click(lab, QPoint(150, 625))                 # 塗りつぶしの四角を選ぶ
    drag(lab, QPoint(150, 625), QPoint(150, 625) + QPoint(120, 0), hold=True)
    pump()
    img = lab.grab().toImage()
    moved = ink(img, reddish, (230, 590, 330, 660))
    check("while dragging, the annotation itself is shown at the new position",
          moved is not None and moved[2] - moved[0] > 60, str(moved))
    mouse(lab, QEvent.Type.MouseButtonRelease, QPoint(270, 625))
    pump()

    # --- ドラッグ中のプレビューは、確定後と同じ太さ・同じ位置 -----------------------
    def column_thickness(img, x, y0, y1):
        return sum(1 for y in range(y0, y1) if reddish(img.pixelColor(x, y)))

    for z in (1.5, 2.0):
        pv.set_zoom(z)
        settle(120)
        lab = pv._labels[0]
        for tool in (PV.TOOL_RECT, PV.TOOL_LINE):
            fresh(doc, pv)
            pv.pen_width = 6
            pv.set_color(QColor(255, 0, 0))
            pv.set_tool(tool)
            pump()
            p0 = QPoint(int(100 * z) + 30, int(150 * z) + 30)
            p1 = QPoint(p0.x() + int(200 * z), p0.y() + (int(110 * z) if tool == PV.TOOL_RECT else 0))
            drag(lab, p0, p1, hold=True)
            pump()
            xm = (p0.x() + p1.x()) // 2
            t_prev = column_thickness(lab.grab().toImage(), xm, p0.y() - 25, p0.y() + 25)
            mouse(lab, QEvent.Type.MouseButtonRelease, p1)
            pump()
            settle(200)
            t_fin = column_thickness(lab.grab().toImage(), xm, p0.y() - 25, p0.y() + 25)
            check(f"zoom {z}: the {tool} preview is as thick as the result",
                  t_prev == t_fin and abs(t_fin - 6 * z) <= 1.5,
                  f"(preview {t_prev}px, final {t_fin}px)")
    # 四角の枠線は、ドラッグした範囲の内側に収まる（プレビューも同じ）
    pv.set_zoom(1.0)
    settle(120)
    lab = pv._labels[0]
    fresh(doc, pv)
    pv.pen_width = 8
    pv.set_tool(PV.TOOL_RECT)
    pump()
    p0, p1 = QPoint(200, 200), QPoint(400, 320)
    drag(lab, p0, p1, hold=True)
    pump()
    b_prev = ink(lab.grab().toImage(), reddish)
    mouse(lab, QEvent.Type.MouseButtonRelease, p1)
    pump()
    settle(200)
    b_fin = ink(lab.grab().toImage(), reddish)
    check("the rectangle preview has its outline inside the dragged area, like the result",
          b_prev and b_fin and all(abs(a - b) <= 1 for a, b in zip(b_prev, b_fin)),
          f"(preview {b_prev}, final {b_fin})")

    # --- スタンプ: 置く前の見本があり、クリックした点が箱の中心 -------------------
    fresh(doc, pv)
    pv.set_stamp("承認", QColor(220, 20, 20), 20)
    pump()
    c = QPoint(300, 400)
    move_to(lab, c)
    pump()
    ghost = ink(lab.grab().toImage(), reddish, (200, 330, 420, 470))
    w_pt, h_pt = (v + 2 for v in PdfDocument.stamp_size("承認", 20))   # 枠線の分
    check("before placing, a faint sample of the stamp follows the cursor",
          ghost is not None and abs((ghost[0] + ghost[2]) / 2 - c.x()) < 6
          and abs((ghost[1] + ghost[3]) / 2 - c.y()) < 6, str(ghost))
    click(lab, c)
    pv.clear_selection()
    settle(150)
    pg = doc._doc[0]
    st = [a for a in pg.annots() if a.type[1] == "FreeText"][0]
    cx, cy = (st.rect.x0 + st.rect.x1) / 2, (st.rect.y0 + st.rect.y1) / 2
    check("the stamp is placed with the clicked point at its centre",
          abs(cx - c.x()) < 1.5 and abs(cy - c.y()) < 1.5,
          f"(centre {cx:.1f},{cy:.1f}; click {c.x()},{c.y()})")
    check("…and its size is the one shown in the sample",
          abs(st.rect.width - w_pt) < 1.5 and abs(st.rect.height - h_pt) < 1.5,
          f"({st.rect.width:.1f}x{st.rect.height:.1f} vs {w_pt:.1f}x{h_pt:.1f})")

    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
