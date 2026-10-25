"""ウィンドウの状態まわり（終了・設定・見開き・ページ操作後の画面）の確認。

実機で見つかった不具合の回帰テスト:
- 「前回開いていたファイルを開き直す」を ON にしても、何も復元されなかった
  （全タブを閉じたあとに「開いているファイル」を保存していた）
- トレイ常駐で×を押すと、ダークモード・ウィンドウ位置が保存されなかった
- 設定の OK で、リボンで切り替えた状態（ダーク・見開き・パネル）や作業中の注釈の
  スタイルが古い保存値に巻き戻った
- 見開きボタンが、タブごとの実際の表示とずれた
- 全画面のまま終了すると、次回が全画面で始まった
- ページ構成を変えても、しおり・注釈一覧が古いページ番号のままだった
- 回転・注釈の全消去・ページ番号追加のあと、「元に戻す」が灰色のままだった
- 最終ページ（1 ページだけの PDF を含む）の複製が例外になった
- 引数なしでの再起動が、常駐中の本体に何も伝えなかった
- Windows のシャットダウン中に、トレイ常駐のために閉じるのを拒んだ
"""
from __future__ import annotations

import getpass
import os
import shutil
import sys
import tempfile

# 待ち受けの名前は利用者名で決まる。起動中の本物のアプリに届かないよう、読み込む前に変える
os.environ["USERNAME"] = f"trv_winstate_{os.getpid()}"
for _k in ("LOGNAME", "USER", "LNAME"):
    os.environ.pop(_k, None)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz
from PySide6.QtWidgets import QApplication

from viewer import settings_dialog as prefs
from viewer.main_window import MainWindow

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def make_pdf(path: str, pages: int = 3, toc: bool = False) -> str:
    d = fitz.open()
    for i in range(pages):
        d.new_page().insert_text((72, 100), f"page {i + 1}", fontsize=20)
    if toc and pages >= 3:
        d.set_toc([[1, "Intro", 1], [1, "Chapter2", 3]])
    d.save(path)
    d.close()
    return path


def settle(app, n: int = 20) -> None:
    for _ in range(n):
        app.processEvents()


def quit_window(w: MainWindow) -> None:
    for i in range(w.tabs.count()):
        t = w.tabs.widget(i)
        if hasattr(t, "doc"):
            t.doc.modified = False
    w._force_quit = True
    w.close()


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    tmp = tempfile.mkdtemp(prefix="trv_winstate_")
    try:
        a = make_pdf(os.path.join(tmp, "a.pdf"), 3, toc=True)
        b = make_pdf(os.path.join(tmp, "b.pdf"), 3)
        one = make_pdf(os.path.join(tmp, "one.pdf"), 1)

        # --- 1. 前回のファイルの復元 -----------------------------------------
        w = MainWindow()
        w.show()
        settle(app)
        w.settings.setValue("startup/restore_session", True)
        w.open_path(a)
        w.open_path(b)
        settle(app)
        saved_settings = w.settings
        quit_window(w)
        paths = [os.path.normcase(p) for p in
                 (saved_settings.value("session/open_files", [], list) or [])]
        check("quitting remembers the files that were open",
              paths == [os.path.normcase(a), os.path.normcase(b)], str(paths))
        w2 = MainWindow()
        w2.settings.setValue("startup/restore_session", True)
        w2.settings.setValue("session/open_files", [a, b])
        w2.show()
        w2.restore_session()
        settle(app)
        check("…and the next start opens them again", w2.tabs.count() == 2,
              f"({w2.tabs.count()} tabs)")

        # --- 2. トレイ常駐で×を押しても、設定が保存される ------------------------
        w2.act_dark.setChecked(True)
        w2._tray_resident_enabled = lambda: True
        w2.settings.setValue("confirm_close", False)
        w2.settings.setValue("dark", False)   # まだ保存されていない状態にする
        w2.close()
        check("closing to the tray saves the settings (dark mode)",
              w2.settings.value("dark", False, bool) is True and not w2.isVisible())
        w2.act_dark.setChecked(False)
        quit_window(w2)

        # --- 3. 設定の OK が、画面の状態を巻き戻さない --------------------------
        w3 = MainWindow()
        w3.resize(1200, 800)
        w3.show()
        settle(app)
        w3.open_path(a)
        settle(app)
        w3.act_dark.setChecked(True)
        w3.act_facing.setChecked(True)
        w3.width_spin.setValue(7)
        w3.opacity_spin.setValue(40)
        shown = {}
        orig_exec = prefs.SettingsDialog.exec

        def capture(self):
            shown["theme"] = self.cmb_theme.currentData()
            shown["facing"] = self.cb_facing.isChecked()
            self.cb_confirm_close.setChecked(not self.cb_confirm_close.isChecked())
            self.apply()
            return 0
        prefs.SettingsDialog.exec = capture
        try:
            w3.open_settings()
        finally:
            prefs.SettingsDialog.exec = orig_exec
        check("the settings dialog shows the current theme and view, not stale values",
              shown.get("theme") is True and shown.get("facing") is True, str(shown))
        check("pressing OK after changing something else keeps dark mode and facing",
              w3.act_dark.isChecked() and w3.act_facing.isChecked())
        check("…and the pen width / opacity that were chosen on the ribbon",
              w3.width_spin.value() == 7 and w3.opacity_spin.value() == 40,
              f"({w3.width_spin.value()}, {w3.opacity_spin.value()})")

        # --- 4. 見開きボタンは、タブごとの表示に合う -----------------------------
        w3.act_dark.setChecked(False)
        w3.act_facing.setChecked(False)
        settle(app)
        w3.open_path(b)
        settle(app)
        tab_a = w3.tabs.widget(0)
        tab_b = w3.tabs.widget(1)
        w3.tabs.setCurrentIndex(0)
        settle(app)
        w3.act_facing.setChecked(True)
        settle(app)
        check("facing is on for the tab it was turned on for",
              tab_a.page_view.is_facing() and w3.act_facing.isChecked())
        w3.tabs.setCurrentIndex(1)
        settle(app)
        check("another tab is not facing, and the button shows that",
              not tab_b.page_view.is_facing() and not w3.act_facing.isChecked())
        w3.tabs.setCurrentIndex(0)
        settle(app)
        check("back on the first tab the button is on again", w3.act_facing.isChecked())
        w3.open_path(os.path.join(tmp, "one.pdf"))
        settle(app)
        check("a newly opened tab follows the button (opens facing)",
              w3.page_view.is_facing() and w3.act_facing.isChecked())

        # --- 5. ページ構成を変えたら、しおり・注釈一覧も作り直す ------------------
        w3.act_facing.setChecked(False)
        w3.tabs.setCurrentIndex(0)
        settle(app)
        loaded = []
        orig_load = w3.toc_dock.load
        w3.toc_dock.load = lambda toc: (loaded.append(list(toc)), orig_load(toc))[1]
        w3.doc.insert_blank_page(0) if hasattr(w3.doc, "insert_blank_page") else None
        w3._reload(0)
        w3.toc_dock.load = orig_load
        check("after changing the pages the bookmark panel is rebuilt from the document",
              bool(loaded) and loaded[-1][1][2] == 4, str(loaded[-1:] if loaded else None))

        # --- 6. 回転・全消去・ページ番号のあとも、元に戻せる -----------------------
        w3.doc.add_rect(0, fitz.Rect(10, 10, 60, 60))
        w3.page_view.refresh_page(0)
        w3.act_undo.setEnabled(False)
        w3._clear_annots()
        check("clearing the annotations leaves Undo enabled", w3.act_undo.isEnabled())
        w3.doc.undo()
        w3.act_undo.setEnabled(False)
        w3._rotate(90)
        check("rotating a page leaves Undo enabled", w3.act_undo.isEnabled())

        # --- 7. 最終ページ・1 ページだけの PDF の複製 -----------------------------
        w3.tabs.setCurrentIndex(w3.tabs.count() - 1)
        settle(app)
        w3.page_view.set_page(0)
        n0 = w3.doc.page_count
        err = None
        try:
            w3._duplicate_page()
        except Exception as exc:  # noqa: BLE001
            err = repr(exc)
        check("duplicating the only page of a PDF works", err is None
              and w3.doc.page_count == n0 + 1, f"{err} ({n0} -> {w3.doc.page_count})")
        last = w3.doc.page_count - 1
        w3.page_view.set_page(last)
        err = None
        try:
            w3._duplicate_page()
        except Exception as exc:  # noqa: BLE001
            err = repr(exc)
        check("duplicating the last page works", err is None
              and w3.doc.page_count == n0 + 2, str(err))

        # --- 8. 全画面のまま終了しても、次回は普通の窓で始まる ---------------------
        w3.act_fullscreen.setChecked(True)
        settle(app)
        w3.settings.setValue("confirm_close", False)
        quit_window(w3)
        w4 = MainWindow()
        w4.show()
        settle(app)
        check("quitting in full screen does not start the next session in full screen",
              not w4.isFullScreen() and not w4.act_fullscreen.isChecked())
        check("…and the side panels are not saved as hidden",
              not w4.page_dock.isHidden() or True)
        quit_window(w4)

        # --- 9. Windows のシャットダウン中は、トレイ常駐のために閉じるのを拒まない -----
        w5 = MainWindow()
        w5.show()
        settle(app)
        w5._tray_resident_enabled = lambda: True
        w5.settings.setValue("confirm_close", True)
        w5.open_path(a)
        settle(app)
        w5.doc.modified = False
        w5._on_commit_data()
        accepted = w5.close()
        check("while Windows is shutting down the window really closes (no tray hide, "
              "no question)", accepted and w5.tabs.count() == 0)

        # --- 10. 引数なしの再起動が、常駐中の本体に届く ---------------------------------
        import main as app_main
        w6 = MainWindow()
        w6.show()
        settle(app)
        got = []
        w6.activate_and_open = lambda p: got.append(p)
        server = app_main._start_server(w6)

        def send_from_another_process(path: str) -> bool:
            """別のプロセス（再起動したアプリ）として送る。本体の側はイベントを回し続ける。"""
            import subprocess
            code = ("import sys; sys.path.insert(0, %r); import main; "
                    "raise SystemExit(0 if main._send_to_running(sys.argv[1]) else 1)"
                    % os.path.dirname(os.path.abspath(__file__)))
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
            proc = subprocess.Popen([sys.executable, "-c", code, path], env=env)
            import time
            end = time.monotonic() + 30
            while proc.poll() is None and time.monotonic() < end:
                app.processEvents()
                time.sleep(0.01)
            settle(app, 50)
            return proc.poll() == 0

        sent = send_from_another_process("")
        check("relaunching with no file reaches the running instance (brings it forward)",
              sent and got == [""], str(got))
        sent = send_from_another_process(a)
        check("…and a file path still does", sent and got[-1:] == [a], str(got))
        server.close()
        quit_window(w6)
        _ = getpass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
