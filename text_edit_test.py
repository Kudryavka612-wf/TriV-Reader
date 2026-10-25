"""テキストの挿入・編集、注釈の掴み方、色の既定を、本物の入力で検証する。

「挿入・編集の粗が大きい」への対応の回帰テスト。合成 PDF を使うので外部
フィクスチャは不要（CI で常に走る）。

- クリックした所からそのまま打てる／打った分だけ欄が広がる
- 句読点の無い日本語も、確定後に枠の右で切れずに全部見える
- 入力欄で見えていた折り返しが、確定後の改行とそのまま一致する
- Esc・枠の外のクリックで確定（消えない）。続けて別の所をドラッグしても前の
  入力が上書きで消えない
- 既存のテキストはテキストツールのクリック／ダブルクリックでその場で直せる
- 線は線の上だけ、塗りの無い四角は枠の上だけで掴む
- 選ぶつもりのクリックで注釈が動かない
- ハイライトは 1 文字単位。ハイライトの上をドラッグすると文字を選ぶ
- 色の既定は 文字・図形=黒 / ハイライト=黄。新しいタブにもツールと色が引き継がれる
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 利用者本人の設定（レジストリ）を書き換えないよう、使い捨ての INI で動かす
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")

import fitz
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QFontDatabase, QInputMethodEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QColorDialog

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from viewer import page_view as PV  # noqa: E402
from viewer import theme  # noqa: E402
from viewer.document import FT_LINE, PdfDocument, ft_line_width  # noqa: E402
from viewer.page_view import PageView  # noqa: E402

TMP = tempfile.gettempdir()
results: list[bool] = []

JA_LONG = "日本語の文章を長く入力して折り返し位置を確認します句読点なしでも切れない"


def check(name: str, ok, extra: str = "") -> bool:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    return bool(ok)


def mouse(app, w, et, pt, button=Qt.MouseButton.LeftButton):
    app.sendEvent(w, QMouseEvent(
        et, QPointF(pt), QPointF(w.mapToGlobal(pt)), button,
        button if et != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


def click(app, w, pt):
    mouse(app, w, QEvent.Type.MouseButtonPress, pt)
    mouse(app, w, QEvent.Type.MouseButtonRelease, pt)
    app.processEvents()


def drag(app, w, p0, p1, steps=4):
    mouse(app, w, QEvent.Type.MouseButtonPress, p0)
    for i in range(1, steps + 1):
        p = p0 + (p1 - p0) * i / steps
        app.sendEvent(w, QMouseEvent(
            QEvent.Type.MouseMove, QPointF(p), QPointF(w.mapToGlobal(p)),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
    mouse(app, w, QEvent.Type.MouseButtonRelease, p1)
    app.processEvents()


def ime_commit(app, w, text):
    """日本語 IME で変換を確定したときと同じイベントを送る。"""
    ev = QInputMethodEvent("", [])
    ev.setCommitString(text)
    app.sendEvent(w, ev)
    app.processEvents()


def annots(doc, index=0):
    page = doc._doc.load_page(index)
    return page, list(page.annots())


def freetexts(doc, index=0):
    page, items = annots(doc, index)
    return page, [a for a in items if a.type[1] == "FreeText"]


def ft_xrefs(doc, index=0) -> list:
    page, items = freetexts(doc, index)
    return [a.xref for a in items]


def dark_rows(img, x0, x1, y0, y1):
    """img の矩形内で、暗い画素がある行の y の一覧。"""
    rows = []
    for y in range(max(0, int(y0)), min(img.height(), int(y1))):
        for x in range(max(0, int(x0)), min(img.width(), int(x1))):
            if QColor(img.pixel(x, y)).lightness() < 110:
                rows.append(y)
                break
    return rows


def build_pdf(path: str) -> None:
    d = fitz.open()
    page = d.new_page(width=500, height=700)
    page.insert_text((40, 640), "あいうえおかきくけこさしすせそ", fontsize=20,
                     fontname="japan")
    p2 = d.new_page(width=500, height=700)
    p2.insert_text((80, 400), "framed text", fontsize=18)   # 枠で囲む文字
    d.save(path)
    d.close()


# --- テキストの挿入 ---------------------------------------------------------
def test_click_to_type(app, pv, doc, lbl) -> None:
    pv.set_tool(PV.TOOL_TEXT)
    pv.set_text_size(16)
    n0 = len(ft_xrefs(doc))
    at = QPoint(60, 80)
    click(app, lbl, at)
    ed = pv._text_editor
    check("click (no drag) opens a text box right there", ed is not None and ed.auto_width)
    if ed is None:
        return
    org = ed.text_area_origin()
    check("the text starts at the clicked x", abs(org.x() - at.x()) <= 1, f"({org.x()})")
    w0 = ed.width()
    QTest.keyClicks(ed, "Hello")
    ime_commit(app, ed, "日本語")
    app.processEvents()
    check("the box grows sideways as you type", ed.width() > w0 + 40,
          f"({w0} -> {ed.width()})")
    check("typed text (keys + IME) is in the box", ed.toPlainText() == "Hello日本語",
          repr(ed.toPlainText()))
    check("the box is a single line (no wrap)", ed.wrapped_lines() == ["Hello日本語"],
          str(ed.wrapped_lines()))
    QTest.keyClick(ed, Qt.Key.Key_Escape)
    app.processEvents()
    page, fts = freetexts(doc)
    check("Esc finishes the entry and KEEPS the text", len(fts) == n0 + 1
          and pv._text_editor is None)
    ft = fts[-1]
    check("committed text is what was typed", doc.annot_text(0, ft.xref) == "Hello日本語")
    fs, tc, _fn = doc._parse_freetext_da(ft)
    check("new text defaults to black", max(tc) < 0.05, str(tc))
    r = ft.rect
    check("committed box starts where the text was", abs(r.x0 - at.x()) < 1.0,
          f"({r.x0:.1f})")
    need = ft_line_width("Hello日本語", 16)
    check("committed box is wide enough for the whole line", r.width >= need,
          f"({r.width:.1f} >= {need:.1f})")


def test_box_wraps_japanese(app, pv, doc, lbl) -> None:
    pv.set_tool(PV.TOOL_TEXT)
    pv.set_text_size(14)
    n0 = len(ft_xrefs(doc))
    drag(app, lbl, QPoint(60, 160), QPoint(220, 190))
    ed = pv._text_editor
    check("dragging opens a fixed-width box", ed is not None and not ed.auto_width)
    if ed is None:
        return
    ime_commit(app, ed, JA_LONG)
    app.processEvents()
    shown = ed.wrapped_lines()
    # 何行になるかは手元の和文フォントで多少変わる（CI に游ゴシックが無い等）
    check("the box wraps the long Japanese line", len(shown) >= 2, str(shown))
    check("the box grows downward to show every line",
          ed.height() >= len(shown) * FT_LINE * 14 - 2, f"({ed.height()}px)")
    # 枠の外をクリック → 確定（新しい欄は作らない）
    click(app, lbl, QPoint(400, 600))
    page, fts = freetexts(doc)
    check("clicking elsewhere commits the box", len(fts) == n0 + 1)
    check("…and does not open another box", pv._text_editor is None)
    ft = fts[-1]
    content = ft.info.get("content", "")
    check("the committed lines are exactly the lines seen while typing",
          content.split("\n") == shown, f"{content.split(chr(10))} vs {shown}")
    check("editing gets the original sentence back (no inserted breaks)",
          doc.annot_text(0, ft.xref) == JA_LONG)
    r = ft.rect
    widest = max(ft_line_width(s, 14) for s in shown)
    check("every line fits the box width (nothing is cut on the right)",
          widest <= r.width + 0.01, f"({widest:.1f} <= {r.width:.1f})")
    check("the box is tall enough for every line",
          r.height >= len(shown) * FT_LINE * 14, f"({r.height:.1f})")
    # 実際に描かせて、最後の行まで描かれているか（以前は 1 行目しか出なかった）
    img = doc.render_page(0, zoom=2.0).toImage()
    band = FT_LINE * 14 * 2
    last_top = r.y0 * 2 + band * (len(shown) - 1)
    rows = dark_rows(img, r.x0 * 2, r.x1 * 2, last_top, last_top + band)
    check("the last wrapped line is actually drawn", len(rows) > 4, f"({len(rows)} rows)")


def test_no_lost_text_when_dragging_next_box(app, pv, doc, lbl) -> None:
    pv.set_tool(PV.TOOL_TEXT)
    n0 = len(ft_xrefs(doc))
    drag(app, lbl, QPoint(60, 300), QPoint(260, 330))
    QTest.keyClicks(pv._text_editor, "first box")
    # 以前は、続けて別の所をドラッグすると前の欄が文字ごと捨てられていた
    drag(app, lbl, QPoint(60, 360), QPoint(260, 390))
    texts = [doc.annot_text(0, x) for x in ft_xrefs(doc)]
    check("starting the next box keeps the first one", "first box" in texts
          and len(texts) == n0 + 1, str(texts[-2:]))


def test_edit_existing(app, pv, doc, lbl) -> None:
    page, fts = freetexts(doc)
    target = [a for a in fts if doc.annot_text(0, a.xref) == "Hello日本語"][0]
    xref = target.xref
    r = fitz.Rect(target.rect)
    pv.set_tool(PV.TOOL_TEXT)
    n0 = len(fts)
    click(app, lbl, QPoint(int(r.x0 + 6), int((r.y0 + r.y1) / 2)))
    ed = pv._text_editor
    check("text tool click on existing text edits it (no new box)",
          ed is not None and pv._text_ctx.get("xref") is not None
          and ed.toPlainText() == "Hello日本語", repr(ed and ed.toPlainText()))
    if ed is None:
        return
    org = ed.text_area_origin()
    check("the edit box sits on the annotation",
          abs(org.x() - r.x0) <= 1.5, f"({org.x()} vs {r.x0:.1f})")
    check("editing shows the page without the old text underneath",
          ed._backdrop is not None)
    ed.moveCursor(ed.textCursor().MoveOperation.End)
    QTest.keyClicks(ed, " world")
    QTest.keyClick(ed, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    app.processEvents()
    page, fts = freetexts(doc)
    texts = [doc.annot_text(0, a.xref) for a in fts]
    check("Ctrl+Enter commits the edit in place", "Hello日本語 world" in texts
          and len(fts) == n0, str(texts))
    # 全部消して確定 → 注釈ごと消える
    tgt = [a for a in fts if doc.annot_text(0, a.xref) == "Hello日本語 world"][0]
    rr = fitz.Rect(tgt.rect)
    click(app, lbl, QPoint(int(rr.x0 + 4), int((rr.y0 + rr.y1) / 2)))
    ed = pv._text_editor
    ed.selectAll()
    QTest.keyClick(ed, Qt.Key.Key_Delete)
    QTest.keyClick(ed, Qt.Key.Key_Escape)
    app.processEvents()
    texts = [doc.annot_text(0, x) for x in ft_xrefs(doc)]
    check("clearing all text and committing removes the annotation",
          "Hello日本語 world" not in texts and len(texts) == n0 - 1, str(texts))
    pv.undo()
    app.processEvents()
    texts = [doc.annot_text(0, x) for x in ft_xrefs(doc)]
    check("…and Ctrl+Z brings it back", "Hello日本語 world" in texts)


def test_resize_rewraps(app, pv, doc) -> None:
    page, fts = freetexts(doc)
    ft = [a for a in fts if doc.annot_text(0, a.xref) == JA_LONG][0]
    r = fitz.Rect(ft.rect)
    n_before = len(ft.info["content"].split("\n"))
    new = doc.resize_annot(0, ft.xref, fitz.Rect(r.x0, r.y0, r.x0 + 330, r.y1))
    page = doc._doc.load_page(0)
    a = [x for x in page.annots() if x.xref == new][0]
    lines = a.info["content"].split("\n")
    check("widening a text box re-wraps it into fewer lines",
          len(lines) < n_before, f"({n_before} -> {len(lines)})")
    check("…and keeps the original sentence", doc.annot_text(0, new) == JA_LONG)
    new2 = doc.move_annot(0, new, 0, 40)
    page = doc._doc.load_page(0)
    b = [x for x in page.annots() if x.xref == new2][0]
    check("moving keeps the line breaks as they were",
          b.info["content"].split("\n") == lines)


# --- 注釈の掴み方 -----------------------------------------------------------
def test_hit_precision(app, pv, doc, lbl) -> None:
    idx = 1
    lbl2 = pv._labels[idx]
    doc.add_line(idx, fitz.Point(40, 100), fitz.Point(340, 250), color=(0, 0, 0), width=2)
    doc.add_rect(idx, fitz.Rect(60, 320, 260, 450), color=(0, 0, 0), width=2)
    doc.add_rect(idx, fitz.Rect(300, 320, 420, 450), color=(0, 0, 0), width=2,
                 fill=(0.8, 0.8, 1.0))
    pv._after_annot(idx)
    app.processEvents()
    kinds = {}
    page, items = annots(doc, idx)
    for a in items:
        kinds.setdefault(a.type[1], []).append(a.xref)
    line_x = kinds["Line"][0]
    open_sq, filled_sq = kinds["Square"][0], kinds["Square"][1]

    check("a click in the empty corner of a slanted line's box misses it",
          pv.hit_annot(idx, QPoint(300, 120)) is None)
    # 線上 (190,175) から 3px 外れた所
    check("a click 3px off a thin line still grabs it",
          pv.hit_annot(idx, QPoint(190, 172)) == line_x)
    check("on text inside an unfilled rectangle, the text wins (stays selectable)",
          pv.hit_annot(idx, QPoint(110, 393)) is None)
    check("an empty spot inside an unfilled rectangle still grabs it",
          pv.hit_annot(idx, QPoint(160, 345)) == open_sq)
    check("on the rectangle's border grabs it",
          pv.hit_annot(idx, QPoint(160, 322)) == open_sq)
    check("inside a filled rectangle grabs it",
          pv.hit_annot(idx, QPoint(360, 385)) == filled_sq)

    # 選ぶつもりのクリックで、手が少し動いても注釈がずれない
    pv.set_tool(PV.TOOL_NONE)
    before = fitz.Rect(doc.annot_rect(idx, filled_sq))
    drag(app, lbl2, QPoint(360, 385), QPoint(362, 386), steps=2)
    after = fitz.Rect(doc.annot_rect(idx, pv._sel_xref))
    check("a jittery click selects without moving", abs(after.x0 - before.x0) < 0.01
          and abs(after.y0 - before.y0) < 0.01, f"{before} -> {after}")
    drag(app, lbl2, QPoint(360, 385), QPoint(380, 405))
    moved = fitz.Rect(doc.annot_rect(idx, pv._sel_xref))
    check("a real drag moves it", abs(moved.x0 - before.x0 - 20) < 1.5, f"{moved}")

    pv.set_tool(PV.TOOL_ERASE)
    click(app, lbl2, QPoint(190, 172))
    page, items = annots(doc, idx)
    left = [a.type[1] for a in items]
    check("the eraser uses the same precise hit (line removed)", "Line" not in left,
          str(left))
    pv.set_tool(PV.TOOL_NONE)


def test_highlight_by_character(app, pv, doc, lbl) -> None:
    pv.set_tool(PV.TOOL_HIGHLIGHT)
    page, items = annots(doc)
    n0 = len(items)
    # 「あいうえお…」(20pt, x=40〜) の 3〜6 文字目あたりをなぞる
    y = 640 - 7
    drag(app, lbl, QPoint(40 + 20 * 2 + 3, y), QPoint(40 + 20 * 6 - 3, y))
    page, items = annots(doc)
    check("highlight tool adds one highlight", len(items) == n0 + 1)
    hl = items[-1]
    check("highlight covers only the dragged characters (not the whole line)",
          hl.type[1] == "Highlight" and hl.rect.width < 20 * 5.5
          and hl.rect.x0 > 40 + 20 * 1.5, f"{hl.rect}")
    c = (hl.colors or {}).get("stroke") or [0, 0, 0]
    check("highlight defaults to yellow", c[0] > 0.9 and c[1] > 0.7 and c[2] < 0.4,
          str(c))
    # ハイライトの上からドラッグ → 動かさずに文字を選ぶ
    pv.set_tool(PV.TOOL_NONE)
    before = fitz.Rect(hl.rect)
    drag(app, lbl, QPoint(40 + 20 * 3, y), QPoint(40 + 20 * 9, y))
    page, items = annots(doc)
    now = [a for a in items if a.type[1] == "Highlight"][-1]
    check("dragging across a highlight selects the text under it",
          pv.has_text_selection(), repr(pv.selection_text()))
    check("…and leaves the highlight where it was",
          abs(now.rect.x0 - before.x0) < 0.01)
    pv.clear_text_selection()


# --- 色の既定・タブ間の引き継ぎ・開いた時の倍率 ----------------------------
def test_window_defaults(app, root) -> None:
    from viewer.main_window import MainWindow

    a = os.path.join(root, "_te_a.pdf")
    b = os.path.join(root, "_te_b.pdf")
    build_pdf(a)
    build_pdf(b)
    w = MainWindow()
    w.resize(1100, 800)
    w.show()
    app.processEvents()
    check("default ink colour is black", w._annot_color.name() == "#000000")
    check("default highlight colour is yellow", w._highlight_color.name() == "#ffdb2e")

    w.settings.setValue("default_zoom_mode", "fit_height")
    w.open_path(a)
    for _ in range(4):
        app.processEvents()
    check("a newly opened PDF uses the chosen default (fit height)",
          w.page_view.fit_mode() == "height", str(w.page_view.fit_mode()))
    check("the zoom box says so", w.zoom_combo.currentText() == "縦に合わせる",
          w.zoom_combo.currentText())

    w.act_tool_text.trigger()
    orig = QColorDialog.getColor
    try:
        QColorDialog.getColor = staticmethod(lambda *a, **k: QColor(200, 0, 0))
        w._pick_color()           # テキストツール中 → 文字・図形の色
        w.act_tool_highlight.trigger()
        QColorDialog.getColor = staticmethod(lambda *a, **k: QColor(0, 200, 0))
        w._pick_color()           # ハイライト中 → ハイライトの色だけ
    finally:
        QColorDialog.getColor = orig
    check("picking a colour with the text tool changes text/shape colour",
          w._annot_color == QColor(200, 0, 0))
    check("picking with the highlighter changes only the highlight colour",
          w._highlight_color == QColor(0, 200, 0) and w._annot_color == QColor(200, 0, 0))
    w.act_tool_text.trigger()
    w.width_spin.setValue(6)
    w.open_path(b)            # 新しいタブ
    for _ in range(4):
        app.processEvents()
    pv = w.page_view
    check("a new tab starts with the tool chosen on the toolbar",
          pv.tool == PV.TOOL_TEXT, pv.tool)
    check("a new tab uses the chosen text colour (not yellow)",
          pv.color == QColor(200, 0, 0), pv.color.name())
    check("a new tab uses the chosen highlight colour",
          pv.highlight_color == QColor(0, 200, 0))
    check("a new tab uses the toolbar width", pv.pen_width == 6)
    w.width_spin.setValue(9)   # タブ b で変更 → タブ a に戻ると揃う
    w.tabs.setCurrentIndex(0)
    app.processEvents()
    check("switching tabs re-applies the toolbar settings",
          w.page_view.pen_width == 9 and w.page_view.tool == PV.TOOL_TEXT)

    # テキストツールでのダブルクリックで、開いた欄が閉じない
    pv = w.page_view
    lbl = pv._labels[0]
    at = QPoint(80, 90)
    click(app, lbl, at)
    app.sendEvent(lbl, QMouseEvent(
        QEvent.Type.MouseButtonDblClick, QPointF(at), QPointF(lbl.mapToGlobal(at)),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    check("a double-click with the text tool keeps the new box open",
          pv._text_editor is not None)
    # 打ちかけのまま上書き保存しても、その文字が保存される
    QTest.keyClicks(pv._text_editor, "typed before save")
    w.settings.setValue("confirm_overwrite", False)
    w.save()
    app.processEvents()
    saved = fitz.open(a)
    contents = [x.info.get("content", "") for x in saved[0].annots()]
    saved.close()
    check("saving while typing keeps the text being typed",
          "typed before save" in contents, str(contents))
    for i in range(w.tabs.count()):
        t = w.tabs.widget(i)
        if hasattr(t, "doc"):
            t.doc.modified = False
    w._force_quit = True
    w.close()


def main() -> int:
    app = QApplication(sys.argv[:1])
    # offscreen は既定でシステムのフォントを拾わない。実機に近づける
    for f in ("arial.ttf", "YuGothM.ttc", "meiryo.ttc", "msgothic.ttc"):
        p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", f)
        if os.path.exists(p):
            QFontDatabase.addApplicationFont(p)
    theme.apply_theme(app, dark=False)
    root = tempfile.mkdtemp(prefix="trv_textedit_")
    src = os.path.join(root, "_te_src.pdf")
    build_pdf(src)
    doc = PdfDocument()
    doc.open(src)
    pv = PageView()
    pv.resize(620, 820)
    pv.set_document(doc)
    pv.set_zoom(1.0)
    pv.show()
    app.processEvents()
    lbl = pv._labels[0]

    test_click_to_type(app, pv, doc, lbl)
    test_box_wraps_japanese(app, pv, doc, lbl)
    test_no_lost_text_when_dragging_next_box(app, pv, doc, lbl)
    test_edit_existing(app, pv, doc, lbl)
    test_resize_rewraps(app, pv, doc)
    test_hit_precision(app, pv, doc, lbl)
    test_highlight_by_character(app, pv, doc, lbl)

    # 保存して開き直しても、折り返しと元の文が残る
    out = os.path.join(root, "_te_out.pdf")
    doc.save_as(out)
    d2 = PdfDocument()
    d2.open(out)
    texts = [d2.annot_text(0, x) for x in ft_xrefs(d2)]
    check("after save + reopen, the original sentence is still there",
          JA_LONG in texts, str(texts))
    d2.close()
    doc.close()

    test_window_defaults(app, root)

    import shutil
    shutil.rmtree(root, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
