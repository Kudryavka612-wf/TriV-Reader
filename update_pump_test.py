"""更新の確認・展開が画面を固めないことの確認（MainWindow._pump）。

通信（つながらない PC では最長 10 秒）と 300MB ほどの展開を画面のスレッドで直接
走らせると、その間ずっと「応答なし」になる。別スレッドで走らせ、待ち画面の裏で
イベントを回し続ける。
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QProgressDialog

from viewer.main_window import MainWindow
from viewer.mw_update import _Cancelled

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def main() -> int:
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    app.processEvents()

    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(10)
    t0 = time.monotonic()
    got = w._pump("確認しています…", lambda: (time.sleep(0.6), 42)[1])
    dt = time.monotonic() - t0
    timer.stop()
    check("the result of the slow work comes back", got == 42, f"({dt:.1f}s)")
    check("the screen keeps running while it waits (timers fire)", len(ticks) >= 20,
          f"({len(ticks)} ticks)")

    try:
        w._pump("確認しています…", lambda: (_ for _ in ()).throw(OSError("つながりません")))
        raised = ""
    except OSError as exc:
        raised = str(exc)
    check("an error in the slow work is raised to the caller", raised == "つながりません", raised)

    def cancel_it() -> None:
        for d in app.topLevelWidgets():
            if isinstance(d, QProgressDialog) and d.isVisible():
                d.cancel()
                return
        QTimer.singleShot(50, cancel_it)

    QTimer.singleShot(100, cancel_it)
    t0 = time.monotonic()
    try:
        w._pump("確認しています…", lambda: time.sleep(5))
        cancelled = False
    except _Cancelled:
        cancelled = True
    check("cancelling the wait returns at once", cancelled and time.monotonic() - t0 < 3,
          f"({time.monotonic() - t0:.1f}s)")

    w.doc.modified = False
    w._force_quit = True
    w.close()
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
