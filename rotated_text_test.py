"""回転したページに入れたテキスト注釈・スタンプが、画面で見える向きで確定することの確認。
あわせて、背景色付きのテキスト注釈が、動かす・書式を変えるたびに塗りを失わないことも。

横向きのスキャン（/Rotate 90 など）でテキストツールを使うと、入力中は正立の箱なのに、
確定すると 90/270 で横倒し、180 で上下逆になった（注釈を回転前の座標系で作り、ページの
回転を渡していなかった）。FreeText の作り直し（移動・書式変更・取り消し）は、背景の塗りを
渡さず、他のアプリで付けた背景色が消えた。
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
# ページを時計回りに rot 度回すと、回転前の座標での文章の向きは、これになる
EXPECT_DIR = {0: (1.0, 0.0), 90: (0.0, -1.0), 180: (-1.0, 0.0), 270: (0.0, 1.0)}


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def text_dirs(doc: PdfDocument) -> list:
    """注釈を焼き込んだ複製から、文章の向きを読む。"""
    work = fitz.open("pdf", doc._doc.tobytes())
    work.bake(annots=True, widgets=False)
    out = [tuple(round(v, 1) for v in line["dir"])
           for b in work[0].get_text("dict")["blocks"] if b.get("type") == 0
           for line in b["lines"]]
    work.close()
    return out


def main() -> int:
    app = QApplication(sys.argv)   # noqa: F841
    for rot in (0, 90, 180, 270):
        doc = PdfDocument()
        d = fitz.open()
        d.new_page(width=500, height=700)
        doc._doc = d
        doc.path = None
        doc._doc[0].set_rotation(rot)
        page = doc._doc[0]
        disp = fitz.Rect(100, 100, 300, 140)                 # 画面で見える位置
        rect = (disp * page.derotation_matrix).normalize()    # 回転前の座標
        doc.add_freetext(0, rect, "Hello World", fontsize=14)
        dirs = text_dirs(doc)
        check(f"rotation {rot}: the text reads the right way up on the page as displayed",
              dirs and all(d_ == EXPECT_DIR[rot] for d_ in dirs), str(dirs[:2]))
        pg = doc._doc[0]   # 注釈はページを弱参照するので、ページを持っておく
        ann = next(a for a in pg.annots() if a.type[1] == "FreeText")
        shown = (fitz.Rect(ann.rect) * pg.rotation_matrix).normalize()
        check(f"rotation {rot}: it sits where it was put (displayed box)",
              abs(shown.x0 - 100) < 3 and abs(shown.y0 - 100) < 3,
              f"({tuple(round(v) for v in shown)})")
        # 動かして、取り消して、やり直しても向きと位置が保たれる
        doc.move_annot(0, ann.xref, 10, 10)
        doc.undo()
        dirs = text_dirs(doc)
        check(f"rotation {rot}: still upright after moving it and undoing",
              dirs and all(d_ == EXPECT_DIR[rot] for d_ in dirs), str(dirs[:2]))
        # スタンプ
        doc.add_stamp(0, fitz.Point(*(fitz.Point(120, 300) * doc._doc[0].derotation_matrix)),
                      "済", fontsize=20)
        dirs = text_dirs(doc)
        check(f"rotation {rot}: a stamp is upright too",
              len(dirs) >= 2 and all(d_ == EXPECT_DIR[rot] for d_ in dirs), str(dirs[:4]))
        doc._doc.close()

    # 背景色付きのテキスト注釈
    doc = PdfDocument()
    d = fitz.open()
    pg = d.new_page(width=500, height=700)
    ft = pg.add_freetext_annot(fitz.Rect(72, 100, 300, 140), "yellow note", fontsize=14,
                               fill_color=(1, 1, 0.4))
    ft.update(fill_color=(1, 1, 0.4))
    doc._doc = d

    def yellow() -> int:
        pix = doc._doc[0].get_pixmap(dpi=72)
        return sum(1 for y in range(100, 140) for x in range(72, 300)
                   if pix.pixel(x, y)[2] < 160 and pix.pixel(x, y)[0] > 200)

    n0 = yellow()
    pg0 = doc._doc[0]
    xref = [a.xref for a in pg0.annots()][0]
    new_x = doc.move_annot(0, xref, 0, 80)
    pix = doc._doc[0].get_pixmap(dpi=72)
    moved = sum(1 for y in range(180, 220) for x in range(72, 300)
                if pix.pixel(x, y)[2] < 160 and pix.pixel(x, y)[0] > 200)
    check("a background-coloured text note keeps its colour when moved",
          n0 > 3000 and moved > n0 * 0.8, f"({n0} -> {moved} yellow pixels)")
    doc.restyle_annot(0, new_x, fontsize=18)
    pix = doc._doc[0].get_pixmap(dpi=72)
    n2 = sum(1 for y in range(170, 240) for x in range(72, 300)
             if pix.pixel(x, y)[2] < 160 and pix.pixel(x, y)[0] > 200)
    check("…and when its font size is changed", n2 > n0 * 0.8, f"({n2} yellow pixels)")
    doc._doc.close()
    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
