"""圧縮機能の UI 検証（offscreen で実ウィジェットを操作する）。

リボンのボタンが実際に押せる位置にあるか、ダイアログの各選択肢が
compress_to_advanced に渡す値へ正しく変換されるか、保存まで通るかを見る。
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
import numpy as np
from PIL import Image
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QToolButton

sys.path.insert(0, os.path.dirname(__file__))
from viewer import compress_dialog as cd
from viewer import mw_convert
from viewer.compress_dialog import CompressDialog
from viewer.document import PdfDocument
from viewer.main_window import MainWindow

TMP = tempfile.gettempdir()


def _scan_image(w: int = 1700, h: int = 2200) -> bytes:
    """スキャン原稿らしい画像（白地＋文字状の濃い帯＋読み取りノイズ）の PNG。

    のっぺりした合成画像だと PNG のままの方が小さく JPEG が選ばれないため、
    実際の原稿に近いノイズを載せる。
    """
    rng = np.random.default_rng(7)
    base = np.full((h, w, 3), 245, np.int16)
    for i in range(18):
        y0 = 120 + i * 110
        base[y0:y0 + 30, 150:1500] = 45          # 文字行に見立てた濃い帯
    base[300:900, 900:1600] = rng.integers(60, 200, (600, 700, 3))  # 写真部分
    noise = rng.integers(-12, 13, (h, w, 1))
    img = Image.fromarray(np.clip(base + noise, 0, 255).astype(np.uint8), "RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_scan(path: str, pages: int = 2) -> None:
    """A4 全面に 200dpi 相当のスキャン風画像を貼った PDF。"""
    data = _scan_image()
    doc = fitz.open()
    for n in range(pages):
        p = doc.new_page(width=595, height=842)
        p.insert_image(fitz.Rect(0, 0, 595, 842), stream=data)
        p.insert_text((72, 100), f"PAGE {n + 1} TEXT", fontsize=18)
    doc.save(path, deflate=True)
    doc.close()


def ribbon_button(w: MainWindow, action) -> QToolButton:
    """リボン上でその action に束ねられているボタンを探す。"""
    for btn in w.ribbon.findChildren(QToolButton):
        if btn.defaultAction() is action:
            return btn
    return None


def main() -> int:  # noqa: C901
    app = QApplication(sys.argv)
    src = os.path.join(TMP, "_cmpgui_src.pdf")
    build_scan(src, 2)
    before = os.path.getsize(src)

    w = MainWindow()
    w.resize(1100, 800)
    w.show()
    app.processEvents()
    w.open_path(src)
    app.processEvents()

    # --- リボンから到達できるか（メニューだけだと画面に出ない） --------
    btn = ribbon_button(w, w.act_compress_img)
    assert btn is not None, "リボンに圧縮ボタンが無い"
    page = btn
    while page is not None and w.ribbon.indexOf(page) < 0:
        page = page.parentWidget()
    assert page is not None, "圧縮ボタンがリボンのページに載っていない"
    tab = w.ribbon.indexOf(page)
    assert w.ribbon.tabText(tab) == "保護", w.ribbon.tabText(tab)
    w.ribbon.setCurrentIndex(tab)   # そのタブを開けば実際に見えるか
    app.processEvents()
    assert btn.isVisible(), "圧縮ボタンがリボン上で不可視（親の付け替え漏れ）"
    assert btn.text() == "圧縮", btn.text()
    assert btn.isEnabled(), "文書を開いているのに無効"
    assert btn.width() >= 52 and btn.height() >= 40, (btn.width(), btn.height())
    print(f"OK: リボン「保護」タブに圧縮ボタン（{btn.width()}x{btn.height()}px）")

    # ライト版（Pillow 非同梱）では画像を再圧縮できないので隠す
    from viewer import storage as storage_mod
    orig_lite = storage_mod.is_lite
    storage_mod.is_lite = lambda: True
    try:
        w._apply_lite_mode()
        assert not w.act_compress_img.isVisible(), "ライト版で圧縮ボタンが出ている"
        assert w.act_compress.isVisible(), "無劣化の最適化まで消えている"
    finally:
        storage_mod.is_lite = orig_lite
        w._apply_lite_mode()
        for act in (w.act_compress_img, w.act_ocr, w.act_deskew):
            act.setVisible(True)
        w.setWindowTitle("TriV-Reader")
    assert w.act_compress_img.isVisible() and not w.is_lite
    print("OK: ライト版では圧縮を隠す（無劣化の最適化は残す）")

    # --- ダイアログの選択肢 → 圧縮パラメータ ---------------------------
    st = QSettings("trivreader", "_compress_gui_test")
    st.clear()
    dlg = CompressDialog(w.doc, st, w)
    assert dlg._before == before, (dlg._before, before)

    dlg._radios["standard"].setChecked(True)
    assert dlg.options() == {"max_dpi": 150, "jpeg_quality": 72, "grayscale": False}
    assert not dlg.dpi.isEnabled(), "プリセット選択中にカスタム欄が編集できる"

    dlg._radios["small"].setChecked(True)
    assert dlg.options()["max_dpi"] == 96 and dlg.options()["jpeg_quality"] == 45
    assert dlg.dpi.value() == 96, "表示中の値がプリセットに追従していない"

    dlg._radios["lossless"].setChecked(True)
    assert dlg.options()["max_dpi"] == 0, dlg.options()
    assert not dlg.gray.isEnabled(), "無劣化なのにグレースケールを選べる"

    dlg._radios["custom"].setChecked(True)
    assert dlg.dpi.isEnabled() and dlg.quality.isEnabled()
    dlg.dpi.setValue(120)
    dlg.quality.setValue(60)
    dlg.gray.setChecked(True)
    assert dlg.options() == {"max_dpi": 120, "jpeg_quality": 60, "grayscale": True}
    print("OK: プリセット/カスタムの値が options() に反映される")

    # 設定の保存と復元
    dlg.save_settings()
    dlg2 = CompressDialog(w.doc, st, w)
    assert dlg2.preset() == "custom" and dlg2.dpi.value() == 120
    assert dlg2.quality.value() == 60 and dlg2.gray.isChecked()
    print("OK: 前回の圧縮設定を復元する")

    # --- 試算（ファイルを残さない） -------------------------------------
    dlg2._radios["standard"].setChecked(True)
    dlg2.gray.setChecked(False)
    assert dlg2.result.text() == "", "プリセット変更で前回の試算が消えていない"
    dlg2._estimate()
    app.processEvents()
    text = dlg2.result.text()
    assert "圧縮後" in text and "削減" in text, text
    estimated = re.search(r"<b>(.+?)</b>", text).group(1)  # 例: "38 KB"
    est_tmp = os.path.join(TMP, "_trivreader_estimate.pdf")
    assert not os.path.exists(est_tmp), "試算の一時ファイルが残っている"
    assert os.path.getsize(src) == before, "試算で元ファイルが書き換わった"
    print(f"OK: 試算 -> {text}")

    # --- 実行（保存先ダイアログとメッセージを差し替えて通す） ------------
    out = os.path.join(TMP, "_cmpgui_out.pdf")
    if os.path.exists(out):
        os.remove(out)
    shown = {}
    orig_exec = CompressDialog.exec
    orig_save = mw_convert.QFileDialog.getSaveFileName
    orig_info = mw_convert.QMessageBox.information
    orig_question = mw_convert.QMessageBox.question

    def fake_exec(self):
        self._radios["standard"].setChecked(True)
        self.gray.setChecked(False)
        return QDialog.DialogCode.Accepted

    CompressDialog.exec = fake_exec
    mw_convert.QFileDialog.getSaveFileName = staticmethod(
        lambda *a, **k: (out, "PDF ファイル (*.pdf)"))
    mw_convert.QMessageBox.information = staticmethod(
        lambda parent, title, msg, *a, **k: shown.setdefault("msg", msg)
        or QMessageBox.StandardButton.Ok)
    # 別名保存のあとは「開きますか？」と聞く（question）。ここでは開かない。
    mw_convert.QMessageBox.question = staticmethod(
        lambda parent, title, msg, *a, **k: shown.setdefault("msg", msg)
        or QMessageBox.StandardButton.No)
    try:
        btn.click()  # 実際のリボンボタンから起動する
        app.processEvents()
    finally:
        CompressDialog.exec = orig_exec
        mw_convert.QFileDialog.getSaveFileName = orig_save
        mw_convert.QMessageBox.information = orig_info
        mw_convert.QMessageBox.question = orig_question

    assert os.path.exists(out), "圧縮ファイルが作られていない"
    after = os.path.getsize(out)
    assert after < before * 0.7, (before, after)
    assert "圧縮して保存しました" in shown.get("msg", ""), shown
    assert "点を再圧縮" in shown["msg"], shown["msg"]
    # 試算と同じ設定なので、実行結果も同じサイズになる
    assert estimated in shown["msg"], (estimated, shown["msg"])
    with fitz.open(out) as d:
        assert d.page_count == 2
        assert "PAGE 2 TEXT" in d.load_page(1).get_text()
    # 開いている文書は元のまま（別名保存なので差し替えない）
    assert w.doc.path == src and os.path.getsize(src) == before
    print(f"OK: リボンから圧縮実行 {before:,}B -> {after:,}B / {shown['msg'].splitlines()[-1]}")

    # --- 表示用のサイズ整形 ---------------------------------------------
    assert cd.human_size(900) == "900 B"
    assert cd.human_size(1024 * 1024 * 3 // 2) == "1.5 MB"
    assert "ほとんど縮みません" in cd.CompressDialog.describe(1000, 1200, {})
    assert "すでに十分小さく" in cd.CompressDialog.describe(
        1000, 900, {"images": 3, "recompressed": 0})
    print("OK: サイズ表示と結果メッセージ")

    # --- 一括処理のメニューに圧縮がある ----------------------------------
    doc2 = PdfDocument()
    doc2.open(src)
    stats = doc2.compress_to_advanced(os.path.join(TMP, "_cmpgui_batch.pdf"),
                                      max_dpi=150, jpeg_quality=72)
    doc2.close()
    assert stats["recompressed"] >= 1
    src_mw = open(os.path.join(os.path.dirname(__file__), "viewer", "mw_convert.py"),
                  encoding="utf-8").read()
    assert "圧縮(画像を150dpiに縮小)" in src_mw, "バッチ処理に圧縮が無い"
    print("OK: バッチ処理からも圧縮できる")

    st.clear()
    w._force_quit = True  # 終了確認ダイアログを出さずに閉じる（offscreen で止まる）
    w.close()
    for f in (src, out, os.path.join(TMP, "_cmpgui_batch.pdf")):
        if os.path.exists(f):
            os.remove(f)
    print("ALL OK: 圧縮 UI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
