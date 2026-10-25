"""チェックボックスの値（/V）を、正しい辞書に書くことの確認。

以前は「親があれば親に書く」と決め打ちしていた。Acrobat や LiveCycle のフォームのように
チェックボックスが自分で名前（/T）を持ち、その上に /T 付きの途中ノードがある構造では、
途中ノードに /V が書かれ、本人の値は Off のまま、/V を持たない兄弟が全部 ON と読めた
（再オープンした後、ほかの読み手でも）。値は「名前 /T のある辞書」に書く。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz
from PySide6.QtWidgets import QApplication

from viewer.document import PdfDocument

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def make(path: str, hierarchical: bool) -> None:
    d = fitz.open()
    pg = d.new_page()
    for i, nm in enumerate(["cb1", "cb2", "cb3"]):
        w = fitz.Widget()
        w.field_name, w.field_type = nm, fitz.PDF_WIDGET_TYPE_CHECKBOX
        w.rect, w.field_value = fitz.Rect(72, 100 + 40 * i, 90, 118 + 40 * i), "Off"
        pg.add_widget(w)
    xs = [w.xref for w in pg.widgets()]
    if hierarchical:
        form = d.get_new_xref()
        d.update_object(form, "<< /T (form1) /Kids [] >>")
        page = d.get_new_xref()
        d.update_object(page, "<< /T (Page1) /Parent %d 0 R /Kids [%s] >>"
                        % (form, " ".join(f"{x} 0 R" for x in xs)))
        d.xref_set_key(form, "Kids", "[%d 0 R]" % page)
        for x in xs:
            d.xref_set_key(x, "Parent", "%d 0 R" % page)
            d.xref_set_key(x, "V", "null")
        d.xref_set_key(d.pdf_catalog(), "AcroForm/Fields", "[%d 0 R]" % form)
    d.save(path)
    d.close()


def on(value) -> bool:
    return value not in ("", "Off", None, False)


def states(path: str) -> dict:
    r = fitz.open(path)
    out = {w.field_name.split(".")[-1]: w.field_value for w in r[0].widgets()}
    r.close()
    return out


def main() -> int:
    app = QApplication(sys.argv)   # noqa: F841
    tmp = tempfile.mkdtemp(prefix="trv_cbh_")
    try:
        for hier in (False, True):
            label = "nested names (form1.Page1.cb1)" if hier else "flat names"
            src = os.path.join(tmp, f"cb_{int(hier)}.pdf")
            make(src, hier)
            doc = PdfDocument()
            doc.open(src)
            fs = doc.page_fields(0)
            first = fs[0]
            doc.set_field_value(0, first["xref"], True, name=first["name"])
            out = os.path.join(tmp, f"out_{int(hier)}.pdf")
            doc.save_as(out)
            st = states(out)
            check(f"{label}: only the clicked box is on after saving and reopening",
                  on(st.get("cb1")) and not on(st.get("cb2")) and not on(st.get("cb3")), str(st))
            doc.set_field_value(0, first["xref"], False, name=first["name"])
            out2 = os.path.join(tmp, f"out2_{int(hier)}.pdf")
            doc.save_as(out2)
            st = states(out2)
            check(f"{label}: clearing it turns it off again, the others stay off",
                  not any(on(v) for v in st.values()), str(st))
            doc.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
