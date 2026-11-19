"""事務所の外へ何も送らないこと・報告ファイルに PDF の名前を書かないことの回帰テスト。

- 匿名テレメトリの送信先（version.TELEMETRY_URL）は空。設定の telemetry_url で
  送信先を足しても送らない（以前は設定で上書きでき、送信先が入っていた）。
- 「未対応フォームの報告」: 保存の既定のファイル名に PDF の名前を使わない。書き出す
  中身にもファイル名・フォルダ名を書かない（以前は既定名が「<PDF名>_未対応機能.txt」
  で、中身の見出しに「ファイル名: …」があった）。画面の説明も中身と合っている。
通信は差し替えて記録するだけ（外へは出ない）。PDF の名前は試しのダミー。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QWidget  # noqa: E402

from viewer import mw_update, telemetry  # noqa: E402
from viewer import version as ver  # noqa: E402
from viewer.document import PdfDocument  # noqa: E402

TMP = tempfile.mkdtemp(prefix="trv_privacy_")
results: list[bool] = []
PDF = os.path.join(TMP, "顧問先", "株式会社テスト商事", "山田太郎_算定基礎届.pdf")
SECRETS = ("株式会社テスト商事", "山田太郎", "算定基礎届", "顧問先", TMP)


def check(name: str, ok, extra: str = "") -> bool:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    return bool(ok)


class FakeDoc:
    """Document.write_govform_diagnostic（本物）に要るものだけを持つ文書。"""

    is_open = True
    page_count = 2

    def __init__(self, path: str) -> None:
        self.path = path
        self._doc = object()

    def govform_unsupported_helpers(self):
        return ["AFSpecial_KeystrokeEx"]

    def doc_scripts(self):
        return {"main": "function AFSpecial_KeystrokeEx(n) {\n  // 書式\n  return n;\n}\n"}

    write_govform_diagnostic = PdfDocument.write_govform_diagnostic


class Win(mw_update.UpdateMixin, QWidget):
    def __init__(self) -> None:
        QWidget.__init__(self)
        self.settings = QSettings(os.path.join(TMP, "s.ini"), QSettings.Format.IniFormat)
        self.doc = FakeDoc(PDF)
        self._unsupported_helpers = ["AFSpecial_KeystrokeEx"]

    def _last_dir(self) -> str:
        return os.path.dirname(PDF)


def test_telemetry(w: Win) -> None:
    check("the telemetry address is empty (nothing is ever sent)", ver.TELEMETRY_URL == "",
          repr(ver.TELEMETRY_URL)[:60])
    w.settings.setValue("telemetry_url", "https://collector.example/report")
    check("a telemetry address in the settings is ignored", w._telemetry_url() == "",
          repr(w._telemetry_url()))
    sent: list[str] = []
    real = urllib.request.urlopen
    urllib.request.urlopen = lambda req, *a, **k: sent.append(getattr(req, "full_url", req))
    try:
        telemetry.send_async("https://collector.example/report", {"schema": 1})
        time.sleep(0.5)
    finally:
        urllib.request.urlopen = real
    check("sending to an address that is not the built-in one does nothing", not sent, str(sent))


def test_report(w: Win) -> None:
    out = os.path.join(TMP, "report.txt")
    proposed: list[str] = []
    told: list[str] = []
    saved = (QFileDialog.getSaveFileName, QMessageBox.information, QMessageBox.warning)
    QFileDialog.getSaveFileName = staticmethod(
        lambda parent, title, path, *a, **k: (proposed.append(path), (out, ""))[1])
    QMessageBox.information = staticmethod(lambda p, t, x, *a, **k: told.append(x) or 0)
    QMessageBox.warning = staticmethod(lambda p, t, x, *a, **k: told.append(x) or 0)
    try:
        w.report_govform_unsupported()
    finally:
        QFileDialog.getSaveFileName, QMessageBox.information, QMessageBox.warning = saved
    name = os.path.basename(proposed[0]) if proposed else ""
    check("the proposed file name does not contain the PDF's name",
          name and not any(s in name for s in SECRETS), name)
    text = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
    check("the report is written", "AFSpecial_KeystrokeEx" in text, text[:60])
    check("…without the PDF's file name or folder", text and not any(s in text for s in SECRETS),
          str([s for s in SECRETS if s in text]))
    check("…and without a file-name line", "ファイル名:" not in text)
    msg = told[0] if told else ""
    check("the on-screen text says the file name is not included",
          "ファイル名" in msg and "含まれません" in msg, msg[-120:])


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    w = Win()
    try:
        for t in (test_telemetry, test_report):
            try:
                t(w)
            except Exception as exc:  # noqa: BLE001
                check(f"{t.__name__} ran to the end", False, f"{type(exc).__name__}: {exc}")
    finally:
        w.deleteLater()
        app.processEvents()
        shutil.rmtree(TMP, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
