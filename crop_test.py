"""トリミングの確認（2 回目の切り取り位置・ページ端を越えたドラッグ・回転したページの自動トリミング）。

- すでにトリミングしたページをもう一度切ると、ドラッグした所と違う（前回の切り取りの原点
  ぶんずれた）範囲が切り取られた
- ドラッグがページの端を 1px でも越えると例外になり、何も切れず、取り消しだけが積まれた
- /Rotate 90・270 のページを自動トリミングすると、下側の文字が切れた
"""
from __future__ import annotations

import os
import sys

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


def red_bbox(page):
    pix = page.get_pixmap(dpi=72)
    xs, ys = [], []
    for y in range(pix.height):
        for x in range(pix.width):
            r, g, b = pix.pixel(x, y)[:3]
            if r > 200 and g < 60 and b < 60:
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def make(rot: int = 0) -> PdfDocument:
    d = fitz.open()
    pg = d.new_page(width=400, height=600)
    pg.draw_rect(fitz.Rect(100, 200, 110, 210), color=(1, 0, 0), fill=(1, 0, 0))
    pg.set_rotation(rot)
    doc = PdfDocument()
    doc._doc = d
    return doc


def main() -> int:
    app = QApplication(sys.argv)   # noqa: F841
    # 2 回目のトリミングは、今のページの座標（左上が原点）で指定した所を切る
    for rot in (0, 90):
        doc = make(rot)
        doc.set_crop(0, fitz.Rect(50, 50, 350, 450))
        pg = doc._doc[0]
        first = red_bbox(pg)
        doc.set_crop(0, fitz.Rect(20, 20, 200, 300))
        pg = doc._doc[0]
        second = red_bbox(pg)
        cb = pg.cropbox
        check(f"rotation {rot}: the second crop cuts exactly the dragged area",
              (cb.x0, cb.y0, cb.x1, cb.y1) == (70, 70, 250, 350), f"(cropbox {tuple(cb)})")
        if rot == 0:
            check("rotation 0: the marker moved by the crop offset (20, 20)",
                  second == (first[0] - 20, first[1] - 20, first[2] - 20, first[3] - 20),
                  f"({first} -> {second})")
        doc.undo()
        check(f"rotation {rot}: undo returns to the first crop",
              tuple(doc._doc[0].cropbox) == (50, 50, 350, 450))
        doc._doc.close()

    # ページの端を越えたドラッグは、端で切り詰める（例外にしない）
    doc = make(0)
    try:
        doc.set_crop(0, fitz.Rect(50, 50, 560, 760))
        ok, msg = True, ""
    except Exception as exc:  # noqa: BLE001
        ok, msg = False, repr(exc)
    cb = doc._doc[0].cropbox
    check("dragging past the page edge crops to the edge instead of failing",
          ok and (cb.x1, cb.y1) == (400, 600), f"{msg} (cropbox {tuple(cb)})")
    try:
        doc.set_crop(0, fitz.Rect(900, 900, 950, 950))
        raised = False
    except ValueError:
        raised = True
    undo_before = doc.can_undo()
    check("a range entirely outside the page is refused with a message", raised)
    check("…and leaves no empty entry in the undo history", doc.can_undo() == undo_before)
    doc._doc.close()

    # 回転したページの自動トリミング: 下側の文字が切れない
    for rot in (0, 90, 270):
        d = fitz.open()
        pg = d.new_page(width=400, height=600)
        pg.insert_text((50, 80), "TOP", fontsize=20)
        pg.insert_text((50, 520), "BOTTOM", fontsize=20)
        pg.set_rotation(rot)
        doc = PdfDocument()
        doc._doc = d
        rect = doc.auto_crop_rect(0)
        words = [fitz.Rect(w[:4]) for w in doc._doc[0].get_text("words")]
        covered = all(rect.contains(w) for w in words)
        check(f"rotation {rot}: auto-crop keeps all the text", covered,
              f"(crop {tuple(round(v) for v in rect)})")
        doc._doc.close()
    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
