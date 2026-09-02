"""「前回の続きから開く」とエラーログの検証。

設定はユーザーの実設定を汚さないよう、使い捨ての INI に差し替えて試す。
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(__file__))
from viewer import storage

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

    w.open_path(src)
    for _ in range(8):
        app.processEvents()
    assert w.page_view.index == 7, f"前回のページに戻らない: {w.page_view.index}"
    print("OK: 閉じたときのページから開き直せる")

    # 設定を切れば常に 1 ページ目
    w.settings.setValue("startup/resume_page", False)
    w._close_tab(0)
    app.processEvents()
    w.open_path(src)
    for _ in range(6):
        app.processEvents()
    assert w.page_view.index == 0, "設定を切っても前回位置へ飛んでいる"
    w.settings.setValue("startup/resume_page", True)
    print("OK: 設定で切れる")

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
    print(f"OK: 範囲外の記録は末尾に丸める（{w.page_view.index + 1} ページ目）")

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
