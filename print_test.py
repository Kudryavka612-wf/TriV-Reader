"""印刷のテスト（PDF 出力プリンタに描かせて中身を確かめる）。

「Adobe より低解像度」「遅い」への対策の回帰テスト。既定はベクター（SVG 経由）で、
プリンタのネイティブ解像度で出る。画像化を選んだときだけラスタで描く。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import QApplication

from viewer.main_window import MainWindow

ROOT = os.path.dirname(os.path.abspath(__file__))
results: list[bool] = []
PAGES = 3


def check(cond: bool, label: str) -> bool:
    print(("OK  : " if cond else "NG  : ") + label)
    results.append(bool(cond))
    return bool(cond)


def build(path: str) -> None:
    doc = fitz.open()
    for n in range(PAGES):
        p = doc.new_page(width=595, height=842)
        p.insert_text((60, 90), f"Page {n + 1}", fontsize=18)
        for row in range(20):
            p.insert_text((60, 130 + row * 22),
                          "あいうえお漢字 ABCDEFG 0123456789", fontsize=9)
    doc.save(path)
    doc.close()


def make_printer(out_pdf: str, dpi: int = 600) -> QPrinter:
    pr = QPrinter(QPrinter.PrinterMode.HighResolution)
    pr.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    pr.setOutputFileName(out_pdf)
    pr.setResolution(dpi)
    return pr


def render(w: MainWindow, out: str, quality) -> float:
    opt = {"size_mode": 2, "custom_scale": 1.0, "orientation": "auto",
           "grayscale": False, "quality": quality}
    t0 = time.perf_counter()
    w._render_to_printer(make_printer(out), list(range(PAGES)), opt)
    return time.perf_counter() - t0


def content_of(path: str) -> tuple[int, int, int]:
    """出力 PDF の (ページ数, 図形の数, 埋め込み画像の数) を数える。"""
    with fitz.open(path) as doc:
        pages = doc.page_count
        drawings = sum(len(doc[i].get_drawings()) for i in range(pages))
        images = sum(len(doc[i].get_images()) for i in range(pages))
    return pages, drawings, images


def test_vector(w: MainWindow, root: str) -> None:
    out = os.path.join(root, "vector.pdf")
    dt = render(w, out, "vector")
    pages, drawings, images = content_of(out)
    check(pages == PAGES, f"全ページ印刷される（{pages}/{PAGES}）")
    check(drawings > 100, f"文字が図形として出る＝ベクター（{drawings} 個）")
    check(images == 0, f"ページを画像に焼いていない（画像 {images} 個）")
    print(f"      ベクター印刷 {dt:.2f} 秒 / {os.path.getsize(out)/1024/1024:.1f} MB")


def test_raster(w: MainWindow, root: str) -> None:
    out = os.path.join(root, "raster600.pdf")
    dt = render(w, out, 600)
    pages, drawings, images = content_of(out)
    check(pages == PAGES, f"画像化でも全ページ印刷される（{pages}/{PAGES}）")
    check(images == PAGES, f"1ページ1枚の画像になる（{images} 枚）")
    print(f"      画像化600dpi {dt:.2f} 秒 / {os.path.getsize(out)/1024/1024:.1f} MB")


def test_raster_dpi_scales(w: MainWindow, root: str) -> None:
    """指定した dpi がそのまま出力画像の細かさになる。"""
    sizes = {}
    for dpi in (300, 600):
        out = os.path.join(root, f"r{dpi}.pdf")
        render(w, out, dpi)
        with fitz.open(out) as doc:
            info = doc[0].get_images()[0]
            sizes[dpi] = info[2]  # 画像の横ピクセル数
    ratio = sizes[600] / max(1, sizes[300])
    check(1.8 < ratio < 2.2,
          f"600dpi は 300dpi の約2倍の細かさ（{sizes[300]}px → {sizes[600]}px）")


def test_vector_is_faster_than_hi_raster(w: MainWindow, root: str) -> None:
    """ベクターは高解像度ラスタより速い（品質は落とさずに）。"""
    v = min(render(w, os.path.join(root, "v2.pdf"), "vector") for _ in range(2))
    r = min(render(w, os.path.join(root, "r2.pdf"), 600) for _ in range(2))
    check(v < r, f"ベクターの方が速い（{v:.2f}秒 < {r:.2f}秒）")


def test_grayscale(w: MainWindow, root: str) -> None:
    """グレースケール指定なら 1画素1バイトで描く（データ量が 1/3 になる）。

    PDF 出力プリンタは受け取った画像を RGB に展開してしまうので、出力 PDF では
    なく描画そのものを確かめる。実プリンタへは 1 チャンネルのまま送られる。
    """
    from PySide6.QtGui import QImage

    gray = w.doc.render_page_image(0, zoom=2.0, gray=True)
    color = w.doc.render_page_image(0, zoom=2.0, gray=False)
    check(gray.format() == QImage.Format.Format_Grayscale8,
          f"グレースケールで描画される（{gray.format().name}）")
    # RGB888 は行頭が4バイト境界に揃うぶん、ちょうど3倍にはならない
    check(gray.sizeInBytes() < color.sizeInBytes() * 0.4,
          f"データ量が約1/3になる（{gray.sizeInBytes()//1024}KB / "
          f"{color.sizeInBytes()//1024}KB）")

    out = os.path.join(root, "gray.pdf")
    opt = {"size_mode": 2, "custom_scale": 1.0, "orientation": "auto",
           "grayscale": True, "quality": 300}
    w._render_to_printer(make_printer(out), [0], opt)
    check(content_of(out)[0] == 1, "グレースケール指定でも印刷できる")


def test_preview_is_vector(w: MainWindow) -> None:
    """印刷プレビューも本番と同じベクター描画で、中身が出ている。"""
    from viewer.print_dialog import PrintDialog

    dlg = PrintDialog(w.doc, 0, w.settings)
    pm = dlg._preview_pixmap(0, 0.5)
    check(not pm.isNull() and pm.width() > 100,
          f"プレビューが描ける（{pm.width()}x{pm.height()}）")
    img = pm.toImage()
    inked = sum(1 for y in range(0, img.height(), 5)
                for x in range(0, img.width(), 5)
                if img.pixelColor(x, y).value() < 250)
    check(inked > 20, f"白紙になっていない（描画された点 {inked}）")

    svg = w.doc.page_svg(0)
    check(svg.lstrip().startswith("<?xml") or "<svg" in svg[:200],
          "ページを SVG として取り出せる")


def main() -> int:
    app = QApplication(sys.argv)
    root = tempfile.mkdtemp(prefix="trv_print_")
    try:
        pdf = os.path.join(root, "sample.pdf")
        build(pdf)
        w = MainWindow()
        w.resize(1100, 800)
        w.show()
        app.processEvents()
        w.open_path(pdf)
        app.processEvents()

        test_vector(w, root)
        test_raster(w, root)
        test_raster_dpi_scales(w, root)
        test_vector_is_faster_than_hi_raster(w, root)
        test_grayscale(w, root)
        test_preview_is_vector(w)

        for i in range(w.tabs.count()):
            t = w.tabs.widget(i)
            if hasattr(t, "doc"):
                t.doc.close()
    finally:
        os.chdir(ROOT)
        shutil.rmtree(root, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    raise SystemExit(main())
