"""「前回の続きから開く」とエラーログの検証。

設定はユーザーの実設定を汚さないよう、使い捨ての INI に差し替えて試す。
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

sys.path.insert(0, os.path.dirname(__file__))
from viewer import settings_dialog as prefs, storage

TMP = tempfile.gettempdir()
_INI = os.path.join(TMP, "_session_test.ini")
if os.path.exists(_INI):
    os.remove(_INI)
storage.make_settings = lambda: QSettings(_INI, QSettings.Format.IniFormat)

from viewer import errorlog  # noqa: E402
from viewer.main_window import MainWindow  # noqa: E402


def build(path: str, pages: int = 12) -> None:
    doc = fitz.open()
    for n in range(pages):
        doc.new_page(width=595, height=842).insert_text(
            (72, 100), f"PAGE {n + 1}", fontsize=24)
    doc.save(path)
    doc.close()


def main() -> int:  # noqa: C901
    app = QApplication(sys.argv)
    src = os.path.join(TMP, "_session_src.pdf")
    build(src)

    w = MainWindow()
    w.resize(1000, 780)
    w.show()
    app.processEvents()

    # --- 前回見ていたページから開く -------------------------------------
    w.open_path(src)
    for _ in range(4):
        app.processEvents()
    assert w.page_view.index == 0, w.page_view.index

    w.page_view.set_page(7)
    app.processEvents()
    assert w.page_view.index == 7
    w._close_tab(0)          # 閉じるときに記録される
    app.processEvents()
    assert w.tabs.count() == 0

    rows = w.settings.value("resume/pages", [], list) or []
    assert any(r.startswith("7\t") for r in rows), rows

    # 既定は「たずねる」: 勝手には飛ばず、先頭のまま［続きから表示］を出す
    w.open_path(src)
    for _ in range(8):
        app.processEvents()
    assert w.page_view.index == 0, f"尋ねずに前回位置へ飛んでいる: {w.page_view.index}"
    assert w.page_view.resume_offer_page() == 7, w.page_view.resume_offer_page()
    bar = w.page_view._resume_bar
    assert bar.isVisible() and "8 ページ目" in bar.findChild(QLabel).text()
    vp = w.page_view.viewport().geometry()
    assert vp.contains(bar.geometry()), (bar.geometry(), vp)
    print("OK: 前回のページは尋ねるだけで、勝手に飛ばない")
    QTest.mouseClick(bar.btn_resume, Qt.MouseButton.LeftButton)
    for _ in range(4):
        app.processEvents()
    assert w.page_view.index == 7, f"［続きから表示］で戻らない: {w.page_view.index}"
    assert w.page_view.resume_offer_page() is None, "押した後も案内が残っている"
    assert w.thumbnails.currentRow() in (-1, 7)
    print("OK: ［続きから表示］で閉じたときのページへ戻れる")

    # 「閉じる」なら先頭のまま。案内だけ消える
    w._close_tab(0)
    app.processEvents()
    w.open_path(src)
    for _ in range(6):
        app.processEvents()
    QTest.mouseClick(w.page_view._resume_bar.btn_close, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert w.page_view.index == 0 and w.page_view.resume_offer_page() is None
    print("OK: ［閉じる］なら先頭のまま")
    w.page_view.set_page(7)   # 次の確認のために 8 ページ目で閉じ直す
    app.processEvents()

    # 自動にすれば今までどおり飛ぶ
    w.settings.setValue("startup/resume_mode", "always")
    w._close_tab(0)
    app.processEvents()
    w.open_path(src)
    for _ in range(8):
        app.processEvents()
    assert w.page_view.index == 7, f"自動でも前回のページに戻らない: {w.page_view.index}"
    assert w.page_view.resume_offer_page() is None
    print("OK: 設定「自動で続きから」なら黙って戻る")

    # しないにすれば常に 1 ページ目・案内も出ない
    w.settings.setValue("startup/resume_mode", "never")
    w._close_tab(0)
    app.processEvents()
    w.open_path(src)
    for _ in range(6):
        app.processEvents()
    assert w.page_view.index == 0, "設定を切っても前回位置へ飛んでいる"
    assert w.page_view.resume_offer_page() is None, "設定を切っても案内が出る"
    print("OK: 設定「常に先頭から」で切れる")

    # 旧設定（前回見ていたページから開く = オフ）は「しない」に読み替える
    w.settings.remove("startup/resume_mode")
    w.settings.setValue("startup/resume_page", False)
    assert prefs.resume_mode(w.settings) == "never"
    w.settings.remove("startup/resume_page")
    assert prefs.resume_mode(w.settings) == "ask"
    print("OK: 旧設定のオフを引き継ぐ（無ければ「たずねる」）")

    # 1 ページ目しか見ていないファイルは憶えない（履歴を無駄に増やさない）
    w._close_tab(0)
    app.processEvents()
    rows = w.settings.value("resume/pages", [], list) or []
    assert not any(r.endswith(os.path.normcase(os.path.abspath(src)))
                   and r.startswith("0\t") for r in rows), rows
    print("OK: 1 ページ目のままなら記録しない")

    # 憶えたページが今の文書より後ろでも落ちない（ページを削った場合など）
    w.settings.setValue("resume/pages",
                        [f"999\t{os.path.normcase(os.path.abspath(src))}"])
    w.open_path(src)
    for _ in range(8):
        app.processEvents()
    assert 0 <= w.page_view.index < w.doc.page_count, w.page_view.index
    assert w.page_view.resume_offer_page() == w.doc.page_count - 1, \
        w.page_view.resume_offer_page()
    print(f"OK: 範囲外の記録は末尾に丸める（{w.page_view.resume_offer_page() + 1} ページ目）")

    # --- エラーログ ------------------------------------------------------
    path = errorlog.log_path()
    if os.path.exists(path):
        os.remove(path)
    try:
        raise ValueError("テスト用の失敗")
    except ValueError as exc:
        written = errorlog.log_exception(exc, "session_test")
    assert os.path.exists(written)
    text = open(written, encoding="utf-8").read()
    assert "テスト用の失敗" in text and "Traceback" in text
    assert "TriV-Reader" in text and "PyMuPDF" in text, "版数が記録されていない"
    print("OK: 例外の内容と版数がログに残る")

    # 上限を超えたら 1 世代だけ回す
    with open(written, "w", encoding="utf-8") as f:
        f.write("x" * (errorlog.MAX_BYTES + 10))
    errorlog.write("次の記録")
    assert os.path.exists(written + ".1"), "ログが回っていない"
    assert len(open(written, encoding="utf-8").read()) < errorlog.MAX_BYTES
    os.remove(written + ".1")
    print("OK: ログは大きくなったら 1 世代だけ回す")

    # 未処理例外の受け止めが有効で、フックが自分のものに差し替わっている
    errorlog.install()
    assert sys.excepthook is errorlog._hook
    print("OK: 未処理例外のフックを入れられる")

    # UI から開ける（ログが無いときは案内だけ出して落ちない）
    assert w.act_open_log.isEnabled()
    os.remove(written)
    w._open_error_log()
    assert "エラーログはまだありません" in w.statusBar().currentMessage()
    print("OK: エラーログを開くメニューがある（無いときは案内）")

    w.doc.modified = False
    w._force_quit = True
    w.close()
    for f in (src, _INI):
        if os.path.exists(f):
            os.remove(f)
    print("ALL OK: 前回の続き / エラーログ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
