"""高 DPI（Windows の表示拡大 125〜200%）でページがにじまないことの確認。

以前は、ページを論理 px の大きさ（devicePixelRatio を無視）で描いていた。表示拡大が 200% の
画面では、Qt がそれを 2 倍に引き伸ばして出すので、文字がにじむ（実測: 文字の部分の純黒の
画素が、ネイティブの解像度で描いたときの 1/13）。いまは、表示倍率ぶん細かく描いて pixmap に
devicePixelRatio を付ける。論理サイズ・当たり判定・注釈や入力欄の座標は変わらない。

QT_SCALE_FACTOR を変えた子プロセスで、実際のウィジェットの描画を測る。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def child(path: str) -> None:
    import fitz
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    import viewer.page_view as PV
    from viewer import theme
    from viewer.document import PdfDocument
    from viewer.page_view import PageView

    app = QApplication(sys.argv[:1])
    theme.apply_theme(app, dark=False)
    doc = PdfDocument()
    doc.open(path)
    pv = PageView()
    pv.resize(1000, 900)
    pv.set_document(doc)
    pv.show()
    for _ in range(10):
        app.processEvents()
    pv.set_zoom(1.0)
    QTest.qWait(250)
    app.processEvents()
    lab = pv._labels[0]
    pm = lab.pixmap()
    img = lab.grab().toImage()
    dark = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.red() < 60 and c.green() < 60 and c.blue() < 60:
                dark += 1
    out = {"dpr": pv.devicePixelRatioF(), "pm_dpr": pm.devicePixelRatio(),
           "pm_device": [pm.width(), pm.height()],
           "label": [lab.width(), lab.height()],
           "logical": [pm.deviceIndependentSize().width(), pm.deviceIndependentSize().height()],
           "dark": dark}

    # 当たり判定・移動は論理 px のまま: 四角を選んで動かし、動かしている最中の描画も出す
    doc.add_rect(0, fitz.Rect(300, 500, 400, 560), color=(1, 0, 0), width=3, fill=(1, 0, 0))
    pv.refresh_page(0)
    QTest.qWait(150)
    pv.set_tool(PV.TOOL_NONE)

    def mouse(et, pt, buttons=None):
        btn = Qt.MouseButton.LeftButton
        if buttons is None:
            buttons = btn if et != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
        app.sendEvent(lab, QMouseEvent(et, QPointF(pt), QPointF(lab.mapToGlobal(pt)), btn,
                                       buttons, Qt.KeyboardModifier.NoModifier))

    c = QPoint(350, 530)
    mouse(QEvent.Type.MouseButtonPress, c)
    mouse(QEvent.Type.MouseButtonRelease, c)
    out["selected"] = pv._sel_xref is not None
    before = fitz.Rect(doc.annot_rect(0, pv._sel_xref)) if pv._sel_xref else None
    mouse(QEvent.Type.MouseButtonPress, c)
    for i in range(1, 6):
        p = c + QPoint(20 * i, 0)
        app.sendEvent(lab, QMouseEvent(QEvent.Type.MouseMove, QPointF(p),
                                       QPointF(lab.mapToGlobal(p)), Qt.MouseButton.NoButton,
                                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    ghost = lab.grab().toImage()          # 動かしている最中の描画（落ちないこと）
    out["ghost_ok"] = not ghost.isNull()
    mouse(QEvent.Type.MouseButtonRelease, c + QPoint(100, 0))
    app.processEvents()
    after = fitz.Rect(doc.annot_rect(0, pv._sel_xref)) if pv._sel_xref else None
    out["moved"] = (round(after.x0 - before.x0, 1) if before and after else None)
    print("RESULT " + json.dumps(out), flush=True)
    os._exit(0)


if len(sys.argv) > 2 and sys.argv[1] == "--child":
    child(sys.argv[2])


results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def run(scale: str, pdf: str) -> dict:
    env = dict(os.environ, QT_SCALE_FACTOR=scale, QT_QPA_PLATFORM="offscreen",
               TRIVREADER_SETTINGS_INI="throwaway", PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, os.path.abspath(__file__), "--child", pdf],
                          capture_output=True, text=True, encoding="utf-8", env=env,
                          timeout=180, cwd=ROOT)
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    raise RuntimeError(proc.stdout[-300:] + proc.stderr[-600:])


def main() -> int:
    import fitz
    from PySide6.QtWidgets import QApplication

    from viewer.document import PdfDocument

    tmp = tempfile.mkdtemp(prefix="trv_hidpi_")
    pdf = os.path.join(tmp, "t.pdf")
    d = fitz.open()
    pg = d.new_page(width=595, height=842)
    for i in range(14):
        pg.insert_text((60, 80 + i * 30), f"The quick brown fox 日本語の文章 {i}", fontsize=16,
                       fontname="japan")
    d.save(pdf)
    d.close()

    r1, r15, r2 = run("1", pdf), run("1.5", pdf), run("2", pdf)
    check("scale 1: nothing changes (dpr 1, pixmap = label size)",
          r1["dpr"] == 1.0 and r1["pm_dpr"] == 1.0 and r1["pm_device"] == r1["label"],
          str(r1))
    for name, r, f in (("150%", r15, 1.5), ("200%", r2, 2.0)):
        check(f"{name}: the page is drawn finer, with the display's pixel ratio attached",
              abs(r["dpr"] - f) < 0.01 and abs(r["pm_dpr"] - f) < 0.01
              and abs(r["pm_device"][0] - r["label"][0] * f) <= 2, str(r))
        check(f"{name}: the logical size (layout, hit-testing, annotation coordinates) is unchanged",
              r["label"] == r1["label"] and abs(r["logical"][0] - r1["label"][0]) <= 1.01,
              f"({r['label']} vs {r1['label']})")
        check(f"{name}: the text is sharp (many more fully-black pixels than a stretched image)",
              r["dark"] >= r1["dark"] * f * f * 0.7, f"({r['dark']} vs {r1['dark']} at 100%)")
        check(f"{name}: selecting and moving an annotation works the same",
              r["selected"] and r["ghost_ok"] and r["moved"] is not None
              and abs(r["moved"] - 100) < 1.5, f"(moved {r['moved']})")

    # 描画の大きさの上限（巨大ページ × 高 DPI で数百 MB にならない）
    app = QApplication(sys.argv[:1])   # noqa: F841
    big = os.path.join(tmp, "big.pdf")
    d = fitz.open()
    d.new_page(width=4000, height=4000)
    d.save(big)
    d.close()
    doc = PdfDocument()
    doc.open(big)
    pm = doc.render_page(0, zoom=1.0, dpr=2.0)
    dev = pm.width() * pm.height()
    check("a huge page at 200% lowers its pixel ratio instead of using hundreds of MB",
          dev <= doc.MAX_RENDER_PIXELS * 1.05 and 1.0 <= pm.devicePixelRatio() < 2.0,
          f"({pm.width()}x{pm.height()} = {dev / 1e6:.0f}M px, dpr {pm.devicePixelRatio()})")
    a = doc.render_page(0, zoom=0.25, dpr=1.0)
    b = doc.render_page(0, zoom=0.25, dpr=2.0)
    check("the render cache keeps the two pixel ratios apart",
          a.devicePixelRatio() == 1.0 and b.devicePixelRatio() == 2.0 and a is not b)
    doc.close()

    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
