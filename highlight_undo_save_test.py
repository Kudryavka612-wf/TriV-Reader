"""「全てハイライト」の取り消しと、保存の書き切り（fsync）・一時ファイルの後始末の確認。

7. 検索結果の「全てハイライト」は、ページをまたいでも Ctrl+Z 1 回で全部消える。以前は
   履歴に積まれず、Ctrl+Z でその前の操作（例: 四角の注釈）が取り消された。
8. 保存は、一時ファイルをディスクまで書き切って（flush + fsync）から置き換える。
   「最適化して保存」で開いているファイル自身へ上書きするとき、書き込みに失敗しても
   .tmp_save を残さない。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import fitz
from PySide6.QtWidgets import QApplication

from viewer.document import PdfDocument

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def make(path: str, pages: int = 3) -> None:
    d = fitz.open()
    for i in range(pages):
        pg = d.new_page(width=300, height=300)
        pg.insert_text((50, 100), f"apple page {i + 1}", fontsize=14)
    d.save(path)
    d.close()


def kinds(doc) -> list:
    return [[a.type[1] for a in p.annots()] for p in doc._doc]


def test_highlight_undo(tmp) -> None:
    src = os.path.join(tmp, "hl.pdf")
    make(src)
    doc = PdfDocument()
    doc.open(src)
    doc.add_rect(0, fitz.Rect(10, 10, 60, 60))          # その前の操作
    hits = doc.search("apple")
    n = doc.add_search_highlights(hits)
    check("7: highlights were added on all 3 pages", n == 3 and all(
        "Highlight" in k for k in kinds(doc)), str(kinds(doc)))
    check("7: the highlight is the next thing to undo", doc.can_undo()
          and doc.undo_label() != "", repr(doc.undo_label()))
    result = doc.undo()
    check("7: one Ctrl+Z removes every highlight (all pages) and keeps the earlier rectangle",
          kinds(doc) == [["Square"], [], []], str(kinds(doc)))
    check("7: …and tells which pages to redraw",
          result is not None and sorted(doc.last_history_pages) == [0, 1, 2],
          str((result, doc.last_history_pages)))
    doc.redo()
    check("7: redo brings all highlights back",
          kinds(doc) == [["Square", "Highlight"], ["Highlight"], ["Highlight"]],
          str(kinds(doc)))
    doc.undo()
    doc.undo()
    check("7: a second Ctrl+Z then undoes the rectangle", kinds(doc) == [[], [], []],
          str(kinds(doc)))
    doc.close()


def test_fsync(tmp) -> None:
    src = os.path.join(tmp, "fs.pdf")
    make(src, 1)
    doc = PdfDocument()
    doc.open(src)
    doc.add_rect(0, fitz.Rect(10, 10, 60, 60))
    synced: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    order: list[str] = []

    def fake_fsync(fd):
        synced.append(fd)
        order.append("fsync")
        return real_fsync(fd)

    def fake_replace(a, b):
        order.append("replace")
        return real_replace(a, b)

    os.fsync, os.replace = fake_fsync, fake_replace
    try:
        doc.save_as(os.path.join(tmp, "fs_out.pdf"))
        doc.save()   # 開いているファイル自身へ
    finally:
        os.fsync, os.replace = real_fsync, real_replace
    check("8: the temporary file is fsync'ed before it replaces the target",
          order[:2] == ["fsync", "replace"] and order.count("fsync") >= 2, str(order))
    doc.close()


def test_compress_self_failure(tmp) -> None:
    src = os.path.join(tmp, "cs.pdf")
    make(src, 1)
    before = open(src, "rb").read()
    doc = PdfDocument()
    doc.open(src)
    real_save = fitz.Document.save

    def failing_save(self, filename, *a, **k):
        if str(filename).endswith(".tmp_save"):
            with open(filename, "wb") as f:   # 途中まで書いて失敗（容量不足など）
                f.write(b"%PDF-1.7 partial")
            raise RuntimeError("disk full")
        return real_save(self, filename, *a, **k)

    fitz.Document.save = failing_save
    raised = False
    try:
        try:
            doc.compress_to_advanced(src, max_dpi=0)
        except RuntimeError:
            raised = True
    finally:
        fitz.Document.save = real_save
    check("8: a failed compress-over-itself raises", raised)
    check("8: …leaves no .tmp_save behind", not os.path.exists(src + ".tmp_save"),
          str(os.listdir(tmp)))
    check("8: …and the original file is untouched", open(src, "rb").read() == before)
    doc.close()


def main() -> int:
    app = QApplication(sys.argv)   # noqa: F841
    tmp = tempfile.mkdtemp(prefix="trv_hlsave_")
    try:
        for t in (test_highlight_undo, test_fsync, test_compress_self_failure):
            try:
                t(tmp)
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(f"{t.__name__} ran without an error", False, repr(exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
