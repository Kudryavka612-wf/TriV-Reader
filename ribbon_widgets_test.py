"""リボン内の入力コントロールが実際に表示・操作可能かを検証する回帰テスト。

太さ・線種・矢印の始点/終点・不透明・文字サイズ・ズーム等のウィジェットは、
元々ツールバーへ addWidget してからリボンへ移設している。ツールバーが作る
QWidgetAction がウィジェットを所有し続けるため、リボン採用時の tb.hide() で
一緒に隠されてしまい「コントロールが操作できない」不具合があった（回帰防止）。

offscreen で実レイアウトを走らせ、各コントロールが hidden=False かつ妥当な
サイズに配置されること、選択中の注釈へ書式が反映されることを確認する。
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtWidgets import QApplication

from viewer.main_window import MainWindow
from viewer import page_view


def main() -> int:
    app = QApplication(sys.argv[:1])
    sample = os.path.join(tempfile.gettempdir(), "_ribbon_widgets.pdf")
    d = fitz.open()
    d.new_page(width=595, height=842)
    d.save(sample)
    d.close()

    w = MainWindow()
    w.resize(1300, 850)
    w.show()
    app.processEvents()
    w.open_path(sample)
    app.processEvents()

    results: list[bool] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        results.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")

    rb = w.ribbon
    # 折りたたみ設定に左右されず中身を検証したいので、明示的に展開する。
    rb.set_collapsed(False)
    for i in range(rb.count()):
        if rb.tabText(i) == "注釈":
            rb.setCurrentIndex(i)
    app.processEvents()
    app.processEvents()

    # 注釈タブのコントロールが「隠れていない」「まともなサイズで置かれている」こと。
    controls = ["width_spin", "line_style_combo", "line_start_combo",
                "line_end_combo", "opacity_spin", "text_size_spin", "stamp_btn"]
    for nm in controls:
        ww = getattr(w, nm)
        ok = (not ww.isHidden()) and ww.isVisible() and 8 < ww.width() < 300
        check(f"{nm} is shown in the ribbon", ok,
              f"(hidden={ww.isHidden()} {ww.width()}x{ww.height()})")

    # ホームタブのズーム系コントロールも同じ経路。タブを出して確認する。
    for i in range(rb.count()):
        if rb.tabText(i) == "ホーム":
            rb.setCurrentIndex(i)
    app.processEvents()
    for nm in ["zoom_slider", "zoom_combo"]:
        ww = getattr(w, nm)
        check(f"{nm} is shown in the ribbon", not ww.isHidden(),
              f"(hidden={ww.isHidden()})")

    # 実際に書式変更が「選択中の注釈」に効くこと（コントロール→PageView 連携）。
    pv = w.page_view
    pv.set_tool(page_view.TOOL_LINE)
    pv.set_line_start("none")
    pv.set_line_end("closed_arrow")
    from fitz import Point
    pv._doc.add_line(0, Point(60, 60), Point(200, 200), color=(1, 0, 0),
                     width=2, arrow=True, start="none", end="closed_arrow")
    xref = list(pv._doc._doc.load_page(0).annots())[-1].xref
    pv.set_tool(page_view.TOOL_NONE)
    pv.set_selection(0, xref)

    def line_ends():
        for a in pv._doc._doc.load_page(0).annots():
            if a.xref == pv._sel_xref:
                return a.line_ends
        return None

    def width_of():
        for a in pv._doc._doc.load_page(0).annots():
            if a.xref == pv._sel_xref:
                return (a.border or {}).get("width")
        return None

    # ツールバーのスピン/コンボを動かしたのと同じ経路（valueChanged/indexChanged）。
    w.width_spin.setValue(11)
    check("changing 太さ restyles the selected annot", width_of() == 11.0,
          f"(w={width_of()})")
    from viewer.document import LINE_END_STYLES
    w._set_combo_data(w.line_end_combo, "open_arrow")
    check("changing 終点 restyles the selected annot",
          line_ends() is not None and line_ends()[1] == LINE_END_STYLES["open_arrow"],
          f"(ends={line_ends()})")

    # 狭いウィンドウでもツールボタンが文字幅より縮んで「ハ..ト」のように
    # 見切れないこと（収まらない分はリボンの横スクロールで送る）。
    from PySide6.QtWidgets import QToolButton
    for i in range(rb.count()):
        if rb.tabText(i) == "注釈":
            rb.setCurrentIndex(i)
    w.resize(690, 420)
    app.processEvents()
    app.processEvents()
    squeezed = []
    tool_btns = [b for b in rb.findChildren(QToolButton)
                 if b.isVisible() and b.text()
                 and b.defaultAction() in w.tool_group.actions()]
    for b in tool_btns:
        need = b.fontMetrics().horizontalAdvance(b.text()) + 34
        if b.width() < need - 2:
            squeezed.append((b.text(), b.width(), need))
    check("tool buttons keep full label width at 690px", not squeezed,
          str(squeezed))
    # 2段目（例: 四角）が縦方向にも隠れず見えていること
    ys = sorted({b.y() for b in tool_btns})
    check("both stacked rows are visible at 690px",
          len(ys) >= 2 and all(0 <= y < rb.height() for y in ys), f"(rows y={ys})")
    w.resize(1300, 850)
    app.processEvents()

    if os.path.exists(sample):
        os.remove(sample)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
