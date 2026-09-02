"""外観（テーマ）まわりの検証。

「ダークにしたのにここだけ白い」を防ぐのが主目的。個々のウィジェットに
スタイルを直接書くとテーマ切替で取り残されるので、切替後に実際の色が
変わっているかを見る。
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(__file__))
from viewer import theme
from viewer.main_window import MainWindow
from viewer.welcome import WelcomeWidget

TMP = tempfile.gettempdir()


def build(path: str, pages: int = 3) -> None:
    doc = fitz.open()
    for n in range(pages):
        doc.new_page(width=595, height=842).insert_text(
            (72, 120), f"PAGE {n + 1}", fontsize=24)
    doc.save(path)
    doc.close()


def main() -> int:  # noqa: C901
    app = QApplication(sys.argv)

    # --- スタイルシートに主要な部品が入っているか ----------------------
    for dark in (False, True):
        theme.apply_theme(app, dark)
        qss = app.styleSheet()
        for token in ("#ribbon", "#docTabs", "#welcomeCard", "QScrollBar",
                      "QPushButton#primary", "QMenu"):
            assert token in qss, f"{token} のスタイルが無い (dark={dark})"
        assert theme.accent_color(dark) in qss
        win = app.palette().color(QPalette.ColorRole.Window).name()
        assert win.lower() == theme._tokens(dark)["bg"].lower(), (win, dark)
    assert app.font().families()[0] == theme.UI_FONT_FAMILIES[0]
    print("OK: テーマのスタイルシートとパレット（ライト/ダーク）")

    # ライト/ダークで色が実際に違う（同じものを 2 回作っていない）
    theme.apply_theme(app, False)
    light_qss = app.styleSheet()
    theme.apply_theme(app, True)
    assert light_qss != app.styleSheet()
    assert theme.canvas_color(True) != theme.canvas_color(False)
    assert theme.paper_color(True) != theme.paper_color(False)
    print("OK: ライトとダークで配色が別物")

    # --- ウィンドウ全体がテーマに追従するか ----------------------------
    theme.apply_theme(app, False)
    src = os.path.join(TMP, "_theme_src.pdf")
    build(src)
    w = MainWindow()
    w.resize(1100, 800)
    w.show()
    app.processEvents()
    w.open_path(src)
    app.processEvents()

    assert w.ribbon.objectName() == "ribbon"
    assert w.tabs.objectName() == "docTabs"
    pv = w.page_view
    assert theme.canvas_color(False) in pv.viewport().styleSheet()
    assert theme.paper_color(False) in pv._labels[0].styleSheet()
    light_bg = pv._container._bg.name()

    w.act_dark.setChecked(True)   # → _toggle_dark
    app.processEvents()
    assert theme.canvas_color(True) in pv.viewport().styleSheet(), "余白がダークにならない"
    assert theme.paper_color(True) in pv._labels[0].styleSheet(), "紙の色が残っている"
    assert pv._container._bg.name() != light_bg, "土台の色が切り替わらない"
    assert theme.accent_color(True) in app.styleSheet()
    print("OK: テーマ切替でリボン・余白・紙・落ち影がすべて追従")

    # 落ち影を含む描画が例外なく走る（ページが見えている状態で grab）
    assert not pv._container.grab().isNull()
    w.act_dark.setChecked(False)
    app.processEvents()
    assert not pv._container.grab().isNull()
    print("OK: ページの落ち影を描いても例外が出ない")

    # --- サムネイルの空表示 --------------------------------------------
    w.thumbnails.clear()
    assert not w.thumbnails.grab().isNull()
    print("OK: サムネイルが空でも描画できる（案内文を出す）")

    # --- ウェルカム画面の「最近使ったファイル」 ------------------------
    wc = WelcomeWidget()
    missing = os.path.join(TMP, "_theme_missing.pdf")
    if os.path.exists(missing):
        os.remove(missing)
    wc.set_recent([src, missing, src])
    assert len(wc._tiles) == 2, "存在しないファイルまで並べている"
    got = []
    wc.file_requested.connect(got.append)
    wc._tiles[0].click()
    assert got == [src], got
    wc.set_recent([])
    assert wc._tiles == [] and not wc._recent_head.isVisible()
    wc.apply_theme(True)  # 例外なく当て直せる
    print("OK: 最近使ったファイルのカード（存在チェック・クリック・空表示）")

    # メインウィンドウ側でも履歴がカードに反映される
    assert any(src in t.toolTip() for t in w.welcome._tiles), \
        "開いたファイルがウェルカムのカードに出ていない"
    print("OK: 開いた履歴がウェルカム画面に載る")

    # --- タイトルバーの色（Windows 以外・古い OS でも落ちないこと）----
    w._apply_title_bar_theme(True)
    w._apply_title_bar_theme(False)
    print("OK: タイトルバーのテーマ適用は失敗しても落ちない")

    w._force_quit = True
    w.close()
    if os.path.exists(src):
        os.remove(src)
    print("ALL OK: テーマ / 外観")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
