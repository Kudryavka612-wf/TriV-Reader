"""リンク・移動履歴・スナップショット・固定保存・全画面・タブ操作の統合テスト。

合成 PDF（目次リンク＋URL リンク＋注釈＋入力欄）を offscreen の実 MainWindow で
開き、本物のマウスイベントで検証する。外部フィクスチャ不要（CI で常に走る）。
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from viewer.document import PdfDocument
from viewer.main_window import MainWindow
from viewer.page_view import TOOL_NONE, TOOL_SNAPSHOT

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def build(path: str) -> None:
    d = fitz.open()
    for _ in range(3):
        d.new_page(width=400, height=600)
    p = d[0]
    p.insert_text((40, 60), "Contents", fontsize=18)
    p.insert_text((40, 100), "Go to page 3", fontsize=12)
    p.insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(38, 88, 140, 104), "page": 2})
    p.insert_text((40, 140), "https://example.com", fontsize=12)
    p.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(38, 128, 160, 144),
                   "uri": "https://example.com"})
    # 黒い四角（スナップショットの中身の確認用）
    p.draw_rect(fitz.Rect(200, 200, 300, 300), color=(0, 0, 0), fill=(0, 0, 0))
    # 注釈と入力欄（固定保存の確認用）
    p.add_highlight_annot(fitz.Rect(38, 50, 140, 66))
    w = fitz.Widget()
    w.field_name = "name"
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.rect = fitz.Rect(40, 400, 240, 430)
    w.field_value = "Taro"
    p.add_widget(w)
    d[2].insert_text((40, 60), "Page 3", fontsize=18)
    d.save(path)
    d.close()


def send(app, w, et, pt, button=Qt.MouseButton.LeftButton):
    app.sendEvent(w, QMouseEvent(et, QPointF(pt), QPointF(w.mapToGlobal(pt)),
                                 button, button, Qt.KeyboardModifier.NoModifier))
    app.processEvents()


def click(app, w, pt):
    send(app, w, QEvent.Type.MouseButtonPress, pt)
    send(app, w, QEvent.Type.MouseButtonRelease, pt)


def drag(app, w, p0, p1):
    send(app, w, QEvent.Type.MouseButtonPress, p0)
    send(app, w, QEvent.Type.MouseMove, p1)
    send(app, w, QEvent.Type.MouseButtonRelease, p1)


def main() -> int:
    tmp = tempfile.gettempdir()
    src = os.path.join(tmp, "_trivreader_links.pdf")
    build(src)

    # --- document 層：リンクの当たり判定（回転あり/なし）-------------------
    doc = PdfDocument()
    doc.open(src)
    links = doc.links(0)
    check("links(): 2 件", len(links) == 2, str([l["kind"] for l in links]))
    lk = doc.link_at(0, 60 * 1.5, 95 * 1.5, 1.5)
    check("link_at: 目次リンクに当たる", lk is not None and lk["kind"] == "goto"
          and lk["page"] == 2, str(lk))
    check("link_at: 外れれば None", doc.link_at(0, 300, 30, 1.0) is None)
    doc.rotate_page(0, 90)
    rot = doc.link_at(0, (600 - 95), 60, 1.0)  # 90° 右回転 → (x,y) = (H - y, x)
    check("link_at: 回転後も当たる", rot is not None and rot["kind"] == "goto", str(rot))
    doc.rotate_page(0, -90)
    check("links: 内容が変わればキャッシュを捨てる", 0 not in doc._links_cache)
    doc.close()

    app = QApplication(sys.argv[:1])
    win = MainWindow()
    win.resize(1000, 760)
    win.show()
    app.processEvents()
    win.open_path(src)
    app.processEvents()
    win.act_tool_none.trigger()
    pv = win.page_view
    pv.set_zoom(1.0)
    app.processEvents()
    lbl = pv._labels[0]

    # --- ホバー → 手カーソル & ステータス ----------------------------------
    hovered: list[str] = []
    pv.link_hovered.connect(hovered.append)
    send(app, lbl, QEvent.Type.MouseMove, QPoint(60, 95), Qt.MouseButton.NoButton)
    check("hover: リンク先を通知", hovered and hovered[-1] == "3 ページへ", str(hovered))
    check("hover: 手カーソル", lbl.cursor().shape() == Qt.CursorShape.PointingHandCursor)
    check("hover: ステータスバーに表示", win.statusBar().currentMessage() == "3 ページへ",
          win.statusBar().currentMessage())
    send(app, lbl, QEvent.Type.MouseMove, QPoint(300, 30), Qt.MouseButton.NoButton)
    check("hover: 離れたら I ビームに戻る", lbl.cursor().shape() == Qt.CursorShape.IBeamCursor)
    check("hover: 離れたら通知が空", hovered[-1] == "")

    # --- クリックで目次ジャンプ → Alt+← で戻る --------------------------------
    activated: list[dict] = []
    pv.link_activated.connect(activated.append)
    click(app, lbl, QPoint(60, 95))
    check("click: link_activated", activated and activated[-1]["kind"] == "goto")
    check("click: 3 ページ目へ移動", pv.index == 2, str(pv.index))
    check("history: 戻るが有効", win.act_nav_back.isEnabled())
    check("history: 進むは無効", not win.act_nav_forward.isEnabled())
    win.act_nav_back.trigger()
    app.processEvents()
    check("history: 戻ると 1 ページ目", pv.index == 0, str(pv.index))
    check("history: 進むが有効", win.act_nav_forward.isEnabled())
    win.act_nav_forward.trigger()
    app.processEvents()
    check("history: 進むと 3 ページ目", pv.index == 2, str(pv.index))
    win.act_nav_back.trigger()
    app.processEvents()

    # --- リンクの上からドラッグ → リンクではなくテキスト選択 ------------------
    n_before = len(activated)
    drag(app, lbl, QPoint(45, 95), QPoint(130, 96))
    check("drag on link: リンクは辿らない", len(activated) == n_before)
    check("drag on link: テキストが選ばれる", pv.has_text_selection(),
          repr(pv.selection_text()))
    pv.clear_text_selection()

    # --- URL リンク：危険なスキームは開かない（http は QDesktopServices へ）----
    win._on_link_activated({"kind": "uri", "uri": "javascript:alert(1)"})
    check("uri: javascript は拒否",
          "開けません" in win.statusBar().currentMessage(), win.statusBar().currentMessage())
    win._on_link_activated({"kind": "uri", "uri": "file:///C:/Windows"})
    check("uri: file は拒否", "開けません" in win.statusBar().currentMessage())

    # --- 目次（しおり）からのジャンプも履歴に入る ------------------------------
    win._nav_back.clear()
    win._nav_forward.clear()
    win.toc_dock.page_requested.emit(1)
    app.processEvents()
    check("toc: 2 ページ目へ", pv.index == 1)
    check("toc: 戻るが有効", win.act_nav_back.isEnabled())
    win.act_nav_back.trigger()
    app.processEvents()
    check("toc: 戻れる", pv.index == 0)

    # --- スナップショット ---------------------------------------------------
    copied: list[tuple] = []
    pv.snapshot_copied.connect(lambda w, h: copied.append((w, h)))
    win.act_tool_snapshot.trigger()
    app.processEvents()
    check("snapshot: ツールが選ばれる", pv.tool == TOOL_SNAPSHOT)
    app.clipboard().clear()
    drag(app, lbl, QPoint(200, 200), QPoint(300, 300))
    img = app.clipboard().image()
    check("snapshot: クリップボードに画像", not img.isNull())
    # 100pt を 200dpi で描く → 約 278px
    check("snapshot: 解像度 200dpi 相当", 270 <= img.width() <= 285, str(img.size()))
    check("snapshot: 中身が描けている（黒）",
          not img.isNull() and img.pixelColor(img.width() // 2, img.height() // 2).red() < 40)
    check("snapshot: 注釈は増えない", win.doc.annot_count(0) == 1, str(win.doc.annot_count(0)))
    check("snapshot: 通知", copied and copied[-1][0] == img.width())
    check("snapshot: 撮ったら選択ツールへ戻る", pv.tool == TOOL_NONE and win.act_tool_none.isChecked())
    app.clipboard().clear()
    win.act_copy_page_image.trigger()
    pimg = app.clipboard().image()
    check("page image: ページ全体（400pt→約1111px）", 1100 <= pimg.width() <= 1120, str(pimg.size()))

    # --- 固定して保存（フラット化）------------------------------------------
    out = os.path.join(tmp, "_trivreader_flat.pdf")
    if os.path.exists(out):
        os.remove(out)
    win.doc.flatten_to(out, annots=True, widgets=True)
    with fitz.open(out) as f:
        n_annots = sum(len(list(pg.annots())) for pg in f)
        n_widgets = sum(len(list(pg.widgets())) for pg in f)
        pix = f[0].get_pixmap(matrix=fitz.Matrix(2, 2), clip=fitz.Rect(38, 50, 140, 66))
        yellowish = sum(1 for x in range(0, pix.width, 8) for y in range(0, pix.height, 4)
                        if (lambda c: c[0] > 200 and c[2] < 120)(pix.pixel(x, y)))
        txt = f[0].get_text()
    check("flatten: 注釈が無くなる", n_annots == 0, str(n_annots))
    check("flatten: 入力欄が無くなる", n_widgets == 0, str(n_widgets))
    check("flatten: ハイライトの色は残る", yellowish > 0, str(yellowish))
    check("flatten: 入力値が文字として残る", "Taro" in txt)
    check("flatten: 元の文書は編集可能なまま", win.doc.annot_count(0) == 1 and win.doc.is_form())
    try:
        win.doc.flatten_to(win.doc.path)
        check("flatten: 同名への上書きは拒否", False)
    except ValueError:
        check("flatten: 同名への上書きは拒否", True)

    # --- 全画面 --------------------------------------------------------------
    # 利用者の実設定でパネルが消えていることがあるので、まず出しておく
    win.page_dock.show()
    win.page_dock.raise_()
    app.processEvents()
    win.act_fullscreen.setChecked(True)
    app.processEvents()
    check("fullscreen: リボンが隠れる", win.ribbon.isHidden())
    check("fullscreen: ステータスバーが隠れる", win.statusBar().isHidden())
    check("fullscreen: パネルが隠れる", win.page_dock.isHidden())
    check("fullscreen: Esc が効く状態", win._esc_fullscreen.isEnabled())
    win._esc_fullscreen.activated.emit()
    app.processEvents()
    check("fullscreen: Esc で解除", not win.act_fullscreen.isChecked())
    check("fullscreen: リボンが戻る", not win.ribbon.isHidden())
    check("fullscreen: パネルが戻る", not win.page_dock.isHidden())
    check("fullscreen: Esc は通常時は無効", not win._esc_fullscreen.isEnabled())

    # --- ショートカット一覧 -------------------------------------------------
    rows = win.shortcut_rows()
    keys = {k for k, _ in rows}
    check("shortcuts: 一覧が作れる", len(rows) > 20, str(len(rows)))
    check("shortcuts: F11 / Alt+Left / Ctrl+F が載る",
          {"F11", "Alt+Left", "Ctrl+F"} <= keys, str(sorted(keys)[:12]))
    check("shortcuts: 重複なし", len(keys) == len(rows))

    # --- タブ操作 -------------------------------------------------------------
    second = os.path.join(tmp, "_trivreader_links2.pdf")
    build(second)
    win.open_path(second)
    app.processEvents()
    check("tabs: 2 タブ", win.tabs.count() == 2)
    check("tabs: 他を閉じるが有効", win.act_close_others.isEnabled())
    win.close_other_tabs(win.tabs.currentIndex())
    app.processEvents()
    check("tabs: 他を閉じると 1 タブ", win.tabs.count() == 1
          and win.doc.path and os.path.samefile(win.doc.path, second))
    check("tabs: 閉じたタブの履歴は消える",
          all(win.tabs.indexOf(t) >= 0 for t, _ in win._nav_back + win._nav_forward))
    win._copy_path(win.doc.path)
    check("tabs: パスをコピー", app.clipboard().text() == os.path.normpath(second))
    win.close_all_tabs()
    app.processEvents()
    check("tabs: すべて閉じる", win.tabs.count() == 0)
    check("tabs: 閉じたら戻る/進むが無効",
          not win.act_nav_back.isEnabled() and not win.act_nav_forward.isEnabled())

    ok = all(results)
    print("ALL PASS" if ok else f"{results.count(False)} FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
