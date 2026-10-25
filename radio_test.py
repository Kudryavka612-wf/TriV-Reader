"""ラジオボタンと押しボタンの扱いの確認（官公庁フォームの「丸で選択」以外の、ふつうのフォーム）。

以前は、ラジオボタンにも押しボタンにも文字入力欄が重なり、何か打つと /V に文字が入った
（ラジオの /V は「/On状態名」でなければならないので、選択が壊れる。押しボタンは値を持たない）。
また、グループの 1 つを選んでも他が外れなかった。いまは、ラジオは丸いボタンとして選べて
（グループの 1 つだけが選ばれる）、押しボタンには何も重ねない。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import fitz
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from viewer import jpfonts
from viewer.document import PdfDocument
from viewer.page_view import PageView, _FormRadioButton

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def make(path: str) -> dict:
    """ラジオ 3 つのグループ "color"（R/G/B）・押しボタン "go"・文字欄 "memo" を持つ PDF。"""
    d = fitz.open()
    pg = d.new_page(width=400, height=300)

    def stream(content: bytes) -> int:
        x = d.get_new_xref()
        d.update_object(x, "<< /Type /XObject /Subtype /Form /BBox [0 0 20 20] >>")
        d.update_stream(x, content, new=True)
        return x

    off = stream(b"0.9 g 0 0 20 20 re f")
    ons = {n: stream(b"0 g 0 0 20 20 re f") for n in ("R", "G", "B")}   # ON は真っ黒の四角
    field = d.get_new_xref()
    kids = []
    for i, n in enumerate(("R", "G", "B")):
        k = d.get_new_xref()
        r = (50 + 60 * i, 200, 70 + 60 * i, 220)    # 上が 300 の PDF 座標 = 画面の y 80..100
        d.update_object(k, "<< /Type /Annot /Subtype /Widget /Parent %d 0 R /P %d 0 R "
                           "/Rect [%d %d %d %d] /F 4 /AS /Off "
                           "/AP << /N << /%s %d 0 R /Off %d 0 R >> >> >>"
                        % (field, pg.xref, *r, n, ons[n], off))
        kids.append(k)
    d.update_object(field, "<< /FT /Btn /Ff 49152 /T (color) /Kids [%s] >>"
                    % " ".join(f"{k} 0 R" for k in kids))
    push = d.get_new_xref()
    d.update_object(push, "<< /Type /Annot /Subtype /Widget /FT /Btn /Ff 65536 /T (go) "
                          "/P %d 0 R /Rect [50 120 130 150] /F 4 /MK << /CA (Go) >> >>" % pg.xref)
    d.xref_set_key(pg.xref, "Annots", "[%s %d 0 R]" % (" ".join(f"{k} 0 R" for k in kids), push))
    d.xref_set_key(d.pdf_catalog(), "AcroForm",
                   "<< /Fields [%d 0 R %d 0 R] /DA (/Helv 0 Tf 0 g) >>" % (field, push))
    d.save(path)
    d.close()
    d = fitz.open(path)
    w = fitz.Widget()
    w.field_name, w.field_type = "memo", fitz.PDF_WIDGET_TYPE_TEXT
    w.rect, w.field_value = fitz.Rect(200, 120, 350, 150), ""
    w.text_font, w.text_fontsize = "Helv", 12
    d[0].add_widget(w)
    d.saveIncr()
    d.close()
    return {"field": field, "kids": kids, "push": push}


def main() -> int:
    app = QApplication(sys.argv)   # noqa: F841
    tmp = tempfile.mkdtemp(prefix="trv_radio_")
    try:
        src = os.path.join(tmp, "radio.pdf")
        ids = make(src)
        doc = PdfDocument()
        doc.open(src)
        fs = doc.page_fields(0)
        radios = [f for f in fs if f.get("ftype") == fitz.PDF_WIDGET_TYPE_RADIOBUTTON]
        pushes = [f for f in fs if f.get("ftype") == fitz.PDF_WIDGET_TYPE_BUTTON]
        check("3 radio buttons and 1 push button are told apart",
              len(radios) == 3 and len(pushes) == 1, f"({len(radios)}, {len(pushes)})")
        check("each radio button knows its ON state name",
              sorted(f["on_state"] for f in radios) == ["B", "G", "R"],
              str([f["on_state"] for f in radios]))
        by = {f["on_state"]: f for f in radios}

        # --- 文書の層 -------------------------------------------------------------------
        check("a push button takes no value (typing into it writes nothing)",
              doc.set_field_value(0, pushes[0]["xref"], "abc", name="go") is False)
        check("(and its /V stays absent)",
              doc._doc.xref_get_key(pushes[0]["xref"], "V")[0] == "null")

        before = doc.render_page(0, 1.0).toImage()
        check("selecting a radio button works",
              doc.set_field_value(0, by["G"]["xref"], True, name="color") is True)
        after = doc.render_page(0, 1.0).toImage()
        g = by["G"]["rect"]
        px = (int((g.x0 + g.x1) / 2), int((g.y0 + g.y1) / 2))
        check("…the page shows it (render cache refreshed: the box turns black)",
              before.pixelColor(*px).red() > 150 and after.pixelColor(*px).red() < 60,
              f"({before.pixelColor(*px).red()} -> {after.pixelColor(*px).red()})")
        d = doc._doc
        check("/V is on the field, /AS on each button, the others are Off",
              d.xref_get_key(ids["field"], "V")[1] == "/G"
              and d.xref_get_key(by["G"]["xref"], "AS")[1] == "/G"
              and d.xref_get_key(by["R"]["xref"], "AS")[1] == "/Off"
              and d.xref_get_key(by["B"]["xref"], "AS")[1] == "/Off")
        doc.set_field_value(0, by["B"]["xref"], True, name="color")
        check("selecting another one turns the first off (only one is selected)",
              d.xref_get_key(ids["field"], "V")[1] == "/B"
              and d.xref_get_key(by["G"]["xref"], "AS")[1] == "/Off"
              and d.xref_get_key(by["B"]["xref"], "AS")[1] == "/B")
        check("clearing is refused (a radio group has no 'off by clicking the same one')",
              doc.set_field_value(0, by["B"]["xref"], False, name="color") is False
              and d.xref_get_key(ids["field"], "V")[1] == "/B")
        check("typing text into a radio button selects it; the text never lands in /V",
              doc.set_field_value(0, by["R"]["xref"], "いいえ", name="color") is True
              and d.xref_get_key(ids["field"], "V")[1] == "/R")

        out = os.path.join(tmp, "saved.pdf")
        doc.set_field_value(0, by["B"]["xref"], True, name="color")
        doc.save_as(out)
        r = fitz.open(out)
        ws = sorted((w for w in r[0].widgets() if w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON),
                    key=lambda w: w.rect.x0)                       # R, G, B の順
        saved_as = [r.xref_get_key(w.xref, "AS")[1] for w in ws]
        saved_v = r.xref_get_key(jpfonts.field_xref(r, ws[0].xref), "V")[1]
        r.close()
        check("saved file: the field's /V = /B, the buttons' /AS = Off, Off, B",
              saved_v == "/B" and saved_as == ["/Off", "/Off", "/B"], f"({saved_v} {saved_as})")
        r = fitz.open(out)
        sel = [w.field_value for w in r[0].widgets() if w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON]
        r.close()
        check("another reader (PyMuPDF) sees exactly one radio button selected",
              sum(1 for v in sel if v not in ("Off", "", None, False)) == 1, str(sel))
        doc.close()

        # --- 画面の層（本物のマウス操作） -----------------------------------------------
        doc = PdfDocument()
        doc.open(src)
        pv = PageView()
        pv.resize(700, 600)
        pv.set_document(doc)
        pv.set_form_mode(True)
        pv.show()
        for _ in range(10):
            app.processEvents()
        pv.set_zoom(1.0)
        QTest.qWait(300)
        app.processEvents()
        ovs = [c for c in pv._form_overlays]
        rb = [c for c in ovs if isinstance(c, _FormRadioButton)]
        edits = [c for c in ovs if isinstance(c, QLineEdit)]
        check("the 3 radio buttons are drawn as round buttons", len(rb) == 3, f"({len(rb)})")
        check("the text box 'memo' is still a text box, and the push button has no overlay",
              [c.property("trv_field") for c in edits] == ["memo"]
              and not any(c.property("trv_field") == "go" for c in ovs),
              str([c.property("trv_field") for c in ovs]))

        rb.sort(key=lambda c: c.property("trv_on"))   # B, G, R
        rb_b, rb_g, rb_r = rb
        QTest.mouseClick(rb_g, Qt.MouseButton.LeftButton)
        app.processEvents()
        check("clicking one selects it (and the others are off on screen)",
              rb_g.isChecked() and not rb_r.isChecked() and not rb_b.isChecked())
        check("…and the document has it", doc._doc.xref_get_key(ids["field"], "V")[1] == "/G")
        QTest.mouseClick(rb_r, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))   # 四角の隅でも反応
        app.processEvents()
        check("clicking another moves the selection (even at the corner of its box)",
              rb_r.isChecked() and not rb_g.isChecked()
              and doc._doc.xref_get_key(ids["field"], "V")[1] == "/R")
        QTest.mouseClick(rb_r, Qt.MouseButton.LeftButton)
        app.processEvents()
        check("clicking the selected one again keeps it selected",
              rb_r.isChecked() and doc._doc.xref_get_key(ids["field"], "V")[1] == "/R")
        check("the document is marked as changed", doc.modified)

        # 拡大・縮小で作り直しても、選択は保たれる
        pv.set_zoom(1.5)
        QTest.qWait(300)
        app.processEvents()
        rb2 = sorted((c for c in pv._form_overlays if isinstance(c, _FormRadioButton)),
                     key=lambda c: c.property("trv_on"))
        check("after zooming, the selection is kept",
              [c.isChecked() for c in rb2] == [False, False, True],
              str([c.isChecked() for c in rb2]))
        doc.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
