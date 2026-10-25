"""注釈の取り消し・やり直し・色変更・移動が、関係のない注釈を壊さないことの確認。

以前の取り消しは「そのページの注釈を全部消して、控えた記述から作り直す」だった。
- 作り直せない種類（Acrobat などで付けたスタンプ・波線・多角形）は、別の注釈を 1 つ
  足して Ctrl+Z しただけで消えた
- 何行にもわたるハイライトは、外接矩形 1 枚になり、塗られていなかった語まで塗れた
- 作成者名が消え、付箋のアイコンが変わった
- スタンプを掴んで動かすと、動かずに注釈ごと消えた

いまは、控えと同じ注釈には触らず、違う分だけ戻す。
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


def summarize(doc: PdfDocument, page: int = 0) -> list:
    out = []
    for a in doc._doc[page].annots():
        out.append((a.type[1], tuple(round(x) for x in a.rect), a.info.get("title"),
                    len(a.vertices or []), a.info.get("name")))
    return sorted(out)


def annot_of(doc: PdfDocument, xref: int, page: int = 0):
    """(ページ, 注釈)。注釈はページに結び付くので、ページも持っておく。"""
    pg = doc._doc[page]
    return pg, next(a for a in pg.annots() if a.xref == xref)


def build(path: str) -> None:
    d = fitz.open()
    pg = d.new_page()
    for i, y in enumerate((100, 130, 160)):
        pg.insert_text((72, y), f"line {i + 1} of the text here", fontsize=14)
    quads = [fitz.Rect(72, y - 12, 200, y + 4).quad for y in (100, 130, 160)]
    h = pg.add_highlight_annot(quads)
    h.set_info(title="Alice", content="alice's note")
    h.update()
    pg.add_stamp_annot(fitz.Rect(300, 100, 400, 150), stamp=0)
    pg.add_squiggly_annot(fitz.Rect(72, 220, 200, 236))
    pg.add_polygon_annot([(300, 300), (380, 300), (340, 360)]).update()
    t = pg.add_text_annot((400, 400), "sticky", icon="Comment")
    t.set_info(title="Carol")
    t.update()
    d.new_page().insert_text((72, 100), "page two", fontsize=14)
    d.save(path)
    d.close()


def main() -> int:
    app = QApplication(sys.argv)   # noqa: F841
    tmp = tempfile.mkdtemp(prefix="trv_annotundo_")
    try:
        src = os.path.join(tmp, "annots.pdf")
        build(src)
        doc = PdfDocument()
        doc.open(src)
        before = summarize(doc)
        kinds = [k[0] for k in before]
        check("(前提) the file has annotations from another application",
              {"Highlight", "Stamp", "Squiggly", "Polygon", "Text"} <= set(kinds), str(kinds))

        # 1. 別の注釈を足して取り消す
        doc.add_rect(0, fitz.Rect(10, 10, 60, 60))
        check("adding an annotation keeps the others", len(summarize(doc)) == len(before) + 1)
        doc.undo()
        check("undoing it leaves every other annotation exactly as it was",
              summarize(doc) == before, str(summarize(doc)))
        doc.redo()
        check("redo brings the annotation back without touching the others",
              len(summarize(doc)) == len(before) + 1
              and all(k in summarize(doc) for k in before))
        doc.undo()

        # 2. 別のページの注釈操作の取り消しも、このページに影響しない
        doc.add_rect(1, fitz.Rect(10, 10, 60, 60))
        doc.undo()
        check("an edit on another page and its undo do not touch this page",
              summarize(doc) == before)

        # 3. 複数行のハイライトを色変更・取り消し・やり直し
        pg0 = doc._doc[0]
        hxref = [a.xref for a in pg0.annots() if a.type[1] == "Highlight"][0]
        _pg, _a = annot_of(doc, hxref)
        n_before = len(_a.vertices)
        new_x = doc.recolor_annot(0, hxref, (0.2, 0.8, 0.2))
        _pg, hl = annot_of(doc, new_x)
        check("recolouring a multi-line highlight keeps all its lines",
              len(hl.vertices) == n_before, f"({len(hl.vertices)} vs {n_before})")
        check("…and the author", hl.info.get("title") == "Alice", hl.info.get("title"))
        doc.undo()
        pg0 = doc._doc[0]
        h2 = [a for a in pg0.annots() if a.type[1] == "Highlight"]
        check("undoing the recolour restores the highlight with all its lines",
              len(h2) == 1 and len(h2[0].vertices) == n_before)
        pix = pg0.get_pixmap()
        check("…and the gaps between the lines stay unpainted",
              pix.pixel(100, 111) == (255, 255, 255), str(pix.pixel(100, 111)))
        doc.redo()
        pg0 = doc._doc[0]
        h3 = [a for a in pg0.annots() if a.type[1] == "Highlight"]
        check("redo keeps the multi-line highlight too",
              len(h3) == 1 and len(h3[0].vertices) == n_before)
        doc.undo()

        # 4. 作成者名と付箋のアイコンが残る
        check("authors survive (Alice / Carol)",
              {k[2] for k in summarize(doc) if k[0] in ("Highlight", "Text")}
              == {"Alice", "Carol"}, str(summarize(doc)))
        check("the sticky note keeps its icon", any(
            k[0] == "Text" and k[4] == "Comment" for k in summarize(doc)))

        # 5. 作り直せない注釈（スタンプ）を掴んで動かしても、消えない
        pg0 = doc._doc[0]
        sx = [a.xref for a in pg0.annots() if a.type[1] == "Stamp"][0]
        res = doc.move_annot(0, sx, 10, 10)
        check("dragging a stamp from another app does not delete it",
              res is None and "Stamp" in [k[0] for k in summarize(doc)])
        doc.modified = False
        doc.move_annot(0, sx, 10, 10)
        check("…and does not mark the document as changed", not doc.modified)

        # 6. 普通の編集（テキストを入れて動かす）は、これまでどおり取り消せて、
        #    もとの注釈は無傷
        tx = doc.add_freetext(0, fitz.Rect(72, 300, 250, 330), "私の入れた文字")
        pg0 = doc._doc[0]
        tx_xref = [a.xref for a in pg0.annots() if a.type[1] == "FreeText"][0]
        doc.move_annot(0, tx_xref, 20, 20)
        doc.undo()
        doc.undo()
        check("a normal text insert + move + undo x2 leaves the original annotations",
              summarize(doc) == before, str(summarize(doc)))
        doc.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
