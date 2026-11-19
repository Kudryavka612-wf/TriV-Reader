"""更新の知らせ（v1.12.1）と、リボンの「設定」タブの回帰テスト。

以前は、起動時の確認が既定オフで、オンでも状態バーに 15 秒出るだけだった。
トレイに常駐して何日も起動し直さない PC では、新しい版に誰も気づかなかった。
- 既定オン（以前の版で「オフ」が保存されていても、一度だけオンに戻す）
- 起動の直後と、常駐中は数時間おきに確かめる（最初のウィンドウだけ）
- 画面上端の帯［今すぐ更新］［あとで］と、隠れているときはトレイの吹き出し
- ［あとで］の版は、起動し直すまで知らせない。署名の合わない更新は勧めない
- 設定・更新・ログは「表示」タブではなく「設定」タブにある
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 利用者本人の設定（レジストリ）を書き換えないよう、使い捨ての INI で動かす
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")

from PySide6.QtWidgets import QApplication, QToolButton

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from viewer import updater  # noqa: E402
from viewer.main_window import MainWindow  # noqa: E402

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}", flush=True)


def pump(app, n: int = 20) -> None:
    for _ in range(n):
        app.processEvents()


class FakeTray:
    """トレイの吹き出しを数える身代わり。"""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def showMessage(self, title, text, *args) -> None:  # noqa: N802 (Qt 命名)
        self.messages.append(text)


def run_check(app, w, result=None, rejected=False) -> None:
    """別スレッドの確認を、結果を決めた身代わりで回し、知らせまで進める。"""
    def fake_check(url, timeout=10, include_skipped=False):
        if rejected:
            raise updater.UpdateRejected("署名が合いません")
        return result
    real_check, real_frozen = updater.check, updater.is_frozen
    updater.check = fake_check
    updater.is_frozen = lambda: True
    try:
        w._silent_update_check()   # 待ちは下の quick_await がその場で済ませる
        pump(app, 5)
    finally:
        updater.check, updater.is_frozen = real_check, real_frozen


def ribbon_texts(w, tab_name: str) -> list[str]:
    rb = w.ribbon
    for i in range(rb.count()):
        if rb.tabText(i) == tab_name:
            return [b.defaultAction().text() for b in rb.widget(i).findChildren(QToolButton)
                    if b.defaultAction() is not None]
    return []


def main() -> int:
    app = QApplication(sys.argv[:1])
    from viewer.mw_update import UpdateMixin

    # 待ちのタイマーを 1 秒→すぐに（テストを速くするだけ。中身は同じ流れ）
    real_await = UpdateMixin._await_update_check

    def quick_await(self, box, tries):
        while not box.get("done") and tries < 300:
            import time
            time.sleep(0.01)
            tries += 1
        real_await(self, box, 999)
    UpdateMixin._await_update_check = quick_await

    # --- 既定オン・以前の版の「オフ」を一度だけオンに戻す ------------------------
    w = MainWindow()
    w.settings.setValue("startup/check_update", False)
    w.settings.remove("migrate/update_check_on_v1")
    w2 = MainWindow()
    w2.settings = w.settings
    w2._restore_settings()
    check("以前の版の「オフ」は一度だけオンに戻る",
          w.settings.value("startup/check_update", False, bool) is True)
    w.settings.setValue("startup/check_update", False)
    w2._restore_settings()
    check("…戻すのは一度だけ（そのあと切った人の設定は守る）",
          w.settings.value("startup/check_update", True, bool) is False)
    w.settings.setValue("startup/check_update", True)

    # --- 起動時と常駐中の確認 ------------------------------------------------
    w.check_update_on_startup()
    timer = getattr(w, "_update_poll_timer", None)
    check("常駐中も数時間おきに確かめる（4 時間）",
          timer is not None and timer.isActive() and timer.interval() == 4 * 3600 * 1000)
    sec = MainWindow(secondary=True)
    sec.check_update_on_startup()
    check("外へ出したウィンドウは確かめない（更新は最初のウィンドウが受け持つ）",
          getattr(sec, "_update_poll_timer", None) is None and sec.update_bar is None)
    sec._force_quit = True
    sec.close()

    # --- 知らせ ----------------------------------------------------------------
    w.show()
    pump(app)
    w.tray = FakeTray()
    run_check(app, w, {"version": "9.0.0", "url": "x", "sha256": "0" * 64})
    check("新しい版があれば、画面上端の帯で知らせる",
          w.update_bar.isVisible() and "9.0.0" in w.update_bar.text.text())
    check("…ウィンドウが見えているときは吹き出しを出さない", w.tray.messages == [])
    w._dismiss_update_notice()
    run_check(app, w, {"version": "9.0.0", "url": "x", "sha256": "0" * 64})
    check("［あとで］の版は、次の確認でも知らせない", not w.update_bar.isVisible())
    w.hide()
    run_check(app, w, {"version": "9.1.0", "url": "x", "sha256": "0" * 64})
    check("もっと新しい版が出たら、また知らせる", w.update_bar.isHidden() is False
          or "9.1.0" in w.update_bar.text.text())
    check("…トレイに隠れているときは吹き出しでも知らせる",
          len(w.tray.messages) == 1 and "9.1.0" in w.tray.messages[0], str(w.tray.messages))
    run_check(app, w, {"version": "9.1.0", "url": "x", "sha256": "0" * 64})
    check("…吹き出しは同じ版で 1 回だけ（数時間おきに出し続けない）",
          len(w.tray.messages) == 1, str(w.tray.messages))
    w.show()
    w.update_bar.hide()
    run_check(app, w, rejected=True)
    check("署名の合わない更新は勧めない（帯を出さず、状態バーで知らせる）",
          not w.update_bar.isVisible()
          and updater.REJECT_MESSAGE in w.statusBar().currentMessage())
    w.settings.setValue("startup/check_update", False)
    called = []
    real_check = updater.check
    updater.check = lambda *a, **k: called.append(1)
    try:
        w._silent_update_check()
    finally:
        updater.check = real_check
    check("設定で切った人には確かめない（常駐中のタイマーも設定を見る）", called == [])
    w.settings.setValue("startup/check_update", True)

    # --- リボン: 設定・更新・ログは「設定」タブ ----------------------------------
    view = ribbon_texts(w, "表示")
    conf = ribbon_texts(w, "設定")
    check("「表示」タブには設定・更新・ログが無い",
          not any(t in view for t in ("設定…", "更新を確認…", "エラーログを開く…",
                                     "バージョン情報")), str(view))
    check("「設定」タブにまとまっている",
          all(t in conf for t in ("設定…", "更新を確認…", "エラーログを開く…",
                                  "バージョン情報")), str(conf))
    check("管理者向けの「更新元URL」はリボンに出さない",
          w.act_set_update_url.text() not in conf + view)

    UpdateMixin._await_update_check = real_await
    for win in (w, w2):
        win._force_quit = True
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====", flush=True)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
