"""入力欄の値の正しさの確認（チェックボックスの二者択一・取り消し・選択欄・XFA・文字色）。

1. 同じ名前で書き出し値の違うチェックボックス（男=/M・女=/F）: 各ウィジェットは自分の
   ON の名前が欄の値（/V）と同じときだけ ON。以前は押した方にだけ /AS を書いたので、
   女を選ぶと男も ON の見た目のまま残り、両方にチェックが入って印刷された。
2. ページ操作（回転など）を取り消しても、そのあとに選んだラジオ・チェックボックスは残る。
   以前はラジオは黙って元に戻り、チェックボックスは同じ名前の最初の 1 つが ON になった。
3. 「フォームのクリア」「墨消しの適用」を取り消しても、そのあとに入れた値は残る。
4. 選択欄（エクスポート値, 表示名）は /V にエクスポート値を書く。開いたときも
   エクスポート値から表示名を選ぶ（余分な "01" の項目を足さない）。
5. XFA と AcroForm の両方を持つフォームは、値を変えて保存したら XFA を外す。
6. 文字色がグレー（1 成分）・CMYK（4 成分）の欄は、その色空間のまま外観に書く。
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
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox

from viewer import field_ap
from viewer.document import PdfDocument
from viewer.page_view import PageView, _FormCheckBox

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def _stream(d, content: bytes) -> int:
    x = d.get_new_xref()
    d.update_object(x, "<< /Type /XObject /Subtype /Form /BBox [0 0 20 20] >>")
    d.update_stream(x, content, new=True)
    return x


def make_buttons(path: str, as_values=("Off", "Off", "Off"), v: str | None = None) -> dict:
    """2 ページ。欄 "sex" のウィジェットは 男(/M, 1 ページ)・女(/F, 1 ページ)・女(/F, 2 ページ)。
    ラジオ "color"（R/G/B, 1 ページ）と文字欄 "memo"（1 ページ）も持つ。"""
    d = fitz.open()
    d.new_page(width=400, height=300)
    d.new_page(width=400, height=300)
    p1, p2 = d.page_xref(0), d.page_xref(1)
    off = _stream(d, b"0.9 g 0 0 20 20 re f")
    on = _stream(d, b"0 g 0 0 20 20 re f")
    field = d.get_new_xref()
    kids = []
    for (state, page, x), as_ in zip((("M", p1, 50), ("F", p1, 110), ("F", p2, 50)), as_values):
        k = d.get_new_xref()
        d.update_object(k, "<< /Type /Annot /Subtype /Widget /Parent %d 0 R /P %d 0 R "
                           "/Rect [%d 200 %d 220] /F 4 /AS /%s "
                           "/AP << /N << /%s %d 0 R /Off %d 0 R >> >> >>"
                        % (field, page, x, x + 20, as_, state, on, off))
        kids.append(k)
    d.update_object(field, "<< /FT /Btn /T (sex) /Kids [%s] %s >>"
                    % (" ".join(f"{k} 0 R" for k in kids), f"/V /{v}" if v else ""))
    rfield = d.get_new_xref()
    radios = []
    for i, n in enumerate(("R", "G", "B")):
        k = d.get_new_xref()
        d.update_object(k, "<< /Type /Annot /Subtype /Widget /Parent %d 0 R /P %d 0 R "
                           "/Rect [%d 100 %d 120] /F 4 /AS /Off "
                           "/AP << /N << /%s %d 0 R /Off %d 0 R >> >> >>"
                        % (rfield, p1, 50 + 60 * i, 70 + 60 * i, n, on, off))
        radios.append(k)
    d.update_object(rfield, "<< /FT /Btn /Ff 49152 /T (color) /Kids [%s] >>"
                    % " ".join(f"{k} 0 R" for k in radios))
    d.xref_set_key(p1, "Annots", "[%s]" % " ".join(
        f"{k} 0 R" for k in kids[:2] + radios))
    d.xref_set_key(p2, "Annots", "[%d 0 R]" % kids[2])
    d.xref_set_key(d.pdf_catalog(), "AcroForm",
                   "<< /Fields [%d 0 R %d 0 R] /DA (/Helv 0 Tf 0 g) >>" % (field, rfield))
    d.save(path)
    d.close()
    d = fitz.open(path)
    w = fitz.Widget()
    w.field_name, w.field_type = "memo", fitz.PDF_WIDGET_TYPE_TEXT
    w.rect, w.field_value = fitz.Rect(200, 40, 350, 60), ""
    w.text_font, w.text_fontsize = "Helv", 10
    d[0].add_widget(w)
    d.saveIncr()
    d.close()
    return {"field": field, "kids": kids, "rfield": rfield, "radios": radios}


def key(d, x, k):
    return d.xref_get_key(x, k)[1]


def test_checkbox_pair(app, tmp) -> None:
    src = os.path.join(tmp, "pair.pdf")
    ids = make_buttons(src)
    m, f1, f2 = ids["kids"]
    doc = PdfDocument()
    doc.open(src)
    d = doc._doc
    doc.set_field_value(0, m, True, name="sex")
    doc.set_field_value(0, f1, True, name="sex")
    check("1: choosing F after M: /V=/F, M is Off, both F widgets (also on page 2) are on",
          key(d, ids["field"], "V") == "/F" and key(d, m, "AS") == "/Off"
          and key(d, f1, "AS") == "/F" and key(d, f2, "AS") == "/F",
          f"(V={key(d, ids['field'], 'V')} M={key(d, m, 'AS')} F={key(d, f1, 'AS')} "
          f"F2={key(d, f2, 'AS')})")
    out = os.path.join(tmp, "pair_saved.pdf")
    doc.save_as(out)
    r = fitz.open(out)
    shown = sorted((r.xref_get_key(w.xref, "AS")[1] for p in r for w in p.widgets()
                    if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX))
    r.close()
    check("1: saved file prints only the F boxes as checked", shown == ["/F", "/F", "/Off"],
          str(shown))
    fs = {f["xref"]: f for f in doc.page_fields(0)}
    check("1: page_fields tells M unchecked, F checked",
          fs[m].get("checked") is False and fs[f1].get("checked") is True,
          str((fs[m].get("checked"), fs[f1].get("checked"))))
    doc.set_field_value(0, f1, False, name="sex")
    check("1: unchecking F turns every widget off",
          key(d, ids["field"], "V") == "/Off"
          and {key(d, k, "AS") for k in ids["kids"]} == {"/Off"})
    doc.close()

    # 以前の版で保存したファイル（/V=F なのに男の /AS も /M）
    bad = os.path.join(tmp, "pair_bad.pdf")
    ids = make_buttons(bad, as_values=("M", "F", "F"), v="F")
    doc = PdfDocument()
    doc.open(bad)
    m = ids["kids"][0]
    check("1: opening an old file with both boxes on shows (and prints) only F",
          key(doc._doc, m, "AS") == "/Off" and not doc.modified,
          f"(M={key(doc._doc, m, 'AS')} modified={doc.modified})")

    # 画面（本物のクリック）
    pv = PageView()
    pv.resize(700, 900)
    pv.set_document(doc)
    pv.set_form_mode(True)
    pv.show()
    for _ in range(10):
        app.processEvents()
    pv.set_zoom(1.0)
    QTest.qWait(300)
    app.processEvents()
    boxes = {c.property("trv_on") + str(c.property("trv_page")): c
             for c in pv._form_overlays if isinstance(c, _FormCheckBox)}
    check("1: on screen, M is unchecked and F is checked (value /F)",
          set(boxes) == {"M0", "F0", "F1"} and not boxes["M0"].isChecked()
          and boxes["F0"].isChecked() and boxes["F1"].isChecked(),
          str({k: c.isChecked() for k, c in boxes.items()}))
    QTest.mouseClick(boxes["M0"], Qt.MouseButton.LeftButton)
    app.processEvents()
    check("1: clicking M on screen unchecks both F boxes (screen and document)",
          boxes["M0"].isChecked() and not boxes["F0"].isChecked()
          and not boxes["F1"].isChecked()
          and key(doc._doc, ids["field"], "V") == "/M"
          and key(doc._doc, ids["kids"][2], "AS") == "/Off")
    pv.close()
    doc.close()


def test_undo_page_op_keeps_buttons(app, tmp) -> None:
    src = os.path.join(tmp, "undo_buttons.pdf")
    ids = make_buttons(src)
    m, f1, f2 = ids["kids"]
    r, g, b = ids["radios"]
    doc = PdfDocument()
    doc.open(src)
    doc.set_field_value(0, m, True, name="sex")
    doc.rotate_page(0, 90)
    doc.set_field_value(0, g, True, name="color")
    doc.set_field_value(0, f1, True, name="sex")
    doc.undo()
    d = doc._doc
    check("2: undoing a rotation keeps the radio chosen after it",
          d.page_count and d[0].rotation == 0 and key(d, ids["rfield"], "V") == "/G"
          and key(d, g, "AS") == "/G" and key(d, r, "AS") == "/Off",
          f"(rot={d[0].rotation} V={key(d, ids['rfield'], 'V')} G={key(d, g, 'AS')})")
    check("2: …and the checkbox value chosen after it (F, not the first same-named box)",
          key(d, ids["field"], "V") == "/F" and key(d, m, "AS") == "/Off"
          and key(d, f1, "AS") == "/F" and key(d, f2, "AS") == "/F",
          f"(V={key(d, ids['field'], 'V')} M={key(d, m, 'AS')} F={key(d, f1, 'AS')})")
    doc.close()


def text_value(doc, name):
    for p in doc._doc:
        for w in p.widgets():
            if w.field_name == name:
                return w.field_value
    return None


def make_texts(path: str) -> None:
    d = fitz.open()
    pg = d.new_page(width=400, height=400)
    for i, nm in enumerate(("a", "b", "c")):
        w = fitz.Widget()
        w.field_name, w.field_type = nm, fitz.PDF_WIDGET_TYPE_TEXT
        w.rect, w.field_value = fitz.Rect(50, 50 + 60 * i, 250, 70 + 60 * i), ""
        w.text_font, w.text_fontsize = "Helv", 10
        pg.add_widget(w)
    d.save(path)
    d.close()


def xref_of(doc, name):
    return next(f["xref"] for f in doc.page_fields(0) if f["name"] == name)


def test_undo_clear_keeps_later_input(app, tmp) -> None:
    src = os.path.join(tmp, "clear.pdf")
    make_texts(src)
    doc = PdfDocument()
    doc.open(src)
    doc.set_field_value(0, xref_of(doc, "a"), "A1", name="a")
    doc.set_field_value(0, xref_of(doc, "b"), "B1", name="b")
    doc.clear_form()
    doc.set_field_value(0, xref_of(doc, "b"), "B2", name="b")   # クリアのあとに入れた
    doc.undo()
    check("3: undoing 'clear form' brings back what the clear erased",
          text_value(doc, "a") == "A1", repr(text_value(doc, "a")))
    check("3: …but keeps what was typed after the clear (not silently thrown away)",
          text_value(doc, "b") == "B2", repr(text_value(doc, "b")))
    check("3: …and says so", "残し" in (doc.undo_notice or ""), repr(doc.undo_notice))
    doc.set_field_value(0, xref_of(doc, "c"), "C1", name="c")   # 取り消したあとに入れた
    doc.redo()
    check("3: redo clears again, keeping what was typed after the clear and after the undo",
          text_value(doc, "a") == "" and text_value(doc, "b") == "B2"
          and text_value(doc, "c") == "C1",
          repr((text_value(doc, "a"), text_value(doc, "b"), text_value(doc, "c"))))
    doc.close()

    doc = PdfDocument()
    doc.open(src)
    doc.set_field_value(0, xref_of(doc, "a"), "secret", name="a")
    doc.set_field_value(0, xref_of(doc, "b"), "B1", name="b")
    doc.add_redaction(0, fitz.Rect(40, 40, 260, 80))   # 欄 a にかかる
    doc.apply_redactions()
    doc.set_field_value(0, xref_of(doc, "b"), "B2", name="b")
    doc.undo()
    check("3: undoing a redaction brings the redacted value back, keeps later input",
          text_value(doc, "a") == "secret" and text_value(doc, "b") == "B2",
          repr((text_value(doc, "a"), text_value(doc, "b"))))
    doc.close()


def make_combo(path: str, value: str) -> None:
    d = fitz.open()
    pg = d.new_page(width=400, height=300)
    w = fitz.Widget()
    w.field_name, w.field_type = "pref", fitz.PDF_WIDGET_TYPE_COMBOBOX
    w.rect = fitz.Rect(50, 50, 250, 75)
    w.choice_values = [("01", "北海道"), ("02", "青森県")]
    w.field_value = value
    w.text_font, w.text_fontsize = "Helv", 10
    pg.add_widget(w)
    d.save(path)
    d.close()


def test_combo_export(app, tmp) -> None:
    src = os.path.join(tmp, "combo.pdf")
    make_combo(src, "01")
    doc = PdfDocument()
    doc.open(src)
    x = xref_of(doc, "pref")
    doc.set_field_value(0, x, "青森県", name="pref")
    d = doc._doc
    v = d.xref_get_key(x, "V")
    ap = d.xref_get_key(x, "AP/N")
    stream = d.xref_stream(int(ap[1].split()[0])) if ap[0] == "xref" else b""
    check("4: choosing a display name stores the export value in /V",
          v == ("string", "02"), str(v))
    check("4: …and the printed appearance still shows the display name",
          "青森県".encode("utf-16-be").hex().encode() in stream.lower(), stream[:120])
    doc.close()

    # 回転した欄（外観を MuPDF が作る）でも、印刷されるのは表示名
    rot = os.path.join(tmp, "combo_rot.pdf")
    d = fitz.open()
    pg = d.new_page(width=400, height=300)
    w = fitz.Widget()
    w.field_name, w.field_type = "pref", fitz.PDF_WIDGET_TYPE_COMBOBOX
    w.rect = fitz.Rect(50, 50, 80, 250)
    w.choice_values = [("01", "HOKKAIDO"), ("02", "AOMORI")]
    w.field_value = "01"
    pg.add_widget(w)
    rx = next(iter(pg.widgets())).xref
    d.xref_set_key(rx, "MK", "<< /R 90 >>")
    d.save(rot)
    d.close()
    doc = PdfDocument()
    doc.open(rot)
    doc.set_field_value(0, rx, "AOMORI", name="pref")
    text = doc._doc[0].get_text()
    check("4: a rotated combo stores 02 and still prints AOMORI (not 02)",
          doc._doc.xref_get_key(rx, "V") == ("string", "02") and "AOMORI" in text
          and "02" not in text, repr(text))
    doc.close()

    doc = PdfDocument()
    doc.open(src)        # Acrobat と同じく /V=01（エクスポート値）
    pv = PageView()
    pv.resize(700, 600)
    pv.set_document(doc)
    pv.set_form_mode(True)
    pv.show()
    for _ in range(10):
        app.processEvents()
    QTest.qWait(200)
    combo = next(c for c in pv._form_overlays if isinstance(c, QComboBox))
    items = [combo.itemText(i) for i in range(combo.count())]
    check("4: opening /V=01 selects 北海道 without adding a '01' item",
          combo.currentText() == "北海道" and items == ["北海道", "青森県"], str(items))
    combo.setCurrentText("青森県")
    app.processEvents()
    check("4: picking on screen stores 02", doc._doc.xref_get_key(
        xref_of(doc, "pref"), "V") == ("string", "02"))
    pv.close()
    doc.close()


def make_xfa(path: str) -> None:
    make_texts(path)
    d = fitz.open(path)
    x = d.get_new_xref()
    d.update_object(x, "<< >>")
    d.update_stream(x, b"<xdp:xdp><xfa:data><a>OLD</a></xfa:data></xdp:xdp>", new=True)
    d.xref_set_key(d.pdf_catalog(), "AcroForm/XFA", "%d 0 R" % x)
    d.saveIncr()
    d.close()


def test_xfa(app, tmp) -> None:
    src = os.path.join(tmp, "xfa.pdf")
    make_xfa(src)
    doc = PdfDocument()
    doc.open(src)
    out = os.path.join(tmp, "xfa_untouched.pdf")
    doc.save_as(out)
    r = fitz.open(out)
    kept = r.xref_get_key(r.pdf_catalog(), "AcroForm/XFA")[0] != "null"
    r.close()
    check("5: a hybrid form saved without changing values keeps its XFA", kept)
    doc.set_field_value(0, xref_of(doc, "a"), "NEW", name="a")
    out = os.path.join(tmp, "xfa_changed.pdf")
    doc.save_as(out)
    r = fitz.open(out)
    xfa = r.xref_get_key(r.pdf_catalog(), "AcroForm/XFA")
    val = next(w.field_value for w in r[0].widgets() if w.field_name == "a")
    r.close()
    check("5: after changing a value, the saved file has no stale XFA (Acrobat uses the value)",
          xfa[0] == "null" and val == "NEW", str((xfa, val)))
    doc.close()


def test_colors(app, tmp) -> None:
    content, _fs = field_ap.build("A", 60, 20, 10, color=(0.5,))
    check("9: a gray text color is written as gray (g), not as dark red",
          "0.500 g" in content and " rg" not in content, content.splitlines()[5:7])
    content, _fs = field_ap.build("A", 60, 20, 10, color=(0, 0, 0, 1))
    check("9: a CMYK text color is written as CMYK (k)",
          "0.000 0.000 0.000 1.000 k" in content, content.splitlines()[5:7])
    content, _fs = field_ap.build("A", 60, 20, 10, color=(1, 0, 0))
    check("9: RGB stays RGB", "1.000 0.000 0.000 rg" in content)

    src = os.path.join(tmp, "cmyk.pdf")
    make_texts(src)
    d = fitz.open(src)
    for w in d[0].widgets():
        if w.field_name == "a":
            d.xref_set_key(w.xref, "DA", "(/Helv 10 Tf 0 0 0 1 k)")
        if w.field_name == "b":
            d.xref_set_key(w.xref, "DA", "(/Helv 10 Tf 0.5 g)")
    d.saveIncr()
    d.close()
    doc = PdfDocument()
    doc.open(src)
    for nm, want in (("a", b"0.000 0.000 0.000 1.000 k"), ("b", b"0.500 g")):
        x = xref_of(doc, nm)
        doc.set_field_value(0, x, "abc", name=nm)
        ap = doc._doc.xref_get_key(x, "AP/N")
        stream = doc._doc.xref_stream(int(ap[1].split()[0]))
        check(f"9: a field whose /DA color is {want.decode()} keeps that color",
              want in stream, stream[:160])
    doc.close()


def main() -> int:
    app = QApplication(sys.argv)
    tmp = tempfile.mkdtemp(prefix="trv_fvfix_")
    try:
        for t in (test_checkbox_pair, test_undo_page_op_keeps_buttons,
                  test_undo_clear_keeps_later_input, test_combo_export, test_xfa,
                  test_colors):
            try:
                t(app, tmp)
            except Exception as exc:  # noqa: BLE001  1 つの失敗で残りを止めない
                import traceback
                traceback.print_exc()
                check(f"{t.__name__} ran without an error", False, repr(exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
