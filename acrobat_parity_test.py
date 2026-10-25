"""印刷と公文書（フォーム）入力を、Acrobat Reader と同じ振る舞いに揃えた点の回帰テスト。

印刷
- 紙の上の位置: 余白を 2 回足して右下へずれていた → 用紙の中央（実寸なら 1:1）
- PDF の印刷の指定: 画面だけの欄は刷らず、印刷だけの欄（_PRINT）は刷る
- 刷る中身: 文書と注釈 / 書き込みだけ残す / 文書のみ / 印刷済みの用紙に刷る
- ハイライトの下の文字がベクター印刷で消えない
- ページ指定は全角・和文の記号も読める。横長ページは A4 横として渡す
- 印刷ダイアログ: 誤りは日本語で見える所に出す。プレビューは刷るページだけ

フォーム
- 保存した後に入れた値も PDF に書き込まれる（以前は黙って消えた）
- カーソルを置いたまま保存しても、その欄の値が保存される
- パスワード付きの PDF は保存してもパスワードが外れない
- 金額のコム欄は右から詰める。チェック欄は四角のどこを押しても切り替わる
- 数字だけの欄は全角数字を半角に、小さいカナは大きいカナに直す
- 複数行の欄は Tab で次へ移り、文字数の上限を守る
- 都道府県の選択欄に見せかけの文字欄が被らない
- 拡大縮小しても、入力中の欄にカーソルが戻る

合成 PDF と同梱のフィクスチャ（tests/fixtures/6010_form_sample.pdf）を使う。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 利用者本人の設定（レジストリ）を書き換えないよう、使い捨ての INI で動かす
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")

import fitz
from PySide6.QtCore import QEvent, QMarginsF, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QInputMethodEvent, QPageLayout, QPageSize
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from viewer import theme  # noqa: E402
from viewer.mw_print import paper_for_page, place_on_paper, svg_is_safe  # noqa: E402
from viewer.util import parse_ranges  # noqa: E402

FORM = os.path.join(ROOT, "tests", "fixtures", "6010_form_sample.pdf")
MM = 72 / 25.4
results: list[bool] = []


def ime(app, widget, text: str) -> None:
    """日本語入力（IME）で確定したのと同じイベント。QTest.keyClicks は ASCII 以外を
    送ると offscreen で落ちるので、和文・全角はこちらで入れる。"""
    ev = QInputMethodEvent("", [])
    ev.setCommitString(text)
    app.sendEvent(widget, ev)
    app.processEvents()


def check(name: str, ok, extra: str = "") -> bool:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    return bool(ok)


def pdf_printer(path: str, margins=(0, 0, 0, 0)) -> QPrinter:
    pr = QPrinter(QPrinter.PrinterMode.HighResolution)
    pr.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    pr.setOutputFileName(path)
    pr.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    pr.setPageMargins(QMarginsF(*margins), QPageLayout.Unit.Millimeter)
    return pr


def ink_gaps_mm(path: str) -> tuple:
    """出力の 1 ページ目で、暗い画素の外接枠と紙の端との距離 (左, 上, 右, 下) mm。"""
    with fitz.open(path) as d:
        pm = d[0].get_pixmap(dpi=100)
    xs, ys = [], []
    for y in range(pm.height):
        row = pm.samples[y * pm.stride: y * pm.stride + pm.width * pm.n]
        for x in range(0, pm.width):
            if row[x * pm.n] < 100:
                xs.append(x)
                ys.append(y)
    k = 25.4 / 100
    return (min(xs) * k, min(ys) * k, (pm.width - 1 - max(xs)) * k,
            (pm.height - 1 - max(ys)) * k)


def color_share(path: str, pred) -> float:
    with fitz.open(path) as d:
        pm = d[0].get_pixmap(dpi=40)
    hit = tot = 0
    for i in range(0, len(pm.samples), pm.n * 2):
        tot += 1
        if pred(pm.samples[i], pm.samples[i + 1], pm.samples[i + 2]):
            hit += 1
    return hit / max(1, tot)


BASE = {"size_mode": 1, "custom_scale": 1.0, "orientation": "auto",
        "grayscale": False, "copies": 1, "paper": None, "quality": "vector"}


def test_print_math() -> None:
    paper = QRectF(0, 0, 100, 200)
    printable = QRectF(10, 10, 85, 185)   # 左上の余白が大きいプリンター
    x, y = place_on_paper(paper, printable, 100, 200)
    check("actual size: content sits on the paper 1:1 (not shifted by a margin)",
          (x, y) == (0, 0), f"({x}, {y})")
    x, y = place_on_paper(paper, printable, 50, 50)
    check("small content is centred on the paper", (x, y) == (25, 75), f"({x}, {y})")
    x, y = place_on_paper(paper, printable, 84, 100)
    check("content that fits the printable area is kept inside it",
          x >= printable.left() and x + 84 <= printable.right(), f"({x})")
    check("full-width page ranges are read (１－３、５)",
          parse_ranges("１－３、５", 10) == [0, 1, 2, 4])
    check("Japanese range marks are read (1～3 / 8-)",
          parse_ranges("1～3", 10) == [0, 1, 2] and parse_ranges("8-", 10) == [7, 8, 9])
    try:
        parse_ranges("abc", 10)
        msg = ""
    except ValueError as exc:
        msg = str(exc)
    check("a bad range gives a Japanese message", "ページ番号" in msg, msg)
    a4l = paper_for_page(842, 595)
    check("a landscape page is printed as A4 (rotated), not a custom paper size",
          a4l.id() == QPageSize.PageSizeId.A4, a4l.name())
    check("a page 1pt off A4 still uses A4", paper_for_page(596, 843).id()
          == QPageSize.PageSizeId.A4)


def build_flags_pdf(path: str) -> None:
    d = fitz.open()
    page = d.new_page(width=595, height=842)
    page.insert_text((60, 120), "Body text under a highlight", fontsize=28)
    hl = page.add_highlight_annot(fitz.Rect(55, 95, 450, 128))
    hl.set_colors(stroke=(1, 0.86, 0.18))
    hl.update()
    for rect, rgb, flags in ((fitz.Rect(60, 300, 200, 440), (1, 0, 0), 0),     # 画面だけ
                             (fitz.Rect(220, 300, 360, 440), (0, 0, 1), 36),   # 印刷だけ
                             (fitz.Rect(380, 300, 520, 440), (0, 0.7, 0), 4)):  # 両方
        a = page.add_rect_annot(rect)
        a.set_colors(stroke=rgb, fill=rgb)
        a.update()
        a.set_flags(flags)
        a.update()
    ft = page.add_freetext_annot(fitz.Rect(60, 600, 400, 640), "WRITTEN", fontsize=24,
                                 text_color=(0.6, 0, 0.6))
    ft.update()
    d.save(path)


def test_print(app, root: str) -> None:
    from viewer.main_window import MainWindow

    src = os.path.join(root, "frame.pdf")
    d = fitz.open()
    d.new_page(width=595, height=842).draw_rect(
        fitz.Rect(10 * MM, 10 * MM, 595 - 10 * MM, 842 - 10 * MM), color=(0, 0, 0), width=1)
    d.save(src)
    w = MainWindow()
    w.resize(1000, 760)
    w.show()
    app.processEvents()
    w.open_path(src)
    app.processEvents()
    out = os.path.join(root, "frame_out.pdf")
    pr = pdf_printer(out, margins=(8, 8, 3, 3))   # 左上の余白が大きい
    w._configure_printer(pr, BASE)
    w._render_to_printer(pr, [0], BASE)
    gaps = ink_gaps_mm(out)
    check("actual-size print lands 1:1 on the paper even with margins",
          all(abs(g - 10) < 1.0 for g in gaps), " / ".join(f"{g:.1f}" for g in gaps))

    flags = os.path.join(root, "flags.pdf")
    build_flags_pdf(flags)
    w.open_path(flags)
    app.processEvents()
    red = lambda r, g, b: r > 200 and g < 80 and b < 80          # noqa: E731
    blue = lambda r, g, b: b > 200 and r < 80 and g < 80          # noqa: E731
    green = lambda r, g, b: g > 120 and r < 80 and b < 80         # noqa: E731
    purple = lambda r, g, b: r > 100 and b > 100 and g < 60       # noqa: E731
    outs = {}
    for content in ("all", "filled", "doc", "fields"):
        o = os.path.join(root, f"flags_{content}.pdf")
        opt = dict(BASE, content=content)
        pr = pdf_printer(o)
        w._configure_printer(pr, opt)
        w._render_to_printer(pr, [0], opt)
        outs[content] = o
    o = outs["all"]
    check("screen-only annotations are not printed", color_share(o, red) < 0.002)
    check("print-only annotations are printed", color_share(o, blue) > 0.01)
    check("normal annotations are printed", color_share(o, green) > 0.01)
    o = outs["filled"]
    check("'書き込んだ文字・スタンプ' keeps typed text but drops other annotations",
          color_share(o, purple) > 0.0005 and color_share(o, green) < 0.002)
    o = outs["doc"]
    check("'文書のみ' drops all annotations",
          color_share(o, purple) < 0.0005 and color_share(o, green) < 0.002)
    o = outs["fields"]
    with fitz.open(o) as dd:
        dark_text = color_share(o, lambda r, g, b: r < 60 and g < 60 and b < 60)
    check("'印刷済みの用紙に刷る' prints the typed text but not the page itself",
          color_share(o, purple) > 0.0005 and dark_text < 0.0005, f"({dark_text:.4f})")

    check("pages with highlights are not drawn through the SVG path (it hides text)",
          not svg_is_safe(w.doc.page_svg(0)))
    with fitz.open(outs["all"]) as dd:
        pm = dd[0].get_pixmap(dpi=72, clip=fitz.Rect(60, 98, 440, 126))
    dark = sum(1 for i in range(0, len(pm.samples), pm.n) if pm.samples[i] < 90)
    check("vector print keeps the text under a highlight readable", dark > 40, f"({dark})")

    # 実際の官公庁フォーム: 画面用のコンボを刷らず、印刷用の _PRINT 欄（1 桁 1 マス）を刷る
    w.open_path(FORM)
    for _ in range(6):
        app.processEvents()
    doc = w.doc
    for name, val in (("FRM6010-01-005", "04"), ("FRM6010-01-006", "15")):
        xref = [x.xref for x in doc._doc[0].widgets() if x.field_name == name][0]
        doc.set_field_value(0, xref, val)
        doc.commit_govform_combo(0, xref, val)
    copy = doc.print_copy("all")
    flags_by_name = {x.field_name: int(copy._doc.xref_get_key(x.xref, "F")[1] or 0)
                     for x in copy._doc[0].widgets()}
    copy.close()
    check("form: the on-screen date combo is hidden when printing",
          flags_by_name["FRM6010-01-005"] & 2)
    check("form: the print-only date field (_PRINT) is printed",
          not flags_by_name["FRM6010-01-005_PRINT"] & 32
          and not flags_by_name["FRM6010-01-005_PRINT"] & 2)

    # ダイアログ: 誤りは見える所に日本語で、プレビューは刷るページだけ
    from viewer.print_dialog import PrintDialog

    w.open_path(src)
    app.processEvents()
    many = os.path.join(root, "many.pdf")
    dm = fitz.open()
    for i in range(6):
        dm.new_page(width=595, height=842).insert_text((72, 100), f"P{i + 1}", fontsize=30)
    dm.save(many)
    w.open_path(many)
    app.processEvents()
    dlg = PrintDialog(w.doc, 0, w.settings, w)
    dlg.show()
    app.processEvents()
    dlg.rb_range.setChecked(True)
    dlg.range_edit.setText("abc")
    app.processEvents()
    check("print dialog: a bad page range is explained in Japanese next to the box",
          dlg.range_error_label.isVisible() and "ページ番号" in dlg.range_error_label.text()
          and not dlg.print_button.isEnabled(), dlg.range_error_label.text())
    dlg.range_edit.setText("２，４－５")
    app.processEvents()
    check("print dialog: full-width ranges are accepted",
          dlg.print_button.isEnabled() and not dlg.range_error_label.isVisible())
    check("print dialog: the preview steps only through the pages that will print",
          dlg._preview_pages == [1, 3, 4], str(dlg._preview_pages))
    dlg._step_preview(1)
    check("print dialog: next sheet in the preview is the next printed page",
          dlg._preview_page() == 3 and "2 / 3" in dlg.page_pos_label.text(),
          dlg.page_pos_label.text())
    i = dlg.content_combo.findData("filled")
    dlg.content_combo.setCurrentIndex(i)
    check("print dialog: the content choice reaches the print options",
          dlg.options()["content"] == "filled")
    dlg.reject()

    # 両面: この PC に両面のプリンターが無くても確かめられるよう、ドライバーの
    # 答えを差し替える。既定はドライバーに従い、選んだものはプリンターごとに覚える
    import viewer.print_dialog as pdlg
    fake = {"複合機A": ([QPageSize(QPageSize.PageSizeId.A4)], True, "long"),
            "複合機B": ([QPageSize(QPageSize.PageSizeId.A4)], True, "none")}
    orig_caps = pdlg.printer_caps
    pdlg.printer_caps = lambda name: fake.get(name) or orig_caps(name)
    try:
        def open_dlg():
            d = PrintDialog(w.doc, 0, w.settings, w)
            for name in fake:
                d.printer_combo.addItem(name)
            return d

        dlg = open_dlg()
        dlg.printer_combo.setCurrentText("複合機A")
        app.processEvents()
        check("duplex: a printer whose driver defaults to duplex starts on duplex",
              dlg.duplex_combo.isEnabled() and dlg.duplex_combo.currentData() == "long")
        dlg.duplex_combo.setCurrentIndex(dlg.duplex_combo.findData("short"))
        dlg._on_accept()
        class Recorder:   # この PC の Qt は両面の指定を持たない（対応機が無い）
            def __getattr__(self, name):
                return lambda *a: calls.setdefault(name, a)
        calls: dict = {}
        w._configure_printer(Recorder(), dlg.options())
        check("duplex: the choice reaches the printer",
              calls.get("setDuplex") == (QPrinter.DuplexMode.DuplexShortSide,),
              str(calls.get("setDuplex")))
        dlg = open_dlg()
        dlg.printer_combo.setCurrentText("Microsoft Print to PDF")
        app.processEvents()
        dlg._on_accept()      # 両面のできないプリンターで刷っても…
        dlg = open_dlg()
        dlg.printer_combo.setCurrentText("複合機A")
        app.processEvents()
        check("duplex: the choice is remembered per printer (not reset by a simplex one)",
              dlg.duplex_combo.currentData() == "short")
        dlg.printer_combo.setCurrentText("複合機B")
        app.processEvents()
        check("duplex: another printer keeps its own driver default",
              dlg.duplex_combo.currentData() == "none")
        dlg.reject()
    finally:
        pdlg.printer_caps = orig_caps

    # 「プロパティ…」: Windows の印刷ダイアログで変えた部数・用紙がこちらに映る
    # （本物のダイアログは画面なしで操作できないので、同じ変え方をする代役で）
    import PySide6.QtPrintSupport as qps
    real_dialog = qps.QPrintDialog

    class DriverDialog:
        DialogCode = real_dialog.DialogCode

        def __init__(self, printer, parent=None):
            self.printer = printer

        def setWindowTitle(self, _t):
            pass

        def exec(self):
            self.printer.setCopyCount(3)
            self.printer.setPageSize(QPageSize(QPageSize.PageSizeId.A5))
            return real_dialog.DialogCode.Accepted

    qps.QPrintDialog = DriverDialog
    try:
        dlg = PrintDialog(w.doc, 0, w.settings, w)
        dlg.printer_combo.setCurrentText("Microsoft Print to PDF")
        app.processEvents()
        dlg._open_printer_properties()
        check("printer properties: copies and paper chosen there show up in the dialog",
              dlg.copies.value() == 3 and dlg.paper_combo.currentText()
              == QPageSize(QPageSize.PageSizeId.A5).name(),
              f"({dlg.copies.value()}, {dlg.paper_combo.currentText()})")
        check("printer properties: the same printer object (with its settings) is printed to",
              dlg.printer() is dlg._printer() and dlg.printer().copyCount() == 3)
        dlg.reject()
    finally:
        qps.QPrintDialog = real_dialog

    for i in range(w.tabs.count()):
        t = w.tabs.widget(i)
        if hasattr(t, "doc"):
            t.doc.modified = False
    w._force_quit = True
    w.close()


def test_forms(app, root: str) -> None:
    from viewer.document import PdfDocument
    from viewer.main_window import MainWindow
    from viewer.page_view import _CombLineEdit, _FormCheckBox, _FormTextArea

    src = os.path.join(root, "form.pdf")
    shutil.copy(FORM, src)
    w = MainWindow()
    w.resize(1100, 820)
    w.show()
    app.processEvents()
    w.open_path(src)
    for _ in range(10):
        app.processEvents()
    pv = w.page_view
    doc = w.doc
    w.settings.setValue("confirm_overwrite", False)

    def overlay(name):
        for c in pv._form_overlays:
            if c.property("trv_field") == name:
                return c
        return None

    def value_in_file(name):
        with fitz.open(src) as d:
            for p in d:
                for x in p.widgets():
                    if x.field_name == name:
                        return x.field_value
        return None

    def type_into(name, text, enter=True):
        c = overlay(name)
        pv.ensureWidgetVisible(c)
        c.setFocus()
        app.processEvents()
        if text.isascii():
            QTest.keyClicks(c, text)
        else:
            ime(app, c, text)
        if enter:
            QTest.keyClick(c, Qt.Key.Key_Return)
        app.processEvents()
        return c

    type_into("FRM6010-01-001", "1111111")
    w.save()
    app.processEvents()
    # 保存した後に入れた値も書き込まれる（以前は番号が付け直されて黙って消えた）
    type_into("FRM6010-01-002", "2222222")
    check("form: a value entered after saving reaches the document",
          doc.modified, f"(modified={doc.modified})")
    w.save()
    app.processEvents()
    check("form: …and the file", value_in_file("FRM6010-01-002") == "2222222",
          repr(value_in_file("FRM6010-01-002")))
    # カーソルを置いたまま（Enter を押さずに）保存しても、その欄の値が入る
    type_into("FRM6010-01-013", "霞が関", enter=False)
    w.save()
    app.processEvents()
    check("form: saving with the caret still in a field saves that field",
          value_in_file("FRM6010-01-013") == "霞が関", repr(value_in_file("FRM6010-01-013")))

    # 数字だけの欄: 全角数字は半角に、数字以外は入らない
    c = overlay("FRM6010-01-009")   # 郵便番号
    c.setFocus()
    c.clear()
    QTest.keyClicks(c, "AB")
    ime(app, c, "１２")
    QTest.keyClicks(c, "3")
    app.processEvents()
    app.processEvents()
    check("digits-only field: full-width digits become half-width, letters are dropped",
          c.text() == "123", repr(c.text()))
    check("the field's input hint is shown as a tooltip",
          "郵便番号" in (c.toolTip() or "") or bool(c.toolTip()), c.toolTip()[:30])
    # 小さいカナは大きく（フォームの ReplaceChisaiKana と同じ）
    c = overlay("FRM6010-01-007")
    c.setFocus()
    c.clear()
    ime(app, c, "ｷｮｳｶｲ")
    app.processEvents()
    app.processEvents()
    check("small kana become full-size kana (as the form asks)", c.text() == "ｷﾖｳｶｲ",
          repr(c.text()))

    # 金額のコム欄は右から詰める（画面と書き込みの両方）
    pg = 2
    amount = [f for f in doc.page_fields(pg) if f["name"] == "FRM6010-03-017"][0]
    c = overlay("FRM6010-03-017")
    check("amount comb field is right-aligned on screen",
          isinstance(c, _CombLineEdit) and c._first_cell(5) == amount["maxlen"] - 5,
          str(amount.get("align")))
    doc.set_field_value(pg, amount["xref"], "50000")
    widget = [x for x in doc._doc[pg].widgets() if x.field_name == "FRM6010-03-017"][0]
    apx = int(doc._doc.xref_get_key(widget.xref, "AP/N")[1].split()[0])
    import re
    xs = [float(x) for x in re.findall(r"1 0 0 1 ([\d.]+) [\d.]+ Tm",
                                       doc._doc.xref_stream(apx).decode())]
    cell = widget.rect.width / widget.text_maxlen
    check("…and in the written appearance (5 digits in the last 5 boxes)",
          len(xs) == 5 and xs[0] >= 2 * cell, str([round(x) for x in xs]))

    # チェック欄: 四角の真ん中を押しても切り替わる
    boxes = [c for c in pv._form_overlays if isinstance(c, _FormCheckBox)]
    if boxes:
        b = boxes[0]
        before = b.isChecked()
        QTest.mouseClick(b, Qt.MouseButton.LeftButton, pos=b.rect().center())
        app.processEvents()
        check("checkbox toggles when clicked in the middle of its box",
              b.isChecked() != before)

    # 複数行: Tab で次の欄へ、文字数の上限
    area = [c for c in pv._form_overlays if isinstance(c, _FormTextArea)
            and c._maxlen][0]
    area.setFocus()
    app.processEvents()
    QTest.keyClick(area, Qt.Key.Key_Tab)
    app.processEvents()
    check("Tab leaves a multi-line field (no tab character inserted)",
          not area.hasFocus() and "\t" not in area.toPlainText())
    area.setFocus()
    ime(app, area, "あ" * (area._maxlen + 10))
    app.processEvents()
    app.processEvents()
    check("multi-line field keeps to its character limit",
          len(area.toPlainText()) <= area._maxlen, f"({len(area.toPlainText())})")

    # 都道府県: 見せかけの文字欄が選択欄に被らない
    check("the prefecture dropdown is not covered by its print twin",
          overlay("FRM6010-01-011_PRINT") is None and overlay("FRM6010-01-011") is not None)

    # 拡大縮小しても、入力中の欄にカーソルが戻る
    c = overlay("FRM6010-01-008")
    pv.ensureWidgetVisible(c)
    c.setFocus()
    ime(app, c, "東京")
    app.processEvents()
    pv.set_zoom(pv.zoom * 1.2)
    pv._overlay_rebuild_timer.timeout.emit()
    app.processEvents()
    fw = QApplication.focusWidget()
    check("after zooming, the caret is back in the field being typed in",
          fw is not None and fw.property("trv_field") == "FRM6010-01-008",
          str(fw.property("trv_field") if fw else None))

    from PySide6.QtWidgets import QMessageBox
    QMessageBox.question = staticmethod(          # 閉じるときの「保存しますか」に答える
        lambda *a, **k: QMessageBox.StandardButton.Discard)
    doc.modified = False
    w._force_quit = True
    w.close()

    # パスワード付きの PDF は、保存してもパスワードが外れない
    enc = os.path.join(root, "enc.pdf")
    d = fitz.open()
    d.new_page().insert_text((72, 72), "secret")
    d.save(enc, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    pd = PdfDocument()
    pd.open(enc, password="pw")
    out = os.path.join(root, "enc_saved.pdf")
    pd.save_as(out)
    with fitz.open(out) as s:
        check("a password-protected PDF keeps its password after saving",
              bool(s.needs_pass) and bool(s.authenticate("pw")))
    pd.close()


def main() -> int:
    app = QApplication(sys.argv[:1])
    theme.apply_theme(app, dark=False)
    root = tempfile.mkdtemp(prefix="trv_parity_")
    try:
        test_print_math()
        test_print(app, root)
        test_forms(app, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
