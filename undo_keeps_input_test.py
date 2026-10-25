"""ページ操作を取り消しても、そのあとに入れた入力値・しおり・文書情報が消えないことの確認。

ページの回転・削除などの履歴は、文書全体のバイト列。入力欄の値・しおり・文書情報の変更は
履歴に入らないので、そのまま戻すと、ページを回したあとに入力した欄の値やしおりが、確認なしに
消えた（さらに何か編集すると、やり直しも消えた）。値を変える操作（フォームのクリア・墨消し）は、
取り消せば値が戻るのが正しいので、対象外にしてある。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import fitz
from PySide6.QtWidgets import QApplication

from viewer.document import PdfDocument

FORM = os.path.join(ROOT, "tests", "fixtures", "6010_form_sample.pdf")
results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def text_fields(doc: PdfDocument, page: int = 0) -> list:
    return [f for f in doc.page_fields(page)
            if f["kind"] == "text" and not f["readonly"] and not f["comb"] and not f["multiline"]]


def value(doc: PdfDocument, name: str) -> str:
    for page in doc._doc:
        for w in page.widgets():
            if w.field_name == name:
                return str(w.field_value or "")
    return "?"


def main() -> int:
    app = QApplication(sys.argv)   # noqa: F841

    # --- 回転のあとに入れた欄の値・しおり・題名が、回転の取り消しで残る -------------------
    doc = PdfDocument()
    doc.open(FORM)
    a, b = text_fields(doc)[:2]
    doc.set_field_value(0, a["xref"], "AAA山田", name=a["name"])
    doc.rotate_page(1, 90)
    doc.set_field_value(0, b["xref"], "BBB花子", name=b["name"])    # 回転のあとに入力
    doc.add_bookmark("回転後のしおり", 1)
    doc.set_metadata({"title": "回転後の題名"})
    r = doc.undo()
    check("the rotation is undone", r and r[2] == "ページの回転" and doc.rotation(1) == 0, str(r))
    check("a value typed before the rotation is kept", value(doc, a["name"]) == "AAA山田")
    check("a value typed AFTER the rotation is kept too (it used to vanish)",
          value(doc, b["name"]) == "BBB花子", value(doc, b["name"]))
    check("a bookmark added after the rotation is kept",
          [t[1] for t in doc._doc.get_toc()] == ["回転後のしおり"], str(doc._doc.get_toc()))
    check("a title set after the rotation is kept",
          doc._doc.metadata.get("title") == "回転後の題名")
    check("redo is still possible, and brings the rotation back with every value",
          doc.can_redo())
    doc.redo()
    check("…redo restores the rotation and keeps the values",
          doc.rotation(1) == 90 and value(doc, a["name"]) == "AAA山田"
          and value(doc, b["name"]) == "BBB花子")
    doc.close()

    # --- ページ削除の取り消し ------------------------------------------------------------
    doc = PdfDocument()
    doc.open(FORM)
    n = doc.page_count
    a = text_fields(doc)[0]
    doc.delete_page(n - 1)
    doc.set_field_value(0, a["xref"], "削除後の入力", name=a["name"])
    doc.undo()
    check("undoing a page delete brings the page back", doc.page_count == n)
    check("…and keeps what was typed after the delete",
          value(doc, a["name"]) == "削除後の入力")
    doc.close()

    # --- ページの数が変わる操作のあとにしおりを足すと、しおりは戻らず、知らせる -------------
    doc = PdfDocument()
    doc.open(FORM)
    doc.delete_page(doc.page_count - 1)
    doc.add_bookmark("削除後に足したしおり", 1)
    doc.undo()
    check("a bookmark added after a page-count change cannot be mapped back, and the user is told",
          "しおり" in doc.undo_notice and doc._doc.get_toc() == [], doc.undo_notice)
    doc.close()

    # --- 値を変える操作は、取り消せば値が戻る（対象外） --------------------------------------
    doc = PdfDocument()
    doc.open(FORM)
    a = text_fields(doc)[0]
    doc.set_field_value(0, a["xref"], "消される値", name=a["name"])
    doc.clear_form()
    check("(前提) clearing the form empties the field", value(doc, a["name"]) == "")
    doc.undo()
    check("undoing 'clear form' brings the typed value back (not re-cleared)",
          value(doc, a["name"]) == "消される値", value(doc, a["name"]))
    doc.close()

    d = fitz.open()
    pg = d.new_page(width=400, height=300)
    w = fitz.Widget()
    w.field_name, w.field_type = "secret", fitz.PDF_WIDGET_TYPE_TEXT
    w.rect, w.field_value = fitz.Rect(50, 50, 250, 80), "SECRET-VALUE"
    w.text_font, w.text_fontsize = "Helv", 12
    pg.add_widget(w)
    doc = PdfDocument()
    doc._doc = d
    doc.add_redaction(0, fitz.Rect(40, 40, 260, 90))
    doc.apply_redactions()
    check("(前提) the redaction blanked the field", value(doc, "secret") == "")
    doc.undo()
    check("undoing the redaction brings the value back", value(doc, "secret") == "SECRET-VALUE",
          value(doc, "secret"))
    doc._doc.close()

    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
