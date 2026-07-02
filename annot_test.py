"""注釈（標準PDFアノテーション）の付与・削除・永続化を検証する。"""
from __future__ import annotations

import os
import sys
import tempfile

import fitz

sys.path.insert(0, os.path.dirname(__file__))
from viewer.document import PdfDocument

TMP = tempfile.gettempdir()


def annot_names(path: str, index: int = 0) -> list[str]:
    doc = fitz.open(path)
    names = [a.type[1] for a in doc.load_page(index).annots()]
    doc.close()
    return names


def main() -> int:
    src = os.path.join(TMP, "_annot_src.pdf")
    d = fitz.open()
    d.new_page(width=595, height=842)
    d.save(src)
    d.close()

    doc = PdfDocument()
    doc.open(src)
    R = fitz.Rect

    doc.add_highlight(0, R(72, 72, 300, 100))
    doc.add_rect(0, R(72, 120, 300, 200))
    doc.add_ink(0, [[fitz.Point(80, 250), fitz.Point(120, 280), fitz.Point(160, 250)]])
    doc.add_freetext(0, R(72, 320, 320, 380), "コメントです")
    doc.add_text_note(0, fitz.Point(400, 100), "付箋メモ")
    assert doc.annot_count(0) == 5, doc.annot_count(0)

    out = os.path.join(TMP, "_annot_out.pdf")
    doc.save_as(out)
    doc.close()

    # 別ライブラリ視点（再オープン）で標準注釈として残るか
    names = annot_names(out)
    print("annots after save:", names)
    for expect in ("Highlight", "Square", "Ink", "FreeText", "Text"):
        assert expect in names, (expect, names)
    print("OK: 5種の注釈が標準PDFアノテーションとして永続化")

    # 座標変換（回転0なら zoom 換算のみ）
    doc2 = PdfDocument()
    doc2.open(out)
    p = doc2.label_to_pdf_point(0, 200, 400, zoom=2.0)
    assert abs(p.x - 100) < 0.01 and abs(p.y - 200) < 0.01, (p.x, p.y)
    print(f"OK: 座標変換 label(200,400)@2x -> pdf({p.x:.0f},{p.y:.0f})")

    # 削除：ハイライト矩形内の点を消す
    before = doc2.annot_count(0)
    deleted = doc2.delete_annot_at(0, fitz.Point(100, 85))
    assert deleted and doc2.annot_count(0) == before - 1
    print(f"OK: 注釈削除 {before}→{doc2.annot_count(0)}")

    # 全消去
    n = doc2.clear_annots(0)
    assert doc2.annot_count(0) == 0
    print(f"OK: 全消去 {n}件")
    doc2.close()

    # --- 移動・編集 ------------------------------------------------
    src2 = os.path.join(TMP, "_annot_edit.pdf")
    e = fitz.open(); e.new_page(width=595, height=842); e.save(src2); e.close()
    doc3 = PdfDocument()
    doc3.open(src2)
    doc3.add_rect(0, R(72, 100, 200, 160), color=(0.1, 0.1, 0.9), width=2)
    doc3.add_freetext(0, R(72, 200, 300, 250), "編集前テキスト")

    # 移動：四角を右に50,下に30
    xref = doc3.annot_at(0, fitz.Point(100, 130))
    assert xref is not None
    r0 = doc3.annot_rect(0, xref)
    new_xref = doc3.move_annot(0, xref, 50, 30)
    r1 = doc3.annot_rect(0, new_xref)
    # 枠線分で±1px程度の膨張があるため許容を持たせる
    assert abs(r1.x0 - (r0.x0 + 50)) <= 2 and abs(r1.y0 - (r0.y0 + 30)) <= 2, (r0, r1)
    print(f"OK: 移動 ({r0.x0:.0f},{r0.y0:.0f})→({r1.x0:.0f},{r1.y0:.0f})")

    # 色変更
    rc_xref = doc3.recolor_annot(0, new_xref, (1.0, 0.0, 0.0))
    assert rc_xref is not None
    print("OK: 色変更")

    # テキスト編集
    ft_xref = doc3.annot_at(0, fitz.Point(80, 220))
    assert doc3.annot_is_textual(0, ft_xref)
    assert "編集前" in doc3.annot_text(0, ft_xref)
    ed_xref = doc3.set_annot_text(0, ft_xref, "編集後テキスト")
    assert "編集後" in doc3.annot_text(0, ed_xref)
    print("OK: テキスト編集 ->", repr(doc3.annot_text(0, ed_xref)))

    # 保存して別ライブラリ視点で残るか
    out2 = os.path.join(TMP, "_annot_edit_out.pdf")
    doc3.save_as(out2)
    doc3.close()
    assert len(annot_names(out2)) == 2
    print("OK: 編集後も標準注釈として保存")
    os.remove(src2); os.remove(out2)

    # --- テキスト選択ハイライト & リサイズ -------------------------
    src3 = os.path.join(TMP, "_annot_txt.pdf")
    t = fitz.open(); tp = t.new_page(width=595, height=842)
    tp.insert_text((72, 100), "The quick brown fox jumps", fontsize=18)
    t.save(src3); t.close()

    doc4 = PdfDocument()
    doc4.open(src3)
    ok = doc4.add_text_highlight(0, fitz.Point(110, 95), fitz.Point(210, 95))
    assert ok is True, "テキストが拾えていない"
    assert doc4.annot_count(0) == 1
    out3 = os.path.join(TMP, "_annot_txt_out.pdf")
    doc4.save_as(out3)
    assert "Highlight" in annot_names(out3)
    print("OK: テキスト選択ハイライト（語のquadに沿う）")

    # テキストが無いページでは False（矩形ハイライトへフォールバック可能）
    blank = fitz.open(); blank.new_page(width=200, height=200)
    bpath = os.path.join(TMP, "_annot_blank.pdf"); blank.save(bpath); blank.close()
    docb = PdfDocument(); docb.open(bpath)
    assert docb.add_text_highlight(0, fitz.Point(10, 10), fitz.Point(50, 50)) is False
    docb.close(); os.remove(bpath)
    print("OK: テキスト無しページは False（フォールバック可能）")

    # リサイズ：四角を新しい矩形へ
    doc4.add_rect(0, R(72, 300, 172, 360), width=2)
    sq = doc4.annot_at(0, fitz.Point(120, 330))
    new = doc4.resize_annot(0, sq, R(72, 300, 300, 500))
    rr = doc4.annot_rect(0, new)
    assert abs(rr.x1 - 300) < 3 and abs(rr.y1 - 500) < 3, rr
    print(f"OK: リサイズ → x1={rr.x1:.0f}, y1={rr.y1:.0f}")
    doc4.close()
    os.remove(src3); os.remove(out3)

    for f in (src, out):
        if os.path.exists(f):
            os.remove(f)

    # --- 線種・矢印の線端・塗り・不透明度 ---------------------------
    src4 = os.path.join(TMP, "_annot_style_src.pdf")
    s = fitz.open(); s.new_page(width=595, height=842); s.save(src4); s.close()
    doc5 = PdfDocument()
    doc5.open(src4)

    doc5.add_line(0, fitz.Point(50, 100), fitz.Point(250, 100), width=3,
                  start="diamond", end="open_arrow", dashes=[6, 3], opacity=0.6)
    doc5.add_circle(0, R(50, 150, 150, 250), fill=(0.0, 1.0, 0.0),
                    dashes=[2, 2], opacity=0.8)
    doc5.add_rect(0, R(200, 150, 300, 250), fill=(0.0, 0.0, 1.0))

    page0 = doc5._doc.load_page(0)
    xref_line = next(a.xref for a in page0.annots() if a.type[1] == "Line")
    xref_circle = next(a.xref for a in page0.annots() if a.type[1] == "Circle")

    # 移動・リサイズ・色変更で Line/Circle が消えない（線端/塗り/線種を保持）こと
    nx_line = doc5.move_annot(0, xref_line, 5, 5)
    assert nx_line is not None, "直線が move で消えた"
    page1 = doc5._doc.load_page(0)
    line_annot = doc5._find_on(page1, nx_line)
    assert line_annot.line_ends == (3, 4), line_annot.line_ends  # diamond, open_arrow
    assert line_annot.border.get("dashes") == (6, 3), line_annot.border
    print("OK: 直線の移動後も線端(始点/終点)・線種を保持")

    nx_circle = doc5.resize_annot(0, xref_circle, R(50, 150, 250, 400))
    assert nx_circle is not None, "円が resize で消えた"
    page2 = doc5._doc.load_page(0)
    circle_annot = doc5._find_on(page2, nx_circle)
    assert circle_annot.colors.get("fill"), "円のリサイズで塗りが消えた"
    print("OK: 円のリサイズ後も塗りつぶしを保持")

    nx_line2 = doc5.recolor_annot(0, nx_line, (0.0, 0.0, 0.0))
    page3 = doc5._doc.load_page(0)
    line_annot2 = doc5._find_on(page3, nx_line2)
    assert line_annot2.line_ends == (3, 4), "直線の色変更で線端が消えた"
    print("OK: 直線の色変更後も線端を保持")

    out4 = os.path.join(TMP, "_annot_style_out.pdf")
    doc5.save_as(out4)
    doc5.close()
    kinds = annot_names(out4)
    assert kinds.count("Line") == 1 and kinds.count("Circle") == 1 and "Square" in kinds
    print("OK: 線種/矢印/塗り/不透明度つき注釈が保存後も正しい種類で残る ->", kinds)

    for f in (src4, out4):
        if os.path.exists(f):
            os.remove(f)

    print("ALL OK: 注釈")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
