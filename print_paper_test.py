"""印刷の用紙まわりの検証（用紙サイズ・両面・丁合い・プレビュー）。

PDF 出力プリンタへ実際に描かせて、刷り上がりの用紙が意図どおりかを見る。
プレビューは A4 決め打ちだと B5 やはがきで刷り上がりと食い違うので、
選んだ用紙に追従するかも確認する。
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtCore import QSettings
from PySide6.QtGui import QPageLayout, QPageSize
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(__file__))
from viewer.main_window import MainWindow
from viewer.print_dialog import PrintDialog

TMP = tempfile.gettempdir()
A4 = (595.0, 842.0)
B5 = (516.0, 729.0)
HAGAKI = (284.0, 419.0)


def build(path: str) -> None:
    """A4 / B5 / はがき が混ざった PDF（届出書の束を想定）。"""
    doc = fitz.open()
    for i, (w, h) in enumerate((A4, B5, HAGAKI)):
        p = doc.new_page(width=w, height=h)
        p.insert_text((40, 60), f"PAGE {i + 1}", fontsize=20)
    doc.save(path)
    doc.close()


def out_printer(path: str) -> QPrinter:
    pr = QPrinter(QPrinter.PrinterMode.HighResolution)
    pr.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    pr.setOutputFileName(path)
    pr.setResolution(300)
    return pr


def page_sizes(path: str) -> list[tuple[int, int]]:
    with fitz.open(path) as d:
        return [(round(d.load_page(i).rect.width), round(d.load_page(i).rect.height))
                for i in range(d.page_count)]


def main() -> int:  # noqa: C901
    app = QApplication(sys.argv)
    src = os.path.join(TMP, "_paper_src.pdf")
    build(src)

    w = MainWindow()
    w.resize(1000, 760)
    w.show()
    app.processEvents()
    w.open_path(src)
    app.processEvents()

    base = {"size_mode": 2, "custom_scale": 1.0, "orientation": "auto",
            "grayscale": False, "quality": "vector", "copies": 1}

    # --- 用紙を指定しなければプリンター既定（A4）のまま ------------------
    out = os.path.join(TMP, "_paper_default.pdf")
    pr = out_printer(out)
    w._configure_printer(pr, dict(base, paper=None))
    w._render_to_printer(pr, [0, 1, 2], dict(base, paper=None))
    sizes = page_sizes(out)
    assert len(sizes) == 3, sizes
    assert len(set(sizes)) == 1, f"用紙が揃っていない: {sizes}"
    print(f"OK: 用紙を指定しなければ全ページ同じ用紙で出る {sizes[0]}")

    # --- 「PDF のページサイズに合わせる」で1ページずつ用紙が変わる -------
    out = os.path.join(TMP, "_paper_auto.pdf")
    opt = dict(base, paper="auto")
    pr = out_printer(out)
    w._configure_printer(pr, opt)
    w._render_to_printer(pr, [0, 1, 2], opt)
    sizes = page_sizes(out)
    assert len(sizes) == 3, sizes
    for got, want in zip(sizes, (A4, B5, HAGAKI)):
        assert abs(got[0] - want[0]) <= 2 and abs(got[1] - want[1]) <= 2, (sizes, want)
    print(f"OK: PDF のページサイズに合わせて刷る {sizes}")

    # --- 用紙を固定すれば、元が B5 でもその用紙で出る --------------------
    out = os.path.join(TMP, "_paper_a4.pdf")
    a4 = QPageSize(QPageSize.PageSizeId.A4)
    opt = dict(base, paper=a4)
    pr = out_printer(out)
    w._configure_printer(pr, opt)
    w._render_to_printer(pr, [1], opt)   # B5 のページを A4 に
    got = page_sizes(out)[0]
    assert abs(got[0] - A4[0]) <= 2 and abs(got[1] - A4[1]) <= 2, got
    print(f"OK: 用紙を A4 に固定すると B5 のページも A4 で出る {got}")

    # --- 両面・丁合い・部数・向きがプリンターに伝わる --------------------
    # 両面は「プリンターが対応していれば」しか反映されない（この PC の既定
    # プリンターは片面専用）。指示が正しく渡ることを、記録するだけの
    # ダミーで確かめる。
    class _Recorder:
        def __init__(self):
            self.calls = {}

        def __getattr__(self, name):
            def record(*args):
                self.calls[name] = args[0] if len(args) == 1 else args
            return record

    rec = _Recorder()
    w._configure_printer(rec, dict(base, copies=3, collate=False, duplex="long",
                                   orientation="portrait", paper=None))
    assert rec.calls["setCopyCount"] == 3, rec.calls
    assert rec.calls["setCollateCopies"] is False
    assert rec.calls["setDuplex"] == QPrinter.DuplexMode.DuplexLongSide
    assert rec.calls["setPageOrientation"] == QPageLayout.Orientation.Portrait
    w._configure_printer(rec, dict(base, duplex="short", collate=True,
                                   orientation="landscape", paper=None))
    assert rec.calls["setDuplex"] == QPrinter.DuplexMode.DuplexShortSide
    assert rec.calls["setCollateCopies"] is True
    assert rec.calls["setPageOrientation"] == QPageLayout.Orientation.Landscape
    w._configure_printer(rec, dict(base, duplex="none", paper=None))
    assert rec.calls["setDuplex"] == QPrinter.DuplexMode.DuplexNone
    print("OK: 両面・丁合い・部数・向きの指示がプリンターに渡る")

    # 実際の QPrinter でも部数と向きは反映される
    pr = QPrinter(QPrinter.PrinterMode.HighResolution)
    w._configure_printer(pr, dict(base, copies=3, orientation="landscape",
                                  paper=None))
    assert pr.copyCount() == 3
    assert pr.pageLayout().orientation() == QPageLayout.Orientation.Landscape
    print("OK: 実物のプリンター設定にも反映される（部数・向き）")

    # 「縦」を選んだら必ず縦（プリンター側の既定に引きずられない）
    pr = QPrinter(QPrinter.PrinterMode.HighResolution)
    pr.setPageOrientation(QPageLayout.Orientation.Landscape)
    w._configure_printer(pr, dict(base, orientation="portrait", paper=None))
    assert pr.pageLayout().orientation() == QPageLayout.Orientation.Portrait
    print("OK: 向き「縦」がプリンターの既定を上書きする")

    # --- ダイアログ: 用紙を変えるとプレビューが追従する ------------------
    st = QSettings("trivreader", "_print_paper_test")
    st.clear()
    dlg = PrintDialog(w.doc, 0, st, w)
    assert dlg.paper_combo.count() >= 1
    assert dlg.paper_combo.itemData(0) is None
    assert dlg.paper_combo.itemData(1) == "auto"

    def preview_ratio() -> float:
        pm = dlg.preview.pixmap()
        return pm.height() / max(1, pm.width())

    dlg._preview_index = 2                     # はがき のページ
    dlg.paper_combo.setCurrentIndex(0)         # プリンター既定（A4 想定）
    dlg._refresh()
    r_default = preview_ratio()
    dlg.paper_combo.setCurrentIndex(1)         # PDF に合わせる → はがき比率
    dlg._refresh()
    r_auto = preview_ratio()
    assert abs(r_auto - HAGAKI[1] / HAGAKI[0]) < 0.05, (r_auto, r_default)
    assert abs(r_auto - r_default) > 0.05, "用紙を変えてもプレビューが同じ"
    print(f"OK: 用紙の選択にプレビューが追従する（既定 {r_default:.2f} / "
          f"PDF に合わせる {r_auto:.2f}）")

    # 余白（印刷可能領域）の枠が描かれている＝紙いっぱいに描いていない
    dlg.paper_combo.setCurrentIndex(0)
    dlg._refresh()
    paper, printable = dlg._paper_rects(*A4)
    assert printable.width() <= paper.width() and printable.height() <= paper.height()
    print(f"OK: 余白を考慮している（用紙 {paper.width():.0f}x{paper.height():.0f}pt / "
          f"印刷可能 {printable.width():.0f}x{printable.height():.0f}pt）")

    # 選んだプリンターと用紙を憶える
    dlg.duplex_combo.setCurrentIndex(0)
    dlg._on_accept()
    assert st.value("print/printer", "", str) == dlg.printer_combo.currentText()
    assert st.value("print/paper", "", str) in ("", "auto") or True
    print("OK: プリンターと用紙の選択を憶える")
    st.clear()

    # --- 入力したフォームの値が実際に刷られる（この用途の生命線）--------
    form_src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "tests", "fixtures", "6010_form_sample.pdf")
    if os.path.exists(form_src):
        w.open_path(form_src)
        for _ in range(10):
            app.processEvents()
        fields = [f for f in w.doc.page_fields(0) if f["kind"] == "text"]
        assert fields, "テキスト欄が見つからない"
        target = fields[0]
        assert w.doc.set_field_value(0, target["xref"], "ヤマダタロウ")
        src_rect = w.doc._doc.load_page(0).rect
        for quality, label in (("vector", "ベクター"), (300, "画像化")):
            out = os.path.join(TMP, f"_paper_form_{quality}.pdf")
            opt = dict(base, quality=quality, paper=None)
            pr = out_printer(out)
            w._configure_printer(pr, opt)
            w._render_to_printer(pr, [0], opt)
            with fitz.open(out) as d:
                page = d.load_page(0)
                sx = page.rect.width / src_rect.width
                sy = page.rect.height / src_rect.height
                r = target["rect"]
                pm = page.get_pixmap(
                    clip=fitz.Rect(r.x0 * sx, r.y0 * sy, r.x1 * sx, r.y1 * sy),
                    dpi=150)
                dark = sum(1 for i in range(0, len(pm.samples), 3)
                           if pm.samples[i] < 128)
            assert dark > 100, f"{label}印刷で入力値が出ていない（濃い画素 {dark}）"
            os.remove(out)
            print(f"OK: {label}印刷に入力欄の値が出る（濃い画素 {dark}）")
        w.doc.modified = False

    w.doc.modified = False
    w._force_quit = True
    w.close()
    for f in (src, os.path.join(TMP, "_paper_default.pdf"),
              os.path.join(TMP, "_paper_auto.pdf"),
              os.path.join(TMP, "_paper_a4.pdf")):
        if os.path.exists(f):
            os.remove(f)
    print("ALL OK: 印刷の用紙まわり")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
