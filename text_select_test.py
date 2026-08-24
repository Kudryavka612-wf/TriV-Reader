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

import fitz

from viewer.document import PdfDocument

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


def main() -> int:
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
    finally:
        shutil.rmtree(root, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    raise SystemExit(main())
