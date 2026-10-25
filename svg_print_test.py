"""印刷のベクター描画（viewer/svg_print.py）の回帰テスト。

Windows の印刷（GDI）は、QSvgRenderer の出力や半透明を画像にしてしまう。
そのため 1 ページごとに 600dpi の大きな画像をプリンターへ送っていて、Acrobat
より明らかに重かった。自前の描画で、文字も線もベクターのままプリンターへ送る。

- 見た目が MuPDF の描画とほぼ同じ（官公庁フォーム・図形と注釈・90°回転）
- 描けないもの（半透明・乗算の重ね方）は False を返す（画像で刷る）
- 実際の Windows の印刷経路（Microsoft Print to PDF）でベクターのまま届き、
  画像化より小さく速い（このプリンターがある PC のときだけ）
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 利用者本人の設定（レジストリ）を書き換えないよう、使い捨ての INI で動かす
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")

import fitz
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtPrintSupport import QPrinter, QPrinterInfo
from PySide6.QtWidgets import QApplication

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from viewer import svg_print  # noqa: E402

FORM = os.path.join(ROOT, "tests", "fixtures", "6010_form_sample.pdf")
results: list[bool] = []


def check(name: str, ok, extra: str = "") -> bool:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    return bool(ok)


def off_share(page, dpi: int = 100):
    """svg_print で描いた絵と MuPDF の絵で、大きく違う画素の割合（描けなければ None）。"""
    z = dpi / 72
    pm = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
    ref = QImage(pm.samples, pm.width, pm.height, pm.stride,
                 QImage.Format.Format_RGB888).copy()
    img = QImage(pm.width, pm.height, QImage.Format.Format_RGB888)
    img.fill(QColor("white"))
    p = QPainter(img)
    ok = svg_print.draw_svg(p, page.get_svg_image(text_as_path=True),
                            QRectF(0, 0, pm.width, pm.height))
    p.end()
    if not ok:
        return None
    a = ref.constBits().tobytes()[::7]
    b = img.constBits().tobytes()[::7]
    return sum(1 for x, y in zip(a, b) if abs(x - y) > 96) / len(a)


def mixed_pdf(path: str) -> None:
    from viewer.document import PdfDocument
    d = fitz.open()
    pg = d.new_page(width=595, height=842)
    pg.insert_text((60, 80), "Annotations ハンコと書き込み", fontsize=18, fontname="japan")
    pg.draw_bezier((60, 200), (200, 100), (300, 300), (500, 200), color=(0.8, 0, 0), width=3)
    pg.draw_line((60, 260), (540, 260), color=(0, 0, 0.8), width=2, dashes="[6 3] 0")
    pg.draw_circle((150, 380), 60, color=(0, 0.5, 0), fill=(0.8, 1, 0.8), width=2)
    pg.draw_rect(fitz.Rect(300, 330, 520, 430), color=(0, 0, 0), fill=(0.9, 0.9, 0.2), width=1)
    img = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 80))
    img.set_rect(img.irect, (40, 90, 200))
    pg.insert_image(fitz.Rect(60, 470, 240, 590), pixmap=img)
    d.save(path)
    doc = PdfDocument()
    doc.open(path)
    doc.add_freetext(0, fitz.Rect(300, 470, 540, 520), "記入した文字 ABC", fontsize=14)
    doc.add_stamp(0, fitz.Point(300, 560), "承認")
    doc.add_line(0, fitz.Point(60, 650), fitz.Point(500, 700), color=(1, 0, 0), width=3,
                 arrow=True)
    doc.add_ink(0, [[(60, 720), (120, 760), (180, 720), (240, 770)]], color=(0, 0, 1), width=2)
    doc.add_rect(0, fitz.Rect(300, 620, 540, 700), color=(0, 0.6, 0), width=2, dashes=[4, 2])
    doc.save_as(path + ".annots.pdf")
    doc.close()


def test_fidelity(root: str) -> None:
    with fitz.open(FORM) as d:
        shares = [off_share(d[i]) for i in range(d.page_count)]
    check("government form pages look the same as MuPDF draws them",
          all(s is not None and s < 0.02 for s in shares),
          "(" + ", ".join("-" if s is None else f"{s * 100:.2f}%" for s in shares) + ")")
    src = os.path.join(root, "mixed.pdf")
    mixed_pdf(src)
    with fitz.open(src + ".annots.pdf") as d:
        s = off_share(d[0])
        check("shapes, dashes, curves, images and annotations look the same",
              s is not None and s < 0.01, f"({s and s * 100:.2f}%)")
        d[0].set_rotation(90)
        s = off_share(d[0])
        check("…and on a rotated page", s is not None and s < 0.01, f"({s and s * 100:.2f}%)")


def test_unsupported(root: str) -> None:
    d = fitz.open()
    pg = d.new_page()
    pg.insert_text((72, 100), "under a highlight", fontsize=20)
    pg.draw_rect(fitz.Rect(60, 200, 300, 300), color=None, fill=(1, 0, 0), fill_opacity=0.4)
    check("half-transparent content is not drawn (printed as an image instead)",
          off_share(pg) is None)
    d2 = fitz.open()
    p2 = d2.new_page()
    p2.insert_text((72, 100), "highlighted text", fontsize=20)
    p2.add_highlight_annot(fitz.Rect(70, 80, 260, 106))
    from viewer.mw_print import svg_is_safe
    svg = p2.get_svg_image(text_as_path=True)
    check("highlights (multiply blending) are left to the image path",
          not svg_is_safe(svg) or off_share(p2) is None)


def test_real_printer(root: str) -> None:
    info = QPrinterInfo.printerInfo("Microsoft Print to PDF")
    if info.isNull():
        print("[SKIP] Microsoft Print to PDF is not installed on this PC")
        return
    from PySide6.QtWidgets import QMessageBox

    from viewer.main_window import MainWindow
    w = MainWindow()
    w.open_path(FORM)
    QApplication.processEvents()
    # 印刷できない環境（スプーラーが止まっている CI 等）で警告を出して止まらない
    failures: list = []
    QMessageBox.warning = staticmethod(lambda *a, **k: failures.append(a[2:3]))

    def print_to(quality) -> tuple:
        # 拡張子を .pdf 以外にすると Qt は自前の PDF 出力に切り替えず、本物の
        # Windows の印刷（GDI → ドライバー）を通る
        out = os.path.join(root, f"print_{quality}.bin")
        pr = QPrinter(info, QPrinter.PrinterMode.HighResolution)
        pr.setOutputFileName(out)
        opt = {"copies": 1, "collate": True, "duplex": "none", "grayscale": False,
               "paper": None, "orientation": "auto", "size_mode": 1, "custom_scale": 1.0,
               "quality": quality, "nup": 1, "content": "all"}
        w._configure_printer(pr, opt)
        t0 = time.perf_counter()
        w._render_to_printer(pr, [0], opt)
        dt = time.perf_counter() - t0
        if failures or not os.path.exists(out):
            return None
        with fitz.open(out) as d:
            pg = d[0]
            area = pg.rect.width * pg.rect.height
            big = [r for r in (pg.get_image_rects(x[0]) for x in pg.get_images()) for r in r
                   if r.width * r.height > area * 0.5]
            return len(pg.get_drawings()), len(big), os.path.getsize(out), dt

    vec = print_to("vector")
    ras = print_to(600)
    if vec is None or ras is None:
        print(f"[SKIP] printing does not work on this PC {failures}")
        return
    check("printing through Windows keeps the page as vectors (no page-sized image)",
          vec[0] > 500 and vec[1] == 0, f"(paths={vec[0]}, page images={vec[1]})")
    check("…and sends much less than printing it as a 600dpi image",
          vec[2] * 3 < ras[2], f"({vec[2] // 1024}KB vs {ras[2] // 1024}KB)")
    check("…and is not slower", vec[3] < max(2.0, ras[3]),
          f"({vec[3]:.2f}s vs {ras[3]:.2f}s)")
    w.doc.modified = False
    w._force_quit = True
    w.close()


def main() -> int:
    app = QApplication(sys.argv[:1])  # noqa: F841
    root = tempfile.mkdtemp(prefix="trv_svgprint_")
    try:
        test_fidelity(root)
        test_unsupported(root)
        test_real_printer(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====", flush=True)
    # os._exit で抜けると、プリンターのドライバーを読み込んだまま終わるため
    # ときどき終了時に落ちる（アプリの通常の終了では起きない）。普通に終える
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
