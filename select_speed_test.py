"""文書全体の選択（Ctrl+A）とページをまたぐドラッグの速さ、結果が変わっていないことの確認。

以前は、(1) 文字の位置を読むときにページの画像の中身まで取り出していた（スキャン PDF は
1 ページ 50ms 超。300 ページで十数秒止まる）。(2) ページ全体を選ぶときも 1 文字ずつ読んでいた。
(3) 文字レイアウトを憶えるのは数ページだけなので、それより長いドラッグはマウスを動かすたびに
途中のページを全部読み直していた。いまは、画像を外し、ページ全体は行単位で読み、結果を憶える。
"""
from __future__ import annotations

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
from PySide6.QtWidgets import QApplication

from viewer.document import PdfDocument

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def make(path: str, pages: int, image: bool) -> None:
    d = fitz.open()
    pm = None
    if image:
        pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 1600, 2200), False)
        pm.set_rect(pm.irect, (240, 240, 240))
    for n in range(pages):
        pg = d.new_page(width=595, height=842)
        if pm is not None:
            pg.insert_image(pg.rect, pixmap=pm)
        for i in range(40):
            pg.insert_text((50, 60 + i * 18),
                           f"{n}-{i} The quick brown fox 日本語の文章がここに入ります。吾輩は猫である。",
                           fontsize=10, fontname="japan", render_mode=3 if image else 0)
    d.save(path)
    d.close()


def timed(fn):
    t = time.perf_counter()
    r = fn()
    return time.perf_counter() - t, r


def main() -> int:
    app = QApplication(sys.argv)   # noqa: F841
    tmp = tempfile.mkdtemp(prefix="trv_sel_")
    try:
        # --- 結果が 1 文字ずつ読んだときと同じ ------------------------------------------
        fixtures = [os.path.join(ROOT, "tests", "fixtures", f)
                    for f in ("6010_form_sample.pdf",) if os.path.exists(
                        os.path.join(ROOT, "tests", "fixtures", f))]
        plain = os.path.join(tmp, "plain.pdf")
        make(plain, 6, image=False)
        fixtures.append(plain)
        for path in fixtures:
            doc = PdfDocument()
            doc.open(path)
            same = True
            worst = 0.0
            for i in range(doc.page_count):
                fast_t, fast_r = doc._whole_page_by_lines(i)
                chars, _ = doc._page_layout(i)
                slow_t, slow_r = doc._selection_from(chars, 0, len(chars))
                if fast_t != slow_t or len(fast_r) != len(slow_r):
                    same = False
                    print("   page", i, len(fast_t), len(slow_t), len(fast_r), len(slow_r))
                    continue
                for a, b in zip(fast_r, slow_r):
                    worst = max(worst, abs(a.x0 - b.x0), abs(a.y0 - b.y0),
                                abs(a.x1 - b.x1), abs(a.y1 - b.y1))
            check(f"{os.path.basename(path)}: same text and lines as the per-character read",
                  same and worst < 0.6, f"(largest rect difference {worst:.2f} pt)")
            doc.close()

        # --- 画像つきのページでも遅くならない（スキャン + 文字レイヤ） -----------------
        n = 30
        text_only = os.path.join(tmp, "t.pdf")
        scan = os.path.join(tmp, "s.pdf")
        make(text_only, n, image=False)
        make(scan, n, image=True)
        times = {}
        for label, path in (("text", text_only), ("scan", scan)):
            doc = PdfDocument()
            doc.open(path)
            dt, (text, per_page) = timed(doc.select_all_text)
            times[label] = dt
            check(f"{label}: Ctrl+A selects every page", len(per_page) == n
                  and f"{n - 1}-39" in text, f"({dt:.2f}s for {n} pages)")
            doc.close()
        check("a scanned page is not much slower than a text page (images are not read)",
              times["scan"] < times["text"] * 2.5 + 0.2,
              f"(scan {times['scan']:.2f}s vs text {times['text']:.2f}s)")

        # --- 長いドラッグ: 2 回目からは憶えた結果を使う ---------------------------------
        doc = PdfDocument()
        doc.open(scan)
        doc.invalidate_text_layout()
        p0, p1 = fitz.Point(60, 100), fitz.Point(300, 700)
        t1, r1 = timed(lambda: doc.select_across(0, p0, n - 1, p1))
        t2, r2 = timed(lambda: doc.select_across(0, p0, n - 1, p1))
        check("dragging across 30 pages: the second move reuses the first (no re-reading)",
              t2 < t1 * 0.5 + 0.02 and r1 == r2, f"({t1:.2f}s then {t2:.3f}s)")

        # --- 編集したら古い結果を使わない -------------------------------------------------
        doc._doc[3].insert_text((50, 800), "追加した一行", fontsize=10, fontname="japan")
        doc.modified = True
        _t, (text, _pp) = timed(doc.select_all_text)
        check("after an edit, the selection includes the new text", "追加した一行" in text)
        doc.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
