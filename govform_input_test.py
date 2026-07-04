"""官公庁フォームの「文字入力」欄を、本物のキー入力で検証する統合テスト。

QtTest で実オーバーレイ（コム欄/1行/複数行/チェックボックス）に実際のキー入力・
クリックを流し、値が確定して PDF に書かれるか、maxlen が効くか、保存→再オープンで
残るかを確認する。選択系(radio/combo)は govform_interact_test.py が担当。

サンプル PDF は TRIV_GOVFORM_PDF か第1引数、無ければ ~/Downloads の 6010。
見つからなければスキップ（CI は PDF 非同梱）。
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QLineEdit, QPlainTextEdit

from viewer import theme
from viewer.document import PdfDocument
from viewer.page_view import PageView


def _find_pdf() -> str | None:
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        return sys.argv[1]
    env = os.environ.get("TRIV_GOVFORM_PDF", "")
    if env and os.path.exists(env):
        return env
    # リポジトリ同梱のフィクスチャ（CI でも使える）
    fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "tests", "fixtures", "6010_form_sample.pdf")
    if os.path.exists(fixture):
        return fixture
    guess = os.path.join(os.path.expanduser("~"), "Downloads",
                         "6010_健康保険傷病手当金支給申請書.pdf")
    return guess if os.path.exists(guess) else None


def main() -> int:
    pdf = _find_pdf()
    if not pdf:
        print("SKIP: govform サンプル PDF が無いのでスキップ")
        return 0

    app = QApplication(sys.argv[:1])
    theme.apply_theme(app, dark=False)
    doc = PdfDocument()
    doc.open(pdf)
    pv = PageView()
    pv.resize(900, 700)
    pv.set_document(doc)
    pv.set_zoom(1.4)
    pv.show()
    app.processEvents()
    pv.set_form_mode(True)
    app.processEvents()

    results: list[bool] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        results.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")

    def field(name: str, page: int):
        for w in doc._doc.load_page(page).widgets():
            if w.field_name == name:
                return w
        return None

    def value(name: str, page: int):
        w = field(name, page)
        return None if w is None else w.field_value

    def overlay_for(name: str, page: int, cls):
        w = field(name, page)
        if w is None:
            return None
        x0, y0, x1, y1 = doc.pdf_rect_to_label(page, w.rect, pv._zoom)
        cx, cy = int((x0 + x1) / 2), int((y0 + y1) / 2)
        for ov in pv._form_overlays:
            if isinstance(ov, cls) and ov.geometry().contains(cx, cy):
                return ov
        return None

    def type_into(le: QLineEdit, text: str, commit: bool = True):
        le.setFocus()
        le.clear()
        QTest.keyClicks(le, text)
        if commit:
            QTest.keyClick(le, Qt.Key.Key_Return)  # editingFinished -> commit
        app.processEvents()

    # --- コム欄（1文字1マス, maxlen 8）: FRM6010-01-001 -----------------------
    pv.set_page(0)
    app.processEvents()
    pv.ensureWidgetVisible(pv._labels[0], 0, 0)
    app.processEvents()
    comb = overlay_for("FRM6010-01-001", 0, QLineEdit)
    if comb is None:
        check("setup: comb overlay FRM6010-01-001", False)
    else:
        type_into(comb, "12345678")
        check("comb: typed value commits to PDF",
              str(value("FRM6010-01-001", 0)) == "12345678",
              f"(={value('FRM6010-01-001', 0)!r})")
        # maxlen=8: 余分は入らない
        type_into(comb, "1234567890ABC")
        check("comb: maxlen(8) clamps input", len(comb.text()) == 8,
              f"(len={len(comb.text())} text={comb.text()!r})")

    # --- 1行テキスト（editable, _PRINT でない display0）------------------------
    plain_name = None
    for w in doc._doc.load_page(0).widgets():
        n = w.field_name or ""
        f = field(n, 0)
        # 素のテキスト・印刷専用でない・コムでない
        import fitz
        if (w.field_type == fitz.PDF_WIDGET_TYPE_TEXT and int(w.field_display) == 0
                and not n.endswith("_PRINT")):
            flags = int(getattr(w, "field_flags", 0) or 0)
            if not (flags & (1 << 24)) and not (flags & (1 << 12)):  # not comb/multiline
                plain_name = n
                break
    if plain_name:
        le = overlay_for(plain_name, 0, QLineEdit)
        if le is not None:
            type_into(le, "Tokyo123")
            check("plain text: typed value commits",
                  str(value(plain_name, 0)) == "Tokyo123",
                  f"({plain_name}={value(plain_name,0)!r})")
        else:
            check("setup: plain-text overlay", False, f"({plain_name})")
    else:
        print("[INFO] editable plain-text field なし（このフォームでは skip）")

    # --- 複数行（_FormTextArea）: FRM6010-01-015 -----------------------------
    ml = overlay_for("FRM6010-01-015", 0, QPlainTextEdit)
    if ml is None:
        check("setup: multiline overlay FRM6010-01-015", False)
    else:
        ml.setFocus()
        ml.setPlainText("みずほ銀行")
        ml.clearFocus()          # focusOut -> committed -> _commit_field
        app.processEvents()
        check("multiline: value commits",
              str(value("FRM6010-01-015", 0)) == "みずほ銀行",
              f"(={value('FRM6010-01-015', 0)!r})")

    # --- チェックボックス: FRM6010-02-009_ (page 1) --------------------------
    pv.set_page(1)
    app.processEvents()
    pv.ensureWidgetVisible(pv._labels[1], 0, 0)
    app.processEvents()
    cb = overlay_for("FRM6010-02-009_", 1, QCheckBox)
    if cb is None:
        check("setup: checkbox overlay", False)
    else:
        before = str(value("FRM6010-02-009_", 1))
        QTest.mouseClick(cb, Qt.MouseButton.LeftButton)
        app.processEvents()
        after = str(value("FRM6010-02-009_", 1))
        check("checkbox: click toggles value", before != after,
              f"({before!r}->{after!r})")

    # --- 保存→再オープンで値が残るか（データ経路の往復）---------------------
    out = os.path.join(tempfile.gettempdir(), "_triv_govform_filled.pdf")
    try:
        doc.save_as(out)
        d2 = PdfDocument()
        d2.open(out)
        def v2(name, page):
            for w in d2._doc.load_page(page).widgets():
                if w.field_name == name:
                    return w.field_value
            return None
        check("round-trip: comb value persists",
              str(v2("FRM6010-01-001", 0)) == "12345678")
        check("round-trip: multiline value persists",
              str(v2("FRM6010-01-015", 0)) == "みずほ銀行")
        d2.close()

        # --- 独立ライブラリ(pikepdf)で読み直し、仕様通り書けているか確認 ----
        # PyMuPDF 自身で読めるだけでなく、別実装（＝他ビューアと同じ土俵）でも
        # AcroForm の値が正しく取り出せることを保証する。
        import pikepdf
        raw: dict = {}

        def walk(f, prefix=""):
            t = f.get("/T")
            fq = ((prefix + "." if prefix else "") + str(t)) if t is not None else prefix
            kids = f.get("/Kids")
            if kids and any(k.get("/T") is not None for k in kids):
                for k in kids:
                    walk(k, fq)
            else:
                raw[fq] = f.get("/V")

        with pikepdf.open(out) as pk:
            acro = pk.Root.get("/AcroForm")
            for f in (acro.get("/Fields") or []):
                walk(f)

            def as_str(v):
                try:
                    return str(v)
                except Exception:  # noqa: BLE001  非UTF8名（チェックボックス）等
                    return None

            check("pikepdf: comb value written per spec",
                  as_str(raw.get("FRM6010-01-001")) == "12345678")
            check("pikepdf: multiline value written per spec",
                  as_str(raw.get("FRM6010-01-015")) == "みずほ銀行")
            cb = raw.get("FRM6010-02-009_")
            check("pikepdf: checkbox is checked (/V != /Off)",
                  cb is not None and cb != pikepdf.Name("/Off"))
    except Exception as exc:  # noqa: BLE001
        check("round-trip save/reopen", False, f"({exc})")
    finally:
        if os.path.exists(out):
            os.remove(out)

    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
