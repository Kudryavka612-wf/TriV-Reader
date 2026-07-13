"""連続スクロール表示の機能テスト（offscreen で実レイアウトを検証）。"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtWidgets import QApplication

from viewer.main_window import MainWindow


def build_sample(path: str, pages: int = 6) -> None:
    doc = fitz.open()
    for n in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 144), f"page {n + 1}", fontsize=24)
    doc.save(path)
    doc.close()


def main() -> int:
    app = QApplication(sys.argv)
    sample = os.path.join(tempfile.gettempdir(), "_trivreader_scroll.pdf")
    build_sample(sample, pages=6)

    w = MainWindow()
    w.resize(900, 700)
    w.show()
    app.processEvents()

    w.open_path(sample)  # タブで開く
    app.processEvents()

    pv = w.page_view
    assert len(pv._labels) == 6, len(pv._labels)

    # ラベルが縦に積まれている（y 座標が増加）か
    ys = [lbl.y() for lbl in pv._labels]
    assert ys == sorted(ys) and ys[0] < ys[-1], ys

    # 先頭付近は描画され、最後のページは未描画（遅延描画）か
    pv.set_page(0)
    app.processEvents()
    assert 0 in pv._rendered, pv._rendered
    assert 5 not in pv._rendered, f"末尾まで描画されている: {pv._rendered}"

    # 最終ページへスクロール → 描画され、現在ページが追従するか
    pv.set_page(5)
    app.processEvents()
    assert 5 in pv._rendered, pv._rendered
    assert pv.index == 5, pv.index

    # 回転してもサイズが更新され再描画されるか
    before = (pv._labels[5].width(), pv._labels[5].height())
    w.doc.rotate_page(5, 90)
    pv.refresh_page(5)
    after = (pv._labels[5].width(), pv._labels[5].height())
    assert before[0] == after[1] and before[1] == after[0], (before, after)

    print(f"OK: labels=6, y={ys}, rendered_after_top={sorted(pv._rendered)}")
    print(f"OK: rotate resized {before} -> {after}")

    # --- オートスクロールの計算ロジック ----------------------------
    # _auto_scroll_tick は実カーソル位置（QCursor.pos）を参照するため、
    # QCursor.setPos で仮想カーソルを動かして検証する（実行環境の実マウス
    # 位置に依存しないように）。
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QCursor

    pv.set_page(0)
    app.processEvents()
    start_val = pv.verticalScrollBar().value()
    pv._auto_active = True
    pv._auto_origin = QPoint(100, 100)
    QCursor.setPos(pv.viewport().mapToGlobal(QPoint(100, 300)))  # 下へ大きく
    pv._auto_scroll_tick()
    assert pv.verticalScrollBar().value() > start_val, "下方向スクロールされない"
    QCursor.setPos(pv.viewport().mapToGlobal(QPoint(100, 105)))  # デッドゾーン内
    held = pv.verticalScrollBar().value()
    pv._auto_scroll_tick()
    assert pv.verticalScrollBar().value() == held, "デッドゾーンで動いてしまう"
    pv._stop_autoscroll()
    print("OK: 中クリックオートスクロール（方向・デッドゾーン）")

    # --- ページ編集（UI 層経由） -----------------------------------
    w._delete_page(2)
    app.processEvents()
    assert w.doc.page_count == 5, w.doc.page_count
    assert len(pv._labels) == 5, len(pv._labels)
    assert pv._container.height() > pv.viewport().height()  # 連続表示が維持
    w._reorder([4, 3, 2, 1, 0])  # 逆順
    app.processEvents()
    assert w.doc.page_count == 5
    print(f"OK: UI削除→5ページ, 並べ替え後ラベル数={len(pv._labels)}")

    # --- 範囲指定パーサ --------------------------------------------
    from viewer.main_window import _parse_ranges

    assert _parse_ranges("1-3,5", 10) == [0, 1, 2, 4]
    assert _parse_ranges("3-1", 10) == [2, 1, 0]  # 逆順も保持
    try:
        _parse_ranges("99", 10)
        raise AssertionError("範囲外を弾けていない")
    except ValueError:
        pass
    print("OK: 範囲指定パーサ (1-3,5 / 逆順 / 範囲外検出)")

    # --- ページ整理画面（大画面グリッド） --------------------------
    w.organizer.load(w.doc)
    app.processEvents()
    n_before = w.doc.page_count
    assert w.organizer.grid.count() == n_before, w.organizer.grid.count()

    # 整理画面への切り替え（ビューワーが隠れて整理画面が前面に）
    w.act_organize.setChecked(True)
    app.processEvents()
    assert w._active_tab().currentWidget() is w.organizer

    # 複数選択 → まとめて削除
    w.organizer.grid.item(0).setSelected(True)
    w.organizer.grid.item(1).setSelected(True)
    assert w.organizer.selected_indices() == [0, 1]
    w._delete_pages([0, 1])
    app.processEvents()
    assert w.doc.page_count == n_before - 2, w.doc.page_count
    assert w.organizer.grid.count() == n_before - 2

    # 選択ページの回転（整理画面経由）
    rot_before = w.doc.rotation(0)
    w._rotate_pages([0], 90)
    app.processEvents()
    assert w.doc.rotation(0) == (rot_before + 90) % 360

    # ダブルクリック相当 → ビューワーへ戻る
    w._jump_from_organizer(0)
    app.processEvents()
    assert w._active_tab().currentWidget() is w.page_view
    assert not w.act_organize.isChecked()
    print(f"OK: 整理画面 複数削除→{w.doc.page_count}ページ / 回転 / ビューワー復帰")

    # --- 注釈（ビューワー経由のコミット） --------------------------
    from PySide6.QtCore import QPoint
    from viewer import page_view as pv_mod

    # ビューワーに戻す
    w.act_organize.setChecked(False)
    app.processEvents()
    idx = w.page_view.index
    base = w.doc.annot_count(idx)

    # ハイライト（矩形ドラッグ相当）
    w.page_view.set_tool(pv_mod.TOOL_HIGHLIGHT)
    w.page_view.commit_drag(idx, QPoint(40, 40), QPoint(200, 90), pv_mod.TOOL_HIGHLIGHT)
    # ペン（フリーハンド相当）
    w.page_view.set_tool(pv_mod.TOOL_PEN)
    w.page_view.commit_stroke(idx, [QPoint(60, 120), QPoint(90, 140), QPoint(120, 120)])
    # 四角
    w.page_view.set_tool(pv_mod.TOOL_RECT)
    w.page_view.commit_drag(idx, QPoint(50, 160), QPoint(180, 220), pv_mod.TOOL_RECT)
    app.processEvents()
    assert w.doc.annot_count(idx) == base + 3, w.doc.annot_count(idx)
    print(f"OK: 注釈追加 {base}→{w.doc.annot_count(idx)}（ハイライト/ペン/四角）")

    # 消しゴム：ペン(ink, 60-120,120-140付近)を消す
    w.page_view.set_tool(pv_mod.TOOL_ERASE)
    w.page_view.commit_point(idx, pv_mod.TOOL_ERASE, QPoint(90, 130))
    app.processEvents()
    assert w.doc.annot_count(idx) == base + 2, w.doc.annot_count(idx)
    print(f"OK: 消しゴムで1件削除 →{w.doc.annot_count(idx)}")

    # --- 選択・移動・色変更・リサイズ・削除（選択ツール） ----------
    w.page_view.set_tool(pv_mod.TOOL_NONE)
    # 残っている四角(50,160)-(180,220)の中心あたりを選択
    xref = w.page_view.hit_annot(idx, QPoint(115, 190))
    assert xref is not None, "注釈をヒットできない"
    w.page_view.set_selection(idx, xref)
    assert w.page_view.has_selection()
    z = w.page_view.zoom
    lb0 = w.doc.pdf_rect_to_label(idx, w.doc.annot_rect(idx, w.page_view._sel_xref), z)
    # ドラッグ移動（画面で右へ+40・下へ+20）
    w.page_view.move_selection(idx, QPoint(115, 190), QPoint(155, 210))
    lb1 = w.doc.pdf_rect_to_label(idx, w.doc.annot_rect(idx, w.page_view._sel_xref), z)
    assert lb1[0] > lb0[0] + 20 and lb1[1] > lb0[1] + 10, (lb0, lb1)
    print(f"OK: 移動 ラベルx {lb0[0]:.0f}→{lb1[0]:.0f}, y {lb0[1]:.0f}→{lb1[1]:.0f}")
    # 色変更（選択中・件数不変）
    from PySide6.QtGui import QColor
    cnt = w.doc.annot_count(idx)
    w.page_view.recolor_selection(QColor(0, 200, 0))
    assert w.doc.annot_count(idx) == cnt
    print("OK: 選択注釈の色変更")
    # リサイズ（選択中の四角をハンドルドラッグ相当で拡大）
    box = w.page_view.selection_box_label(idx)
    bigger = (box[0], box[1], box[2] + 60, box[3] + 40)
    w.page_view.resize_selection(idx, bigger)
    nb = w.page_view.selection_box_label(idx)
    assert (nb[2] - nb[0]) > (box[2] - box[0]), (box, nb)
    print(f"OK: リサイズ 幅 {box[2]-box[0]:.0f}→{nb[2]-nb[0]:.0f}")

    # 削除
    w.page_view.delete_selection()
    app.processEvents()
    assert not w.page_view.has_selection()
    print(f"OK: 選択削除 →{w.doc.annot_count(idx)}")

    # テキスト選択ベースのハイライト（文字のあるページ）
    tdoc = os.path.join(tempfile.gettempdir(), "_gui_text.pdf")
    td = fitz.open(); tpg = td.new_page(width=595, height=842)
    tpg.insert_text((72, 100), "Hello selectable world", fontsize=18)
    td.save(tdoc); td.close()
    w.open_path(tdoc)  # 新しいタブで開く
    app.processEvents()
    z = w.page_view.zoom
    # "Hello" 付近を横にドラッグ → 文字に沿うハイライト
    y = int(95 * z)
    w.page_view.set_tool(pv_mod.TOOL_HIGHLIGHT)
    w.page_view.commit_drag(0, QPoint(int(72 * z), y), QPoint(int(140 * z), y + 4),
                            pv_mod.TOOL_HIGHLIGHT)
    app.processEvents()
    assert w.doc.annot_count(0) >= 1
    print(f"OK: テキスト選択ハイライト →{w.doc.annot_count(0)}件")
    w.page_view.set_tool(pv_mod.TOOL_NONE)

    # --- しおりドック ----------------------------------------------
    w.doc.set_toc([[1, "見出しA", 1], [2, "小見出し", 1]])
    w.toc_dock.load(w.doc.get_toc())
    assert w.toc_dock.tree.topLevelItemCount() == 1
    assert w.toc_dock.tree.topLevelItem(0).childCount() == 1
    print("OK: しおりドックに階層表示")

    # --- 検索（tdoc は "Hello selectable world"） ------------------
    w.search_edit.setText("selectable")
    w._do_search()
    app.processEvents()
    assert w.page_view.search_count() == 1, w.page_view.search_count()
    assert "1 / 1" in w.search_count_label.text(), w.search_count_label.text()
    boxes = w.page_view.search_boxes_label(0)
    assert boxes and boxes[0][1] is True  # 現在ヒット
    print("OK: 検索ヒット表示", w.search_count_label.text())

    # 検索結果の一括ハイライト
    before_hl = w.doc.annot_count(0)
    w._highlight_all_hits()
    app.processEvents()
    assert w.doc.annot_count(0) == before_hl + 1
    print("OK: 検索結果を一括ハイライト")

    w.search_edit.setText("")
    w._do_search()
    assert w.page_view.search_count() == 0
    print("OK: 検索クリア")

    # ダークモードのオンオフ（クラッシュしないこと）
    w.act_dark.setChecked(True)
    app.processEvents()
    w.act_dark.setChecked(False)
    app.processEvents()
    print("OK: ダークモード切替")

    # コンパクト表示（アイコンのみ）トグル
    from PySide6.QtCore import Qt as _Qt
    w.act_compact.setChecked(True)
    app.processEvents()
    assert w.main_toolbar.toolButtonStyle() == _Qt.ToolButtonStyle.ToolButtonIconOnly
    assert not w.act_open.icon().isNull()  # アイコンが付与されている
    w.act_compact.setChecked(False)
    app.processEvents()
    assert w.main_toolbar.toolButtonStyle() == _Qt.ToolButtonStyle.ToolButtonTextOnly
    print("OK: コンパクト表示トグル")

    # 注釈バー（旧ツールバー）はリボン統合後は既定で非表示。トグルの往復のみ確認
    assert not w.annot_toolbar.isVisible()
    w.act_toggle_annotbar.trigger()
    app.processEvents()
    assert w.annot_toolbar.isVisible()
    w.act_toggle_annotbar.trigger()
    app.processEvents()
    assert not w.annot_toolbar.isVisible()
    print("OK: 注釈バー折りたたみトグル")

    # テキスト選択 → コピー（tdoc は "Hello selectable world"）
    from PySide6.QtWidgets import QApplication as _QA
    z = w.page_view.zoom
    y = int(95 * z)
    w.page_view.set_tool(pv_mod.TOOL_NONE)
    w.page_view.update_text_selection(0, QPoint(int(70 * z), y), QPoint(int(230 * z), y))
    assert w.page_view.has_text_selection(), "テキスト選択できない"
    assert w.act_copy.isEnabled()
    assert w.page_view.copy_selection()
    clip = _QA.clipboard().text()
    assert "selectable" in clip or "Hello" in clip, repr(clip)
    print("OK: テキスト選択→コピー", repr(clip))
    w.page_view.clear_text_selection()
    assert not w.page_view.has_text_selection()

    # --- ページ番号 ------------------------------------------------
    w.doc.add_page_numbers(position="bottom-center", fmt="{n} / {total}")
    import fitz as _fitz
    assert "1 / 1" in w.doc._doc.load_page(0).get_text()
    print("OK: ページ番号焼き込み")

    # --- 新ツール（円・墨消し）と墨消し適用 ------------------------
    z = w.page_view.zoom
    base_a = w.doc.annot_count(0)
    w.page_view.set_tool(pv_mod.TOOL_CIRCLE)
    w.page_view.commit_drag(0, QPoint(int(60*z), int(180*z)),
                            QPoint(int(160*z), int(240*z)), pv_mod.TOOL_CIRCLE)
    assert w.doc.annot_count(0) == base_a + 1
    print("OK: 円注釈ツール")
    # 墨消しツール → 文字の上に矩形 → 適用で消える
    w.page_view.set_tool(pv_mod.TOOL_REDACT)
    w.page_view.commit_drag(0, QPoint(int(70*z), int(88*z)),
                            QPoint(int(220*z), int(105*z)), pv_mod.TOOL_REDACT)
    assert w.doc.pending_redactions(0) >= 1
    w.page_view.apply_redactions()
    app.processEvents()
    assert "Hello" not in w.doc._doc.load_page(0).get_text()
    print("OK: 墨消しツール→適用で文字削除")
    w.page_view.set_tool(pv_mod.TOOL_NONE)

    # --- ズームプリセット ------------------------------------------
    w.zoom_combo.setCurrentText("150%")
    w._on_zoom_preset(0)
    assert abs(w.page_view.zoom - 1.5) < 0.001, w.page_view.zoom
    assert w.zoom_slider.value() == 150
    print("OK: ズームプリセット 150%")

    # --- タブ複数開き ----------------------------------------------
    n_tabs = w.tabs.count()
    facedoc = os.path.join(tempfile.gettempdir(), "_gui_face.pdf")
    build_sample(facedoc, pages=6)
    w.open_path(facedoc)  # 新しいタブ
    app.processEvents()
    assert w.tabs.count() == n_tabs + 1, w.tabs.count()
    assert w.doc.page_count == 6
    print(f"OK: タブ複数開き（{w.tabs.count()}タブ）")

    # --- 見開き表示（2列レイアウト） -------------------------------
    w.act_facing.setChecked(True)
    app.processEvents()
    assert w.page_view._cols == 2
    y0 = w.page_view._labels[0].y()
    y1 = w.page_view._labels[1].y()
    y2 = w.page_view._labels[2].y()
    assert abs(y0 - y1) < 5 and y2 > y0, (y0, y1, y2)
    print(f"OK: 見開き 2列配置 y=[{y0},{y1},{y2}]")
    w.act_facing.setChecked(False)
    app.processEvents()
    assert w.page_view._cols == 1
    print("OK: 見開き解除")

    # --- タブを閉じる ----------------------------------------------
    before_close = w.tabs.count()
    w._close_tab(w.tabs.currentIndex())
    app.processEvents()
    assert w.tabs.count() == before_close - 1
    print(f"OK: タブを閉じる（{w.tabs.count()}タブ）")

    # --- 最近のファイル / 設定 -------------------------------------
    w._add_recent(tdoc)
    recent = w.settings.value("recent", [], list)
    assert tdoc in recent
    assert w.recent_menu.actions(), "最近メニューが空"
    print("OK: 最近開いたファイル登録")

    # 後始末：全タブのドキュメントを閉じてからファイル削除
    for i in range(w.tabs.count()):
        t = w.tabs.widget(i)
        if hasattr(t, "doc"):
            t.doc.close()
    for f in (sample, tdoc, facedoc):
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))
    raise SystemExit(main())
