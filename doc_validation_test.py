"""失敗する操作が、文書を中途半端に変えず、無意味な取り消しも残さないことの確認。

- パスワード付きの PDF を結合・挿入すると、空のページが 1 枚増えたあとで英語の例外になった
- 3 枚の画像の 3 枚目が壊れていると、先の 2 枚だけ入って、画面に反映されなかった
- ヘッダー/フッターに「{会社名}」のような未知の項目があると、1 ページ目だけ文字が入って
  「未変更」のまま残った
- 「1-99999999」のようなページ指定が、巨大なリストを作ってから範囲外と分かった（キー 1 つ
  ごとに数秒固まる）
- ページが 1 枚も無い PDF は、開けたように見えて、以後の操作が全部失敗した
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz
from PySide6.QtWidgets import QApplication

from viewer.document import PdfDocument
from viewer.util import parse_ranges

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def main() -> int:
    app = QApplication(sys.argv)   # noqa: F841
    tmp = tempfile.mkdtemp(prefix="trv_valid_")
    try:
        src = os.path.join(tmp, "src.pdf")
        d = fitz.open()
        for i in range(3):
            d.new_page().insert_text((72, 100), f"p{i + 1}", fontsize=20)
        d.save(src)
        d.close()
        enc = os.path.join(tmp, "enc.pdf")
        d = fitz.open(src)
        d.save(enc, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="o")
        d.close()

        def fresh() -> PdfDocument:
            doc = PdfDocument()
            doc.open(src)
            return doc

        # 暗号化 PDF の挿入
        doc = fresh()
        try:
            doc.insert_pdf(enc)
            err = ""
        except ValueError as exc:
            err = str(exc)
        check("inserting a password-protected PDF is refused with a clear message",
              "パスワード" in err, err)
        check("…without adding a page or an undo entry or marking the document changed",
              doc.page_count == 3 and not doc.can_undo() and not doc.modified)

        # 壊れた画像
        bad = os.path.join(tmp, "bad.png")
        with open(bad, "wb") as f:
            f.write(b"not an image")
        good = os.path.join(tmp, "good.png")
        fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False).save(good)
        try:
            doc.insert_images_as_pages([good, good, bad])
            err = ""
        except ValueError as exc:
            err = str(exc)
        check("a broken image among several is refused, naming the file",
              "bad.png" in err, err)
        check("…and nothing was added", doc.page_count == 3 and not doc.can_undo())

        # すべて削除
        try:
            doc.delete_pages([0, 1, 2])
        except ValueError:
            pass
        check("deleting every page is refused without leaving an undo entry",
              doc.page_count == 3 and not doc.can_undo())

        # ヘッダー/フッター・ページ番号の書式
        before = [doc._doc[i].get_text() for i in range(3)]
        for kwargs in ({"left": "{会社名}"}, {"center": "社外秘 {"}, {"right": "{page} / {x}"}):
            try:
                doc.add_header_footer(**kwargs)
                err = ""
            except ValueError as exc:
                err = str(exc)
            after = [doc._doc[i].get_text() for i in range(3)]
            check(f"header/footer {kwargs}: refused, and no page was changed",
                  bool(err) and after == before and not doc.can_undo() and not doc.modified,
                  err[:60])
        try:
            doc.add_page_numbers("bottom-center", "{n} / {total} {oops}")
            err = ""
        except ValueError as exc:
            err = str(exc)
        check("a bad page-number format is refused before changing anything",
              bool(err) and not doc.can_undo() and not doc.modified, err[:60])
        doc.add_header_footer(left="{filename} {page}/{total}")
        check("a valid header/footer still works", "src.pdf" in doc._doc[0].get_text())
        doc.close()

        # ページ範囲
        t0 = time.monotonic()
        try:
            parse_ranges("1-99999999", 5)
            err = ""
        except ValueError as exc:
            err = str(exc)
        check("a huge page range is refused at once (no giant list)",
              "範囲外" in err and time.monotonic() - t0 < 0.5,
              f"({time.monotonic() - t0:.3f}s)")
        check("normal ranges still parse",
              parse_ranges("１－３、5", 5) == [0, 1, 2, 4] and parse_ranges("4-", 5) == [3, 4]
              and parse_ranges("3-1", 5) == [2, 1, 0])

        # ページの無い PDF
        zero = os.path.join(tmp, "zero.pdf")
        with open(zero, "wb") as f:
            f.write(b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
                    b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
                    b"trailer<</Root 1 0 R/Size 3>>\n%%EOF\n")
        doc = PdfDocument()
        try:
            doc.open(zero)
            err = ""
        except RuntimeError as exc:
            err = str(exc)
        check("a PDF with no pages is reported as such when opened (not half-opened)",
              "ページが 1 枚もありません" in err and not doc.is_open, err)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
