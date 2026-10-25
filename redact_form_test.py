"""墨消しを適用したら、黒い帯の下の入力欄の値・注釈の本文も残らないことの確認。

page.apply_redactions() はページの文字と絵だけを消し、入力欄（Widget）の値と、範囲に
全部は収まらない注釈の本文を残す。黒い帯の下に値が残り、コピーや文字の抽出で取り出せた
（マイナンバーなどを伏せたつもりで配る使い方で、いちばん困る）。
"""
from __future__ import annotations

import os
import re
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


def secrets_in_file(path: str) -> set[str]:
    """保存したファイルの全オブジェクト（展開後）に残る SECRET- の文字列。"""
    y = fitz.open(path)
    found: set[str] = set()
    for xr in range(1, y.xref_length()):
        try:
            o = y.xref_object(xr, compressed=False)
            st = y.xref_stream(xr) if y.xref_is_stream(xr) else b""
        except Exception:  # noqa: BLE001
            continue
        found.update(re.findall(r"SECRET-[A-Z0-9]+", o + st.decode("latin-1", "ignore")))
        for m in re.findall(r"<FEFF([0-9A-Fa-f]+)>", o):
            try:
                t = bytes.fromhex(m).decode("utf-16-be")
            except Exception:  # noqa: BLE001
                continue
            found.update(re.findall(r"SECRET-[A-Z0-9]+", t))
    y.close()
    return found


def main() -> int:
    app = QApplication(sys.argv)   # noqa: F841
    tmp = tempfile.mkdtemp(prefix="trv_redform_")
    try:
        src = os.path.join(tmp, "form.pdf")
        d = fitz.open()
        pg = d.new_page()
        pg.insert_text((72, 100), "SECRET-BODY", fontsize=14)

        def text_field(name, rect, value):
            w = fitz.Widget()
            w.field_name, w.field_type = name, fitz.PDF_WIDGET_TYPE_TEXT
            w.rect, w.field_value = fitz.Rect(rect), value
            w.text_font, w.text_fontsize = "Helv", 12
            pg.add_widget(w)

        text_field("under", (72, 150, 300, 174), "SECRET-FIELD1")
        text_field("edge", (280, 220, 420, 244), "SECRET-EDGE")   # 帯に端だけかかる
        text_field("outside", (72, 500, 300, 524), "KEEP-ME")
        cb = fitz.Widget()
        cb.field_name, cb.field_type = "cb", fitz.PDF_WIDGET_TYPE_CHECKBOX
        cb.rect, cb.field_value = fitz.Rect(72, 190, 90, 208), True
        pg.add_widget(cb)
        pg.add_text_annot((72, 250), "SECRET-NOTE")   # 範囲に全部は収まらない付箋
        pg.add_text_annot((72, 600), "KEEP-NOTE")
        d.save(src)
        d.close()

        doc = PdfDocument()
        doc.open(src)
        doc.add_redaction(0, fitz.Rect(60, 80, 300, 260))
        doc.apply_redactions()
        out = os.path.join(tmp, "out.pdf")
        doc.save_as(out)
        doc.close()

        y = fitz.open(out)
        text = y[0].get_text()
        vals = {w.field_name: w.field_value for w in y[0].widgets()}
        notes = [a.info.get("content") for a in y[0].annots()]
        y.close()
        check("the body text under the box is gone", "SECRET-BODY" not in text, repr(text))
        check("a form field under the box is emptied", vals.get("under") == "", str(vals))
        check("a field that only touches the box is emptied too", vals.get("edge") == "",
              str(vals))
        check("a checkbox under the box is unchecked", vals.get("cb") in ("Off", "", False),
              str(vals.get("cb")))
        check("the field values cannot be extracted from the saved text", "SECRET" not in text)
        check("a note that overlaps the box is removed", "SECRET-NOTE" not in notes,
              str(notes))
        check("nothing secret is left anywhere in the file's objects",
              not secrets_in_file(out), str(secrets_in_file(out)))
        check("a field outside the box keeps its value", vals.get("outside") == "KEEP-ME",
              str(vals))
        check("a note outside the box is kept", "KEEP-NOTE" in notes, str(notes))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
