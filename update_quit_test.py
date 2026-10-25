"""更新を適用するとき、アプリが本当に終了してから入れ替えることの確認。

以前は QApplication.quit() を呼ぶだけだった。終了は closeEvent を通るので、タブが開いて
いると「ウィンドウを閉じます。どうしますか？」の確認が出て（既定の選択はキャンセル）、
取りやめると終了しない。それでも入れ替え役は 60 秒で待ちきれて、動いている版の上に
新しい版を書いた（実行中の exe は上書きできず、新旧のファイルが混ざった）。

ここでは、入れ替え役の起動（updater.apply_and_restart）を記録に差し替え、
- 終了すると決まってから呼ばれる
- 未保存の変更を取りやめたら、呼ばれず、アプリも終了しない
- トレイ常駐でも終了する
ことを、本物のイベントループで確かめる。シナリオごとに別プロセスで動かす。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def _child(scenario: str) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
    import fitz
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from viewer import updater
    from viewer.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # main.py と同じ（トレイ常駐のため）
    path = os.path.join(tempfile.mkdtemp(prefix="trv_updquit_"), "a.pdf")
    d = fitz.open()
    d.new_page().insert_text((72, 100), "x")
    d.save(path)
    d.close()
    w = MainWindow()
    w.show()
    w.open_path(path)
    app.processEvents()
    if scenario != "clean":
        w.doc.modified = True
    calls: list[str] = []
    updater.apply_and_restart = lambda zip_path, **k: calls.append(zip_path)
    infos: list[str] = []
    QMessageBox.information = staticmethod(lambda *a, **k: infos.append(str(a[2])))
    answers = {"discard": QMessageBox.StandardButton.Discard,
               "cancel": QMessageBox.StandardButton.Cancel}
    QMessageBox.question = staticmethod(
        lambda *a, **k: answers.get(scenario, QMessageBox.StandardButton.Cancel))
    # 確認なしの窓の×でも、トレイ常駐の既定でも、終了できることを見る
    w._tray_resident_enabled = lambda: True
    hung = []

    def give_up() -> None:
        hung.append(True)
        app.exit(0)

    QTimer.singleShot(0, lambda: w._apply_update_and_quit("update.zip", "new_dir"))
    QTimer.singleShot(8000, give_up)
    app.exec()
    print(f"calls={calls} hung={bool(hung)} tabs={w.tabs.count()} infos={len(infos)} "
          f"force_quit={w._force_quit}", flush=True)
    os._exit(0)


if len(sys.argv) > 2 and sys.argv[1] == "--child":
    _child(sys.argv[2])


results: list[bool] = []


def check(name: str, ok, extra: str = "") -> bool:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    return bool(ok)


def run(scenario: str) -> str:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", TRIVREADER_SETTINGS_INI="throwaway",
               PYTHONIOENCODING="utf-8")
    out = subprocess.run([sys.executable, os.path.abspath(__file__), "--child", scenario],
                         capture_output=True, text=True, encoding="utf-8", env=env,
                         timeout=120, cwd=ROOT)
    return [ln for ln in out.stdout.splitlines() if ln.startswith("calls=")][-1:] and \
        [ln for ln in out.stdout.splitlines() if ln.startswith("calls=")][-1] or out.stderr[-400:]


def main() -> int:
    r = run("clean")
    check("a clean window quits for real (even when it lives in the tray), then the "
          "update is applied", "calls=['update.zip']" in r and "hung=False" in r, r)
    r = run("discard")
    check("unsaved changes are asked about; discarding them quits and applies the update",
          "calls=['update.zip']" in r and "hung=False" in r, r)
    r = run("cancel")
    check("cancelling the unsaved-changes question keeps the app, and does not apply",
          "calls=[]" in r and "tabs=1" in r and "infos=1" in r and "force_quit=False" in r, r)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
