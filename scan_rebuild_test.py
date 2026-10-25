"""スキャンを作り直す操作（傾き補正・OCR）が、ページの大きさ・しおり・文書情報を保つことの確認。

- 傾き補正は、画像だけのページを作り直していたので、解像度の指定が無く 96dpi 扱いになり、
  A4 が約 2.1 倍の大きさになった。しおり・題名・作成者も消え、傾きが無いページまで画像に
  作り直されて容量が増えた
- OCR も、しおり・文書情報が消えた。ページ番号などを足したスキャンページは「文字あり」と
  されて OCR されなかった
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz
from PIL import Image
from PySide6.QtWidgets import QApplication

from viewer import deskew as deskew_mod
from viewer import ocr
from viewer.document import PdfDocument

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def scan_image(skew: float, words: str = "The quick brown fox jumps over") -> bytes:
    d = fitz.open()
    p = d.new_page(width=595, height=842)
    for n in range(14):
        p.insert_text((72, 80 + n * 40), words, fontsize=16)
    pix = p.get_pixmap(dpi=150)
    d.close()
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if skew:
        img = img.rotate(skew, resample=Image.Resampling.BICUBIC, fillcolor=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="jpeg", quality=80)
    return buf.getvalue()


def gray_of(page) -> Image.Image:
    pix = page.get_pixmap(dpi=150, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")


def main() -> int:
    app = QApplication(sys.argv)   # noqa: F841
    tmp = tempfile.mkdtemp(prefix="trv_scan_")
    try:
        src = os.path.join(tmp, "src.pdf")
        d = fitz.open()
        d.new_page(width=595, height=842).insert_text((72, 100), "born digital", fontsize=20)
        d.new_page(width=595, height=842).insert_image(fitz.Rect(0, 0, 595, 842),
                                                       stream=scan_image(-5.0))
        p3 = d.new_page(width=595, height=842)
        p3.insert_image(fitz.Rect(0, 0, 595, 842), stream=scan_image(0.0))
        p3.insert_text((280, 820), "- 3 -", fontsize=10)    # あとから足したページ番号
        d.set_toc([[1, "第1章", 1], [1, "第2章", 2], [1, "第3章", 3]])
        d.set_metadata({"title": "題名OK", "author": "作者OK"})
        d.save(src)
        d.close()
        size_before = os.path.getsize(src)

        doc = PdfDocument()
        doc.open(src)
        n = doc.deskew_all(dpi=150)
        check("only the tilted scan is corrected", n == 1, f"({n} pages)")
        sizes = [tuple(round(v) for v in (pg.rect.width, pg.rect.height)) for pg in doc._doc]
        check("every page keeps its size (A4 stays A4)", sizes == [(595, 842)] * 3, str(sizes))
        check("the outline and the title/author survive",
              [t[1] for t in doc._doc.get_toc()] == ["第1章", "第2章", "第3章"]
              and doc._doc.metadata.get("title") == "題名OK"
              and doc._doc.metadata.get("author") == "作者OK", str(doc._doc.metadata.get("title")))
        out = os.path.join(tmp, "out.pdf")
        doc.save_as(out)
        doc.close()
        after = deskew_mod.estimate_skew(gray_of(fitz.open(out)[1]))
        check("the tilted page is now straight", abs(after) < 1.0, f"({after:.1f} deg)")
        size_after = os.path.getsize(out)
        check("the file does not balloon (untouched pages are kept as they were)",
              size_after < size_before * 2.0, f"({size_before} -> {size_after} bytes)")
        check("the text on the born-digital page is still text",
              "born digital" in fitz.open(out)[0].get_text())

        # --- OCR ---------------------------------------------------------------
        if os.path.exists(os.path.join(ocr.INSTALL_TESSDATA, "eng.traineddata")):
            td = os.path.join(tmp, "tess")
            os.makedirs(td)
            shutil.copy(os.path.join(ocr.INSTALL_TESSDATA, "eng.traineddata"), td)
            doc = PdfDocument()
            doc.open(src)
            ocr_out = os.path.join(tmp, "ocr.pdf")
            doc.ocr_to(ocr_out, "eng", td, dpi=200)
            doc.close()
            r = fitz.open(ocr_out)
            check("OCR keeps the outline and the title/author",
                  [t[1] for t in r.get_toc()] == ["第1章", "第2章", "第3章"]
                  and r.metadata.get("title") == "題名OK", str(r.metadata.get("title")))
            check("a scan with a page number added is still recognised (3rd page)",
                  "quick" in r[2].get_text().lower(), repr(r[2].get_text()[:60]))
            check("a born-digital page is left as it was (not rasterised)",
                  "born digital" in r[0].get_text() and not r[0].get_images())
            r.close()
        else:
            print("SKIP: 手元の Tesseract が無いので、OCR の確認を省略")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
