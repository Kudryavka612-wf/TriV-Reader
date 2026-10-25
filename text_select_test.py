"""文字選択の精度テスト。

行末より右をクリックすると隣の行の文字を拾う、段組みで別の段へ飛ぶ、といった
取りこぼしの回帰テスト。座標はフォント幅に依存しないよう、PDF から実際の文字
位置を引いて組み立てる。
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
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from viewer.document import PdfDocument
from viewer.main_window import MainWindow

ROOT = os.path.dirname(os.path.abspath(__file__))
results: list[bool] = []

# 左段（x=72〜）と右段（x=330〜）の2段組み。y はベースライン。
LEFT = [(72, 100, "Hello World"),
        (72, 130, "Second line"),
        (72, 160, "日本語のテキスト")]
RIGHT = [(330, 100, "Right column"),
         (330, 130, "Lower right")]


def check(cond: bool, label: str) -> bool:
    print(("OK  : " if cond else "NG  : ") + label)
    results.append(bool(cond))
    return bool(cond)


def build(path: str) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    for x, y, text in LEFT + RIGHT:
        page.insert_text((x, y), text, fontsize=12, fontname="japan")
    doc.save(path)
    doc.close()


def box_of(doc: PdfDocument, snippet: str) -> fitz.Rect:
    """PDF 内の文字列の位置（テストがフォント幅に左右されないように実測する）。"""
    page = doc._doc.load_page(0)
    hits = page.search_for(snippet)
    assert hits, f"見つからない: {snippet}"
    return hits[0]


def center(rect: fitz.Rect) -> fitz.Point:
    return fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)


def sel(doc: PdfDocument, p0, p1) -> str:
    return doc.select_text(0, p0, p1)[0]


def test_drag_within_line(doc: PdfDocument) -> None:
    """1行の中をドラッグすると、その範囲だけが取れる。"""
    hello = box_of(doc, "Hello")
    world = box_of(doc, "World")
    text = sel(doc, fitz.Point(hello.x0, center(hello).y),
               fitz.Point(world.x1, center(world).y))
    check(text.replace(" ", "") == "HelloWorld", f"語の境界どおりに取れる（{text!r}）")
    check("\n" not in text, f"隣の行を巻き込まない（{text!r}）")


def test_click_right_of_line_end(doc: PdfDocument) -> None:
    """行末より右をクリックしても、その行の末尾で止まる（次の行に飛ばない）。"""
    line = box_of(doc, "Hello World")
    right_of_end = fitz.Point(line.x1 + 40, center(line).y)  # 行末の右の余白
    text = sel(doc, fitz.Point(line.x0, center(line).y), right_of_end)
    check(text.replace(" ", "") == "HelloWorld", f"行末まで取れる（{text!r}）")
    check("Second" not in text, f"次の行を取り込まない（{text!r}）")


def test_left_column_only(doc: PdfDocument) -> None:
    """段組みで、左段のドラッグが右段を巻き込まない。"""
    first = box_of(doc, "Hello World")
    second = box_of(doc, "Second line")
    text = sel(doc, fitz.Point(first.x0, center(first).y),
               fitz.Point(second.x1, center(second).y))
    check("Right" not in text, f"右段を巻き込まない（{text!r}）")
    check("Hello" in text and "line" in text, f"左段の2行が取れる（{text!r}）")


def test_right_column(doc: PdfDocument) -> None:
    """右段をクリックしたら右段の文字が選ばれる。"""
    box = box_of(doc, "Right column")
    text = sel(doc, fitz.Point(box.x0, center(box).y),
               fitz.Point(box.x1, center(box).y))
    check(text.replace(" ", "") == "Rightcolumn", f"右段の行が取れる（{text!r}）")
    check("Hello" not in text, f"左段に飛ばない（{text!r}）")


def test_click_between_columns(doc: PdfDocument) -> None:
    """段と段のあいだをクリックしたら、近いほうの段に付く。"""
    left = box_of(doc, "Hello World")
    right = box_of(doc, "Right column")
    y = center(left).y
    near_left = fitz.Point(left.x1 + (right.x0 - left.x1) * 0.25, y)
    near_right = fitz.Point(left.x1 + (right.x0 - left.x1) * 0.75, y)
    t1 = sel(doc, fitz.Point(left.x0, y), near_left)
    check("Right" not in t1 and "World" in t1, f"左寄りなら左段の行末（{t1!r}）")
    t2 = sel(doc, near_right, fitz.Point(right.x1, y))
    check("Right" in t2, f"右寄りなら右段（{t2!r}）")


def test_outside_page(doc: PdfDocument) -> None:
    """本文の外（余白）をクリックしても、いちばん近い行に寄せる。"""
    first = box_of(doc, "Hello World")
    top = sel(doc, fitz.Point(first.x0, 20), fitz.Point(first.x1, center(first).y))
    check("Hello" in top, f"上余白から引くと先頭行に寄る（{top!r}）")
    last = box_of(doc, "日本語")
    bottom = sel(doc, fitz.Point(last.x0, center(last).y), fitz.Point(400, 800))
    check("日本語" in bottom, f"下余白まで引くと最終行まで取れる（{bottom!r}）")


def test_word_select(doc: PdfDocument) -> None:
    """ダブルクリック相当（select_word）で語が取れる。"""
    word, rects = doc.select_word(0, center(box_of(doc, "Hello")))
    check(word == "Hello", f"英単語を選べる（{word!r}）")
    check(len(rects) == 1, f"選択矩形は1行分（{len(rects)}）")

    word, _ = doc.select_word(0, center(box_of(doc, "World")))
    check(word == "World", f"隣の語を選べる（{word!r}）")

    word, _ = doc.select_word(0, center(box_of(doc, "column")))
    check(word == "column", f"右段の語も選べる（{word!r}）")


def test_word_select_japanese(doc: PdfDocument) -> None:
    """日本語は文字種の続くまとまりを1語として選ぶ。"""
    word, _ = doc.select_word(0, center(box_of(doc, "日本語")))
    check(word == "日本語", f"漢字のまとまりを選べる（{word!r}）")
    word, _ = doc.select_word(0, center(box_of(doc, "テキスト")))
    check(word == "テキスト", f"カタカナのまとまりを選べる（{word!r}）")


def test_line_select(doc: PdfDocument) -> None:
    """トリプルクリック相当（select_line）で行全体が取れる。"""
    text, rects = doc.select_line(0, center(box_of(doc, "line")))
    check(text.replace(" ", "") == "Secondline", f"行を丸ごと選べる（{text!r}）")
    check(len(rects) == 1, f"選択矩形は1行分（{len(rects)}）")

    text, _ = doc.select_line(0, center(box_of(doc, "Right column")))
    check(text.replace(" ", "") == "Rightcolumn", f"右段の行も選べる（{text!r}）")


def test_layout_cache_speed(doc: PdfDocument) -> None:
    """ドラッグ中に何度呼んでも重くならない（レイアウトを憶えている）。"""
    box = box_of(doc, "Hello World")
    y = center(box).y
    doc.invalidate_text_layout()
    t0 = time.perf_counter()
    doc.select_text(0, fitz.Point(box.x0, y), fitz.Point(box.x1, y))
    first = time.perf_counter() - t0

    t0 = time.perf_counter()
    for n in range(50):
        doc.select_text(0, fitz.Point(box.x0, y), fitz.Point(box.x0 + n, y))
    rest = (time.perf_counter() - t0) / 50
    check(rest < first,
          f"2回目以降が速い（初回 {first*1000:.2f}ms / 以降 {rest*1000:.2f}ms）")


def test_cache_invalidation(root: str) -> None:
    """ページを消したら、憶えていたレイアウトを使い続けない。"""
    pdf = os.path.join(root, "two.pdf")
    doc = PdfDocument()
    doc.open(pdf)
    doc.insert_blank_page(0)          # 先頭に白紙を差し込む
    doc.invalidate_text_layout()
    text = doc.select_line(0, fitz.Point(150, 96))[0]
    check(text == "", f"白紙ページでは何も選ばれない（{text!r}）")
    doc.close()


def build_pages(path: str, pages: int = 3) -> None:
    """1ページに1行だけ書いた、ページまたぎ確認用の PDF。"""
    doc = fitz.open()
    for n in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"Page{n + 1} alpha bravo", fontsize=14,
                         fontname="japan")
    doc.save(path)
    doc.close()


def test_select_across_pages(root: str) -> None:
    """ページをまたぐ選択で、間のページも含めて取れる。"""
    path = os.path.join(root, "multi.pdf")
    build_pages(path, 3)
    doc = PdfDocument()
    doc.open(path)

    text, per_page = doc.select_across(0, fitz.Point(72, 95),
                                       2, fitz.Point(400, 95))
    check(set(per_page) == {0, 1, 2}, f"3ページ分の矩形が返る（{sorted(per_page)}）")
    check(all(w in text for w in ("Page1", "Page2", "Page3")),
          f"またいだページの文字が全部入る（{text!r}）")

    # 逆向きのドラッグでも同じ範囲になる
    back, back_pages = doc.select_across(2, fitz.Point(400, 95),
                                         0, fitz.Point(72, 95))
    check(back == text and set(back_pages) == set(per_page),
          "下から上へ引いても同じ結果")

    # 途中で止めれば、その範囲だけ
    part, part_pages = doc.select_across(0, fitz.Point(72, 95),
                                         1, fitz.Point(400, 95))
    check(set(part_pages) == {0, 1} and "Page3" not in part,
          f"またいでいないページは含まない（{sorted(part_pages)}）")
    doc.close()


def test_select_all(root: str) -> None:
    """全選択で全ページのテキストが取れる。"""
    path = os.path.join(root, "multi.pdf")
    doc = PdfDocument()
    doc.open(path)
    text, per_page = doc.select_all_text()
    check(set(per_page) == {0, 1, 2}, f"全ページが対象（{sorted(per_page)}）")
    check(text.count("Page") == 3, f"各ページの文字が入る（{text!r}）")
    doc.close()


def test_markup_rects(root: str) -> None:
    """選択済みの範囲に、ハイライト/下線/取消線を付けられる。"""
    path = os.path.join(root, "markup.pdf")
    build_pages(path, 1)
    doc = PdfDocument()
    doc.open(path)
    _text, rects = doc.select_line(0, fitz.Point(120, 95))
    for kind, want in (("highlight", "Highlight"), ("underline", "Underline"),
                       ("strikeout", "StrikeOut")):
        ok = doc.markup_rects(0, rects, kind, (1.0, 0.86, 0.18))
        check(ok, f"{kind} を付けられる")
    kinds = [a.type[1] for a in doc._doc.load_page(0).annots()]
    check(kinds == ["Highlight", "Underline", "StrikeOut"],
          f"3種類の注釈ができる（{kinds}）")
    doc.close()


def label_bottom(view, index: int) -> QPoint:
    lbl = view._labels[index]
    return QPoint(lbl.width() - 30, lbl.height() - 10)


def test_drag_across_pages_in_view(app: QApplication, root: str) -> None:
    """連続スクロール表示で、ページをまたいでドラッグ選択できる。"""
    path = os.path.join(root, "multi.pdf")
    w = MainWindow()
    w.resize(1000, 780)
    w.show()
    app.processEvents()
    w.open_path(path)
    app.processEvents()

    pv = w.page_view
    first, second = pv._labels[0], pv._labels[1]
    start = QPoint(60, 40)
    end = QPoint(300, 60)
    app.sendEvent(first, QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(start),
        QPointF(first.mapToGlobal(start)),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    # 終点は2ページ目の上（マウスは1ページ目にグラブされたまま動く）
    app.sendEvent(first, QMouseEvent(
        QEvent.Type.MouseMove, QPointF(first.mapFromGlobal(second.mapToGlobal(end))),
        QPointF(second.mapToGlobal(end)),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    app.processEvents()

    check(pv.has_text_selection(), "ページをまたいでも選択できている")
    check(pv.selection_pages() == [0, 1],
          f"2ページ分が選択される（{pv.selection_pages()}）")
    text = pv.selection_text()
    check("Page1" in text and "Page2" in text, f"両ページの文字が入る（{text!r}）")
    check(bool(pv.text_sel_boxes_label(1)), "2ページ目にも選択の塗りが出る")

    app.sendEvent(first, QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(end),
        QPointF(second.mapToGlobal(end)),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    for i in range(w.tabs.count()):
        t = w.tabs.widget(i)
        if hasattr(t, "doc"):
            t.doc.close()


def test_select_all_and_menu_actions(app: QApplication, root: str) -> None:
    """Ctrl+A と、右クリックメニューから使う操作。"""
    path = os.path.join(root, "multi.pdf")
    w = MainWindow()
    w.resize(1000, 780)
    w.show()
    app.processEvents()
    w.open_path(path)
    app.processEvents()
    pv = w.page_view

    app.sendEvent(pv, QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_A,
        Qt.KeyboardModifier.ControlModifier, "a"))
    app.processEvents()
    check(pv.selection_pages() == [0, 1, 2],
          f"Ctrl+A で全ページが選ばれる（{pv.selection_pages()}）")

    # メニューの「選択文字列を検索」
    got = []
    pv.search_requested.connect(got.append)
    pv.search_requested.emit("alpha")
    app.processEvents()
    check(w.search_edit.text() == "alpha", "検索欄に選択文字列が入る")

    # メニューの「ハイライト」（選択したページすべてに付く）
    check(pv.markup_selection("highlight"), "選択範囲にハイライトを付けられる")
    counts = [len(list(w.doc._doc.load_page(i).annots())) for i in range(3)]
    check(all(c == 1 for c in counts), f"各ページに1件ずつ付く（{counts}）")
    check(not pv.has_text_selection(), "付けたら選択は解除される")

    # メニューの「右に回転」
    before = w.doc.rotation(0)
    pv.rotate_requested.emit(90)
    app.processEvents()
    check(w.doc.rotation(0) == (before + 90) % 360,
          f"メニューからページを回転できる（{before} → {w.doc.rotation(0)}）")

    for i in range(w.tabs.count()):
        t = w.tabs.widget(i)
        if hasattr(t, "doc"):
            t.doc.close()


def main() -> int:
    app = QApplication(sys.argv)
    root = tempfile.mkdtemp(prefix="trv_sel_")
    try:
        pdf = os.path.join(root, "text.pdf")
        build(pdf)
        build(os.path.join(root, "two.pdf"))
        doc = PdfDocument()
        doc.open(pdf)

        test_drag_within_line(doc)
        test_click_right_of_line_end(doc)
        test_left_column_only(doc)
        test_right_column(doc)
        test_click_between_columns(doc)
        test_outside_page(doc)
        test_word_select(doc)
        test_word_select_japanese(doc)
        test_line_select(doc)
        test_layout_cache_speed(doc)
        doc.close()
        test_cache_invalidation(root)
        test_select_across_pages(root)
        test_select_all(root)
        test_markup_rects(root)
        test_drag_across_pages_in_view(app, root)
        test_select_all_and_menu_actions(app, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    raise SystemExit(main())
