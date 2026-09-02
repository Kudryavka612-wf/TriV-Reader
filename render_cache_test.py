"""ページ描画キャッシュと先読みの検証。

速さそのものではなく「同じ絵を描き直さない」「中身が変わったら必ず捨てる」
という、間違えると古い絵が出る側の性質を見る。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(__file__))
from viewer.document import PdfDocument
from viewer.page_view import PageView

TMP = tempfile.gettempdir()


def build(path: str, pages: int = 8) -> None:
    doc = fitz.open()
    for n in range(pages):
        p = doc.new_page(width=595, height=842)
        p.insert_text((72, 120), f"PAGE {n + 1}", fontsize=28)
        for i in range(40):  # 少し重くして描画時間を作る
            p.draw_circle(fitz.Point(100 + i * 9, 400 + (i % 7) * 30), 26)
    doc.save(path)
    doc.close()


def main() -> int:
    app = QApplication(sys.argv)
    src = os.path.join(TMP, "_cache_src.pdf")
    build(src)

    doc = PdfDocument()
    doc.open(src)

    # --- 同じ要求は描き直さない ---------------------------------------
    first = doc.render_page(0, zoom=1.5)
    again = doc.render_page(0, zoom=1.5)
    assert again is first, "同じページ・同じ倍率なのに描き直している"
    other = doc.render_page(0, zoom=2.0)
    assert other is not first, "倍率が違うのに使い回している"
    print("OK: 同一ページ/倍率はキャッシュから返す")

    t0 = time.perf_counter()
    for _ in range(30):
        doc.render_page(0, zoom=1.5)
    cached = time.perf_counter() - t0
    assert cached < 0.05, f"キャッシュが効いていない({cached:.3f}s)"
    print(f"OK: 30 回取り出して {cached * 1000:.1f} ms")

    # --- 中身が変われば捨てる（古い絵を出さない）----------------------
    doc.rotate_page(0, 90)          # modified が立つ経路
    after = doc.render_page(0, zoom=1.5)
    assert after is not first, "回転したのに前の絵を返している"
    assert after.width() != first.width(), (after.width(), first.width())
    print("OK: 回転などの変更でキャッシュを捨てる")

    doc.add_highlight(1, [fitz.Rect(72, 100, 300, 130)])
    base = doc.render_page(2, zoom=1.5)
    doc.set_metadata({"title": "x"})   # これも modified 経路
    assert doc.render_page(2, zoom=1.5) is not base
    print("OK: 注釈やメタデータの変更でも捨てる")

    # --- 別ファイルを開いたら引き継がない ------------------------------
    other_pdf = os.path.join(TMP, "_cache_src2.pdf")
    build(other_pdf, pages=3)
    keep = doc.render_page(0, zoom=1.0)
    doc.open(other_pdf)
    assert doc.render_page(0, zoom=1.0) is not keep, "前のファイルの絵が残っている"
    print("OK: 開き直しでキャッシュを捨てる")

    # --- 上限を超えたら古いものから捨てる ------------------------------
    doc.open(src)
    doc.RENDER_CACHE_BYTES = 4 * 1024 * 1024  # わざと小さくする
    doc._invalidate_view_caches()
    kept = doc.render_page(0, zoom=1.0)
    for i in range(1, doc.page_count):
        doc.render_page(i, zoom=1.0)
    assert doc._render_bytes <= doc.RENDER_CACHE_BYTES, doc._render_bytes
    assert doc.render_page(0, zoom=1.0) is not kept, "上限を超えても捨てていない"
    print(f"OK: 上限 {doc.RENDER_CACHE_BYTES // 1024}KB を超えたら古い順に破棄"
          f"（保持 {doc._render_bytes // 1024}KB）")

    # --- 先読み（スクロールが止まったら前後を描いておく）--------------
    doc2 = PdfDocument(); doc2.open(src)
    view = PageView()
    view.resize(700, 800)
    view.show()
    view.set_document(doc2)
    app.processEvents()
    view.set_page(3)
    app.processEvents()
    doc2._invalidate_view_caches()
    view._prefetch_around()
    cached_pages = {k[0] for k in doc2._render_cache}
    assert cached_pages, "先読みが何も描いていない"
    assert min(cached_pages) < view.index or max(cached_pages) > view.index, cached_pages
    print(f"OK: 表示中 {view.index} の前後 {sorted(cached_pages)} を先読み")

    view.close()
    doc.close(); doc2.close()
    for f in (src, other_pdf):
        if os.path.exists(f):
            os.remove(f)
    print("ALL OK: 描画キャッシュ / 先読み")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
