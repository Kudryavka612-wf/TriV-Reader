"""注釈の描画・選択編集を、本物のマウス操作で検証する統合テスト。

QtTest で実 PageView に矩形を描き、選択してから太さ・線種・不透明度・塗りを
変更し、注釈へ反映されること／新規描画にツールバー設定が効くこと／保存往復で
残ることを確認する。合成 PDF を使うので外部フィクスチャ不要（CI で常に走る）。
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtWidgets import QApplication

from viewer import theme
from viewer.document import PdfDocument
from viewer.page_view import (PageView, TOOL_LINE, TOOL_NONE, TOOL_RECT,
                              TOOL_TEXT, _TextAnnotEditor)


def main() -> int:
    tmp = tempfile.gettempdir()
    src = os.path.join(tmp, "_annot_blank.pdf")
    d = fitz.open()
    d.new_page(width=400, height=560)
    d.save(src)
    d.close()

    app = QApplication(sys.argv[:1])
    theme.apply_theme(app, dark=False)
    doc = PdfDocument()
    doc.open(src)
    pv = PageView()
    pv.resize(500, 650)
    pv.set_document(doc)
    pv.set_zoom(1.0)
    pv.show()
    app.processEvents()
    lbl = pv._labels[0]

    def drag(p0, p1):
        for et, pt in ((QEvent.Type.MouseButtonPress, p0),
                       (QEvent.Type.MouseMove, p1),
                       (QEvent.Type.MouseButtonRelease, p1)):
            app.sendEvent(lbl, QMouseEvent(
                et, QPointF(pt), QPointF(lbl.mapToGlobal(pt)),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier))
        app.processEvents()

    def last():
        r = None
        for x in doc._doc.load_page(0).annots():
            b = x.border or {}
            c = x.colors or {}
            r = {"w": b.get("width"), "dashes": b.get("dashes"),
                 "op": round(x.opacity, 2), "fill": c.get("fill"), "xref": x.xref}
        return r or {}

    results: list[bool] = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")

    # --- new draw honours the toolbar settings ---
    pv.set_tool(TOOL_RECT)
    pv.set_pen_width(9)
    pv.set_line_style("dashed")
    pv.set_opacity(0.4)
    pv.set_fill_enabled(True)
    pv.set_fill_color(QColor(0, 200, 0))
    drag(QPoint(40, 40), QPoint(240, 130))
    r = last()
    check("new rect: width applied", r.get("w") == 9.0, str(r))
    check("new rect: dash applied", bool(r.get("dashes")))
    check("new rect: opacity applied", r.get("op") == 0.4)
    check("new rect: fill applied", r.get("fill") is not None)

    pv.set_tool(TOOL_LINE)
    pv.set_pen_width(4)
    pv.set_line_style("dotted")
    drag(QPoint(40, 200), QPoint(240, 250))
    r = last()
    check("new line: width applied", r.get("w") == 4.0, str(r))
    check("new line: dash applied", bool(r.get("dashes")))

    # --- restyle an EXISTING selected annotation ---
    pv.set_tool(TOOL_RECT)
    pv.set_pen_width(2)
    pv.set_line_style("solid")
    pv.set_opacity(1.0)
    pv.set_fill_enabled(False)
    drag(QPoint(40, 320), QPoint(240, 420))
    xref = last()["xref"]
    pv.set_tool(TOOL_NONE)
    pv.set_selection(0, xref)
    pv.set_pen_width(12)
    check("restyle selected: width", last().get("w") == 12.0, str(last()))
    pv.set_line_style("dashed")
    check("restyle selected: dash", bool(last().get("dashes")))
    pv.set_opacity(0.5)
    check("restyle selected: opacity", last().get("op") == 0.5)
    pv.set_fill_enabled(True)
    pv.set_fill_color(QColor(200, 0, 0))
    check("restyle selected: fill", last().get("fill") is not None)
    check("restyle keeps width through later edits", last().get("w") == 12.0, str(last()))

    # --- in-place text annotation (type live, size + colour) ---
    from PySide6.QtGui import QColor as _QC
    pv.set_tool(TOOL_TEXT)
    pv.set_color(_QC(200, 0, 0))
    pv.set_text_size(24)
    drag(QPoint(40, 460), QPoint(300, 500))
    ed = pv._text_editor
    check("text tool opens an in-place editor", isinstance(ed, _TextAnnotEditor))
    check("editor font follows text size", ed is not None and ed.font().pixelSize() == 24)
    ed.setPlainText("メモ テスト")
    pv.set_text_size(30)   # live change while editing must not commit
    check("size change mid-edit keeps editing", pv._text_editor is ed
          and ed.font().pixelSize() == 30)
    ed._finish()
    app.processEvents()
    ft = None
    for a in doc._doc.load_page(0).annots():
        if a.type[1] == "FreeText":
            ft = a
    check("text commits to a FreeText annot", ft is not None)
    check("FreeText holds the typed text",
          ft is not None and ft.info.get("content") == "メモ テスト")
    check("editor is gone after commit", pv._text_editor is None)

    # --- round-trip persistence ---
    out = os.path.join(tmp, "_annot_restyled.pdf")
    doc.save_as(out)
    d2 = fitz.open(out)
    widths = sorted((a.border or {}).get("width", 0) for a in d2.load_page(0).annots())
    d2.close()
    check("round-trip: restyled width (12) persists", 12.0 in widths, str(widths))

    for f in (src, out):
        if os.path.exists(f):
            os.remove(f)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
