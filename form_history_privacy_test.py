"""フォームの入力候補の個人情報の扱い（事務所の決定）の確認。

(a) 候補は同じ名前の欄で入れたものだけ（名前は正規化して比べる）。ほかの欄の値は出さない。
(b) 空白・ハイフンを除き全角数字を半角にして 7 桁以上の数字が続く値、番号・ID・口座・
    パスワード・暗証番号・コードなどを思わせる欄（欄の名前・/TU・JS の項目名）の値は覚えない。
    以前の版で覚えたそうした値は、読み込んだときにファイルからも消す。
(c) 利用者が確定したとき（Enter・Tab・クリックで抜けた）だけ覚える。保存・自動保存・
    拡大縮小・プログラムからのフォーカスの移動で書き込んだ打ちかけの値は覚えない。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import fitz
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget, QVBoxLayout

from viewer import form_history
from viewer.document import PdfDocument
from viewer.page_view import PageView

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def test_rules(tmp) -> None:
    h = form_history.FormHistory(os.path.join(tmp, "h.json"))
    check("(a) a value is suggested in a field of the same name",
          h.add("山田太郎", "shimei") and h.suggestions("shimei") == ["山田太郎"])
    check("(a) …also when the name differs only in width/case/spaces",
          h.suggestions("ＳＨＩＭＥＩ ") == ["山田太郎"], str(h.suggestions("ＳＨＩＭＥＩ ")))
    check("(a) it is never suggested in a different field",
          h.suggestions("jigyosho") == [], str(h.suggestions("jigyosho")))
    for value, key, names, why in (
            ("089-123-4567", "renraku", (), "a phone number (10 digits)"),
            ("７９０－０００１", "yubin", (), "a full-width postal code (7 digits)"),
            ("1234 567", "memo", (), "7 digits split by a space"),
            ("山田花子名義", "kouza_meigi", (), "an account (kouza) field"),
            ("abcd", "tel_no", (), "a 'no' field"),
            ("abcd", "TelNo", (), "a 'No' field (camel case)"),
            ("abcd", "pin_code", (), "a PIN field"),
            ("abcd", "UserID", (), "an ID field"),
            ("abcd", "Text1", ("基礎年金番号",), "a field whose tooltip (/TU) says 基礎年金番号"),
            ("山田花子", "FRM-01-008", ("被保険者氏名",), "a field whose JS name says 被保険者"),
            ("abcd", "f1", ("暗証番号",), "a 暗証 field"),
            ("abcd", "f2", ("ナンバー",), "a ナンバー field"),
            ("abcd", "f3", ("事業所コード",), "a コード field"),
            ("abcd", "pass1", (), "a password field")):
        check(f"(b) not remembered: {why}", not h.add(value, key, *names))
    check("(b) short numbers in an ordinary field are still remembered",
          h.add("123456", "memo") and h.add("2026/11/19", "hizuke"))
    check("(b) a sensitive-named field gets no suggestions even with history",
          h.suggestions("shimei", 0, "口座名義") == [])

    # 以前の版で覚えた値（7 桁の数字・ほかの欄の値）を読み込むと、覚えてはいけない分は消える
    path = os.path.join(tmp, "old.json")
    now = int(time.time())
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "entries": [["1234567", "備考", now],
                                             ["山田", "氏名", now],
                                             ["9876", "口座番号", now]]}, f, ensure_ascii=False)
    old = form_history.FormHistory(path)
    values = [e[0] for e in old._load()]
    with open(path, encoding="utf-8") as f:
        on_disk = f.read()
    check("(b) old history: long numbers and account values are dropped, also from the file",
          values == ["山田"] and "1234567" not in on_disk and "9876" not in on_disk,
          f"{values} {on_disk!r}")


def make(path: str) -> None:
    d = fitz.open()
    pg = d.new_page(width=400, height=300)
    for i, nm in enumerate(("shimei", "jusho")):
        w = fitz.Widget()
        w.field_name, w.field_type = nm, fitz.PDF_WIDGET_TYPE_TEXT
        w.rect, w.field_value = fitz.Rect(50, 50 + 50 * i, 300, 75 + 50 * i), ""
        w.text_font, w.text_fontsize = "Helv", 10
        pg.add_widget(w)
    d.save(path)
    d.close()


def test_commit_only(app, tmp) -> None:
    form_history.set_enabled(True)
    h = form_history.history()
    h.clear()
    src = os.path.join(tmp, "f.pdf")
    make(src)
    doc = PdfDocument()
    doc.open(src)
    host = QWidget()
    lay = QVBoxLayout(host)
    pv = PageView()
    lay.addWidget(pv)
    other = QLineEdit()   # 欄の外（クリックで移る先）
    lay.addWidget(other)
    host.resize(700, 700)
    pv.set_document(doc)
    pv.set_form_mode(True)
    host.show()
    host.activateWindow()
    for _ in range(10):
        app.processEvents()
    QTest.qWait(200)

    def ov(name):
        return next(c for c in pv._form_overlays
                    if isinstance(c, QLineEdit) and c.property("trv_field") == name)

    def values():
        return [e[0] for e in h._load()]

    ed = ov("shimei")
    ed.setFocus()
    app.processEvents()
    QTest.keyClicks(ed, "yamada")
    pv.commit_focused_field()   # 保存・自動保存の前の書き込み
    app.processEvents()
    check("(c) writing the field for saving/autosave does not remember a half-typed value",
          "yamada" not in values(), str(values()))
    check("(c) …the value is still written to the document",
          next(w.field_value for w in doc._doc[0].widgets() if w.field_name == "shimei")
          == "yamada")
    pv.set_zoom(1.5)            # 拡大縮小で作り直す
    QTest.qWait(300)
    app.processEvents()
    check("(c) zooming (rebuilding the fields) does not remember it either",
          "yamada" not in values(), str(values()))
    ed = ov("shimei")
    ed.setFocus()
    app.processEvents()
    QTest.keyClicks(ed, "taro")
    ed.clearFocus()             # プログラムからフォーカスを外した
    app.processEvents()
    check("(c) a programmatic focus change does not remember it",
          not any(v.endswith("taro") for v in values()), str(values()))
    ed = ov("shimei")
    ed.setFocus()
    app.processEvents()
    QTest.keyClick(ed, Qt.Key.Key_Return)
    app.processEvents()
    check("(c) pressing Enter (the user's commit) remembers it",
          values()[:1] == [ed.text()], str(values()))
    j = ov("jusho")
    j.setFocus()
    app.processEvents()
    QTest.keyClicks(j, "matsuyama")
    QTest.mouseClick(other, Qt.MouseButton.LeftButton)   # 欄の外をクリック
    other.setFocus(Qt.FocusReason.MouseFocusReason)
    app.processEvents()
    check("(c) leaving the field by clicking elsewhere remembers it",
          "matsuyama" in values(), str(values()))
    check("(a) the name is offered only in its own field",
          h.suggestions(ov("jusho").property("trv_hkey")) == ["matsuyama"]
          and "matsuyama" not in h.suggestions(ov("shimei").property("trv_hkey")),
          str(h.suggestions(ov("shimei").property("trv_hkey"))))
    host.close()
    doc.close()
    h.clear()


def main() -> int:
    app = QApplication(sys.argv)
    tmp = tempfile.mkdtemp(prefix="trv_fhist_")
    try:
        for t in (lambda: test_rules(tmp), lambda: test_commit_only(app, tmp)):
            try:
                t()
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                check("test ran without an error", False, repr(exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
