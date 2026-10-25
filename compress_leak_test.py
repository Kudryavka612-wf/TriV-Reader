"""圧縮の「試算」の一時ファイルが、使われなかったときも残らないことの確認。

「試算」のあと「圧縮して保存…」を押し、保存先の選択でキャンセルしたり、保存先に元の
ファイル自身を選んだりすると、数百 MB になりうるスキャン PDF の試算ファイル
（%TEMP%/_trivreader_estimate_*.pdf）が残った。試算が失敗・キャンセルされたときも、
空のファイルが残った。
"""
from __future__ import annotations

import glob
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from viewer import mw_convert
from viewer.compress_dialog import CompressDialog
from viewer.main_window import MainWindow

results: list[bool] = []
TMP = tempfile.gettempdir()


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def estimates() -> set[str]:
    return set(glob.glob(os.path.join(TMP, "_trivreader_estimate_*.pdf")))


def main() -> int:
    app = QApplication(sys.argv)   # noqa: F841
    src = os.path.join(tempfile.mkdtemp(prefix="trv_cleak_"), "scan.pdf")
    d = fitz.open()
    pg = d.new_page(width=595, height=842)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 800, 1000), False)
    pix.set_rect(pix.irect, (200, 200, 220))
    pg.insert_image(fitz.Rect(0, 0, 595, 842), pixmap=pix)
    d.save(src)
    d.close()

    w = MainWindow()
    w.resize(1000, 700)
    w.show()
    w.open_path(src)
    app.processEvents()
    baseline = estimates()

    orig_exec = CompressDialog.exec
    orig_save = mw_convert.QFileDialog.getSaveFileName
    orig_info = mw_convert.QMessageBox.information
    orig_question = mw_convert.QMessageBox.question

    def run(save_target):
        """試算してから「圧縮して保存」へ。save_target が保存先の選択の結果。"""
        def fake_exec(self):
            self._radios["standard"].setChecked(True)
            self._estimate()
            return QDialog.DialogCode.Accepted
        CompressDialog.exec = fake_exec
        mw_convert.QFileDialog.getSaveFileName = staticmethod(
            lambda *a, **k: (save_target, "PDF"))
        mw_convert.QMessageBox.information = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Ok)
        mw_convert.QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.No)
        try:
            w.compress_document()
        finally:
            CompressDialog.exec = orig_exec
            mw_convert.QFileDialog.getSaveFileName = orig_save
            mw_convert.QMessageBox.information = orig_info
            mw_convert.QMessageBox.question = orig_question

    run("")                                   # 保存先の選択でキャンセル
    check("cancelling the save-as dialog leaves no estimate file", estimates() == baseline,
          str(estimates() - baseline))
    run(src)                                  # 元のファイル自身へ上書き
    check("compressing over the file itself leaves no estimate file",
          estimates() == baseline, str(estimates() - baseline))
    run(os.path.join(os.path.dirname(src), "out.pdf"))   # 試算をそのまま使う
    check("using the estimate for the save leaves no estimate file either",
          estimates() == baseline and os.path.exists(os.path.join(os.path.dirname(src), "out.pdf")),
          str(estimates() - baseline))

    # 試算が失敗したとき
    dlg = CompressDialog(w.doc, w.settings, w)
    real = w.doc.compress_to_advanced
    w.doc.compress_to_advanced = lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
    try:
        dlg._estimate()
    finally:
        w.doc.compress_to_advanced = real
    check("a failed estimate leaves no empty file", estimates() == baseline,
          str(estimates() - baseline))
    # 試算のキャンセル
    w.doc.compress_to_advanced = lambda *a, **k: {"canceled": True}
    try:
        dlg._estimate()
    finally:
        w.doc.compress_to_advanced = real
    check("a cancelled estimate leaves no empty file", estimates() == baseline,
          str(estimates() - baseline))
    dlg.reject()
    w.doc.modified = False
    w._force_quit = True
    w.close()
    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
