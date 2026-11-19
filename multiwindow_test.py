"""タブを新しいウィンドウで開く・1 タブなら確認なしで閉じる・閉じたらフォルダを放す。

- タブが 1 つのときは「ウィンドウを閉じます。どうしますか？」を出さずに閉じる
  （2 つ以上なら従来どおり尋ねる）
- タブを右クリックの「新しいウィンドウで開く」、またはウィンドウの外へドラッグして
  離すと、同じアプリの中の新しいウィンドウで開き直し、元のタブは閉じる
- 未保存の変更があるタブは、保存してから移す（取りやめたら移さない）
- 外へ出したウィンドウは閉じてもアプリを終えない。本当に終えるときは一緒に閉じる
- 前回のファイルの記録には、外へ出したウィンドウの分も入る
- ファイル選択で作業フォルダが動いても、タブを閉じる／ウィンドウに戻ると放す
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 利用者本人の設定（レジストリ）を書き換えないよう、使い捨ての INI で動かす
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import fitz
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from viewer import main_window as mwmod
from viewer.main_window import MainWindow

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}", flush=True)


def make_pdf(path: str, pages: int = 3) -> None:
    d = fitz.open()
    for i in range(pages):
        d.new_page().insert_text((72, 72), f"page {i + 1}")
    d.save(path)
    d.close()


def pump(app, n: int = 15) -> None:
    for _ in range(n):
        app.processEvents()


def send(app, w, et, pt: QPoint, button) -> None:
    app.sendEvent(w, QMouseEvent(et, QPointF(pt), QPointF(w.mapToGlobal(pt)),
                                 button, button if et != QEvent.Type.MouseButtonRelease
                                 else Qt.MouseButton.NoButton,
                                 Qt.KeyboardModifier.NoModifier))
    app.processEvents()


def new_primary(app, paths) -> MainWindow:
    w = MainWindow()
    w.settings.setValue("confirm_close", True)
    w.resize(1000, 700)
    w.move(300, 300)
    w.show()
    pump(app)
    for p in paths:
        w.open_path(p)
    pump(app)
    return w


def tab_paths(w) -> list[str]:
    return [os.path.normcase(w.tabs.widget(i).doc.path) for i in range(w.tabs.count())]


def main() -> int:
    app = QApplication(sys.argv[:1])
    root = tempfile.mkdtemp(prefix="trv_multiwin_")
    # 確認の箱が出たかを数える（offscreen では押す人がいないので、出たら取りやめ扱い）
    asked: list[str] = []
    real_exec = QMessageBox.exec

    def fake_exec(box):
        asked.append(box.text())
        return 0
    QMessageBox.exec = fake_exec
    answers: list = []
    real_question = QMessageBox.question

    def fake_question(*a, **k):
        asked.append(str(a[2]) if len(a) > 2 else "")
        return answers.pop(0) if answers else QMessageBox.StandardButton.Cancel
    QMessageBox.question = staticmethod(fake_question)
    try:
        a = os.path.join(root, "a.pdf")
        b = os.path.join(root, "b.pdf")
        c = os.path.join(root, "c.pdf")
        for p in (a, b, c):
            make_pdf(p)

        # --- 1 タブなら確認なしで閉じる --------------------------------
        w1 = new_primary(app, [a])
        w1.tray = None   # トレイ常駐で隠すのではなく、本当に閉じる場合を見る
        asked.clear()
        closed = w1.close()
        check("1 タブ: 「どうしますか？」を出さずに閉じる", closed and not asked, str(asked))

        w2 = new_primary(app, [a, b])
        w2.tray = None
        asked.clear()
        closed = w2.close()
        check("2 タブ: 従来どおり尋ねる（ここでは取りやめ＝閉じない）",
              not closed and any("どうしますか" in t for t in asked), str(asked))
        w2._force_quit = True
        w2.close()
        pump(app)

        # --- 右クリックの「新しいウィンドウで開く」 -----------------------
        w = new_primary(app, [a, b, c])
        w.tabs.setCurrentIndex(1)
        pump(app)
        w.page_view.set_page(2)
        pump(app)
        new = w.open_tab_in_new_window(1)
        pump(app, 30)
        check("右クリック: 新しいウィンドウができる", new is not None and new._secondary)
        check("…そのウィンドウで同じファイルを開く",
              new is not None and tab_paths(new) == [os.path.normcase(b)],
              str(tab_paths(new) if new else None))
        check("…見ていたページのまま（3 ページ目）",
              new is not None and new.page_view.index == 2,
              str(new.page_view.index if new else None))
        check("…元のウィンドウからはそのタブが消える",
              tab_paths(w) == [os.path.normcase(a), os.path.normcase(c)], str(tab_paths(w)))
        check("…外へ出したウィンドウにはトレイを作らない", new is not None and new.tray is None)
        check("…一覧に入る", new in mwmod.secondary_windows())

        # --- 未保存の変更があるタブ ----------------------------------------
        w.tabs.setCurrentIndex(1)
        pump(app)
        w.doc.modified = True
        answers[:] = [QMessageBox.StandardButton.Cancel]
        asked.clear()
        r = w.open_tab_in_new_window(1)
        check("未保存: 保存するかを尋ね、取りやめたら移さない",
              r is None and w.tabs.count() == 2 and any("保存" in t for t in asked),
              str(asked))
        w.doc.modified = False

        # --- タブが 1 つだけなら移さない ------------------------------------
        check("1 タブ: ウィンドウと同じなので移さない", new.open_tab_in_new_window(0) is None
              and new.tabs.count() == 1)

        # --- ウィンドウの外へドラッグして離す ------------------------------
        bar = w.tabs.tabBar()
        start = bar.tabRect(1).center()
        before = len(mwmod.secondary_windows())
        send(app, bar, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton)
        send(app, bar, QEvent.Type.MouseMove, start + QPoint(10, 40), Qt.MouseButton.LeftButton)
        send(app, bar, QEvent.Type.MouseButtonRelease, start + QPoint(10, 40),
             Qt.MouseButton.LeftButton)
        pump(app, 30)
        check("ドラッグ: ウィンドウの中で離したら並べ替えのまま（増えない）",
              len(mwmod.secondary_windows()) == before and w.tabs.count() == 2)
        outside = QPoint(start.x(), -400)   # ウィンドウの上の外
        send(app, bar, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton)
        send(app, bar, QEvent.Type.MouseMove, outside, Qt.MouseButton.LeftButton)
        send(app, bar, QEvent.Type.MouseButtonRelease, outside, Qt.MouseButton.LeftButton)
        pump(app, 40)
        secs = mwmod.secondary_windows()
        dragged = secs[-1] if len(secs) > before else None
        check("ドラッグ: ウィンドウの外で離したら新しいウィンドウで開く",
              dragged is not None and tab_paths(dragged) == [os.path.normcase(c)],
              str([tab_paths(s) for s in secs]))
        check("…元のウィンドウはタブが 1 つになる", tab_paths(w) == [os.path.normcase(a)],
              str(tab_paths(w)))

        # --- テーマはウィンドウをまたいで揃う --------------------------------
        on = not w.act_dark.isChecked()
        w.act_dark.setChecked(on)
        pump(app)
        check("テーマ: ほかのウィンドウも同じになる",
              all(s.act_dark.isChecked() == on for s in mwmod.secondary_windows()))
        w.act_dark.setChecked(not on)
        pump(app)

        # --- 外のウィンドウを閉じても、ほかは残る ---------------------------
        quit_called: list[bool] = []
        real_quit = QApplication.quit
        QApplication.quit = staticmethod(lambda: quit_called.append(True))
        try:
            ok = dragged.close()
            pump(app)
            check("外のウィンドウを閉じる: 閉じる（アプリは終えない）",
                  ok and not quit_called and dragged not in mwmod.secondary_windows()
                  and w.isVisible())

            # --- 本当に終えるときは、外のウィンドウも閉じ、ファイルを記録する ---
            w.settings.setValue("startup/restore_session", True)
            w.tray = None
            w._force_quit = True
            ok = w.close()
            pump(app)
            files = [os.path.normcase(p) for p in
                     (w.settings.value("session/open_files", [], list) or [])]
            check("終了: 外のウィンドウも閉じる", ok and not mwmod.secondary_windows())
            check("終了: 前回のファイルに外のウィンドウの分も入る",
                  os.path.normcase(a) in files and os.path.normcase(b) in files, str(files))
            check("終了: アプリを終える", bool(quit_called))
        finally:
            QApplication.quit = real_quit

        # --- 閉じたらフォルダを放す（作業フォルダを戻す） -------------------
        folder = os.path.join(root, "書類")
        os.makedirs(folder)
        d = os.path.join(folder, "d.pdf")
        make_pdf(d)
        w3 = new_primary(app, [d, a])
        os.chdir(folder)   # 保存などのファイル選択が作業フォルダを動かしたまね
        w3._close_tab(0)
        pump(app)
        try:
            os.rename(folder, folder + "_moved")
            os.rename(folder + "_moved", folder)
            movable = True
        except OSError as exc:
            movable = f"{exc}"
        check("閉じたあと: そのフォルダの名前を変えられる（使用中にならない）",
              movable is True, str(movable))
        os.chdir(folder)
        w3.isActiveWindow = lambda: True   # offscreen では前面にならないので見なす
        app.sendEvent(w3, QEvent(QEvent.Type.ActivationChange))
        check("ウィンドウに戻る: 作業フォルダを安全な場所へ戻す",
              not os.path.normcase(os.getcwd()).startswith(os.path.normcase(folder)),
              os.getcwd())
        w3._force_quit = True
        QApplication.quit = staticmethod(lambda: None)
        try:
            w3.close()
        finally:
            QApplication.quit = real_quit
        pump(app)
    finally:
        QMessageBox.exec = real_exec
        QMessageBox.question = real_question
        os.chdir(ROOT)
        shutil.rmtree(root, ignore_errors=True)

    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====", flush=True)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
