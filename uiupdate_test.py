"""v1.3.19 の UI 更新の回帰テスト（offscreen で実レイアウトを検証）。

- 表示倍率「縦に合わせる」（アクション / ズームコンボ / 開いた時の既定倍率）
- ページ整理画面の選択ページプレビュー
- テキスト注釈: 入力後の文字サイズ変更・フォント変更・その場（ダイアログ無し）編集
- 左パネル（ページ/しおり/注釈一覧）の表示状態と前面タブの記憶
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtWidgets import QApplication

from viewer import page_view as pv_mod
from viewer.main_window import MainWindow
from viewer.page_view import PAGE_SPACING


def build_sample(path: str, pages: int = 6) -> None:
    doc = fitz.open()
    for n in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 144), f"page {n + 1}", fontsize=24)
    doc.save(path)
    doc.close()


def main() -> int:
    app = QApplication(sys.argv[:1])
    sample = os.path.join(tempfile.gettempdir(), "_trivreader_uiupdate.pdf")
    build_sample(sample)

    w = MainWindow()
    w.resize(1100, 800)
    w.show()
    app.processEvents()

    # テストで書き換える設定は最後に元へ戻す
    settings_backup = {
        k: w.settings.value(k)
        for k in ("default_zoom_mode", "left_dock_active", "annot_dock_visible",
                  "page_dock_visible", "toc_dock_visible", "geometry", "dark",
                  "compact", "annot_bar", "ribbon_collapsed")
    }

    w.open_path(sample)
    app.processEvents()

    results: list[bool] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        results.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")

    pv = w.page_view
    doc = w.doc

    # --- 縦に合わせる ---------------------------------------------------
    w.act_fit_height.trigger()
    app.processEvents()
    _, h_pt = doc.page_pixel_size(pv.index, 1.0)
    expect = (pv.viewport().height() - PAGE_SPACING * 2) / h_pt
    check("act_fit_height sets zoom to fit page height",
          abs(pv.zoom - expect) < 0.02, f"(zoom={pv.zoom:.3f} expect={expect:.3f})")
    fh_zoom = pv.zoom
    w.act_fit.trigger()
    check("fit-height < fit-width for a portrait page", fh_zoom < pv.zoom,
          f"(h={fh_zoom:.3f} w={pv.zoom:.3f})")

    check("zoom combo has 縦に合わせる",
          any(w.zoom_combo.itemText(i) == "縦に合わせる"
              for i in range(w.zoom_combo.count())))
    w.zoom_combo.setCurrentText("縦に合わせる")
    w._on_zoom_preset(w.zoom_combo.currentIndex())
    check("zoom combo preset applies fit-height", abs(pv.zoom - fh_zoom) < 0.001)

    # --- 開いた時の既定倍率（fit_height を固定できる） -------------------
    check("default-zoom menu has fit_height preset",
          "fit_height" in w.default_zoom_actions)
    w.settings.setValue("default_zoom_mode", "fit_height")
    sample2 = os.path.join(tempfile.gettempdir(), "_trivreader_uiupdate2.pdf")
    build_sample(sample2, pages=2)
    w.open_path(sample2)
    app.processEvents()
    pv2 = w.page_view
    _, h2 = w.doc.page_pixel_size(pv2.index, 1.0)
    expect2 = (pv2.viewport().height() - PAGE_SPACING * 2) / h2
    check("newly opened PDF honours default_zoom_mode=fit_height",
          abs(pv2.zoom - expect2) < 0.02, f"(zoom={pv2.zoom:.3f})")
    w._close_tab(w.tabs.currentIndex())
    app.processEvents()

    # --- ページ整理: 選択ページのプレビュー ------------------------------
    w.act_organize.setChecked(True)
    app.processEvents()
    org = w.organizer
    check("organizer preview starts empty", org.preview._pixmap is None)
    org.select_positions([2])
    app.processEvents()
    check("organizer preview renders the selected page",
          org.preview._pixmap is not None and not org.preview._pixmap.isNull())
    check("organizer preview caption shows the page number",
          "ページ 3" in org.preview._caption, f"({org.preview._caption})")
    org.select_positions([0, 4])
    app.processEvents()
    check("organizer preview follows multi-selection (current row)",
          "5" in org.preview._caption and "2 ページ" in org.preview._caption,
          f"({org.preview._caption})")
    org.grid.clearSelection()
    app.processEvents()
    check("organizer preview clears when nothing is selected",
          org.preview._pixmap is None)
    w.act_organize.setChecked(False)
    app.processEvents()

    # --- テキスト注釈: サイズ/フォントの後編集・その場編集 ---------------
    doc.add_freetext(0, fitz.Rect(60, 60, 260, 120), "こんにちは",
                     color=(0, 0, 1), fontsize=18, fontname="helv")
    xref = [a.xref for a in doc._doc.load_page(0).annots()
            if a.type[1] == "FreeText"][-1]
    pv.set_tool(pv_mod.TOOL_NONE)
    pv.set_selection(0, xref)
    app.processEvents()
    check("selecting a FreeText syncs the size spin",
          w.text_size_spin.value() == 18, f"({w.text_size_spin.value()})")

    w.text_size_spin.setValue(30)
    app.processEvents()
    style = doc.annot_textstyle(0, pv._sel_xref)
    check("changing the size spin restyles the selected FreeText",
          style and abs(style["fontsize"] - 30) < 0.5, f"({style})")

    i = w.text_font_combo.findData("tiro")
    w.text_font_combo.setCurrentIndex(i)
    app.processEvents()
    style = doc.annot_textstyle(0, pv._sel_xref)
    check("changing the font combo restyles the selected FreeText",
          style and style["fontname"] == "tiro", f"({style})")
    check("text content survives size/font restyle",
          doc.annot_text(0, pv._sel_xref) == "こんにちは")

    # その場編集（ダイアログを開かず、編集ボックスが注釈位置に出る）
    pv.edit_selection_text()
    app.processEvents()
    check("edit_selection_text opens the inline editor (no dialog)",
          pv._text_editor is not None)
    check("inline editor is pre-filled with the current text",
          pv._text_editor is not None
          and pv._text_editor.toPlainText() == "こんにちは")
    pv._text_editor.setPlainText("編集後のテキスト")
    pv._text_editor._finish()
    app.processEvents()
    check("committing the inline editor updates the annotation",
          doc.annot_text(0, pv._sel_xref) == "編集後のテキスト",
          f"({doc.annot_text(0, pv._sel_xref)!r})")
    style = doc.annot_textstyle(0, pv._sel_xref)
    check("inline edit keeps size and font",
          style and abs(style["fontsize"] - 30) < 0.5
          and style["fontname"] == "tiro", f"({style})")

    # Esc（取消）では本文が変わらない
    pv.edit_selection_text()
    app.processEvents()
    pv._text_editor.setPlainText("破棄されるべき")
    pv._text_editor.cancelled.emit()
    app.processEvents()
    check("cancelling the inline editor keeps the old text",
          doc.annot_text(0, pv._sel_xref) == "編集後のテキスト")

    # --- リボン: 新コントロールが見えて操作可能 ---------------------------
    rb = w.ribbon
    rb.set_collapsed(False)
    for idx in range(rb.count()):
        if rb.tabText(idx) == "注釈":
            rb.setCurrentIndex(idx)
    app.processEvents()
    ff = w.text_font_combo
    check("font combo is shown in the ribbon",
          (not ff.isHidden()) and ff.width() > 8,
          f"(hidden={ff.isHidden()} {ff.width()}x{ff.height()})")
    check("opacity spin has a comfortable width",
          w.opacity_spin.width() >= 58, f"({w.opacity_spin.width()}px)")
    for idx in range(rb.count()):
        if rb.tabText(idx) == "ホーム":
            rb.setCurrentIndex(idx)
    app.processEvents()
    check("default-zoom dropdown is shown in the ribbon",
          not w.default_zoom_btn.isHidden())

    # --- 左パネルの表示状態と前面タブの記憶 ------------------------------
    w.annot_dock.show()
    w.annot_dock.raise_()
    app.processEvents()
    w._save_settings()
    check("annot dock visibility is saved",
          w.settings.value("annot_dock_visible", None, bool) is True)
    check("raised dock is saved as left_dock_active",
          w.settings.value("left_dock_active", "", str) == "annot",
          f"({w.settings.value('left_dock_active', '', str)!r})")

    # 設定を元に戻す（ユーザー環境を汚さない）
    for k, v in settings_backup.items():
        if v is None:
            w.settings.remove(k)
        else:
            w.settings.setValue(k, v)

    for p in (sample, sample2):
        if os.path.exists(p):
            os.remove(p)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
