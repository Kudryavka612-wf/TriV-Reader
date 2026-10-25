"""PDF を開いてもフォルダを掴んだままにしないことを確認するテスト。

エクスプローラから PDF をダブルクリックすると、その PDF が入っているフォルダが
プロセスの CWD になる。Windows は CWD にしているフォルダの削除/移動を拒否する
（WinError 32）ため、閲覧しただけでフォルダを整理できなくなっていた。
トレイ常駐でプロセスが残る分、症状も長引く。

「フォルダを使っているアプリを調べる」（viewer/lockfinder.py）が、ファイルを開いて
いる別のアプリと、フォルダを作業フォルダにしている別のアプリを見つけ、上書き保存に
失敗したときの画面からも調べられることも確かめる。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 利用者本人の設定（レジストリ）を書き換えないよう、使い捨ての INI で動かす
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")

import fitz
from PySide6.QtWidgets import QApplication

from viewer import storage
from viewer.document import PdfDocument
from viewer.main_window import MainWindow

ROOT = os.path.dirname(os.path.abspath(__file__))
results: list[bool] = []


def build_sample(path: str, pages: int = 3) -> None:
    doc = fitz.open()
    for n in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 144), f"page {n + 1}", fontsize=24)
    doc.save(path)
    doc.close()


def _try_move(folder: str) -> OSError | None:
    """folder をリネームして元に戻す。掴まれていれば OSError を返す。"""
    dst = folder + "_moved"
    try:
        os.rename(folder, dst)
        os.rename(dst, folder)
        return None
    except OSError as exc:
        return exc


def check_movable(folder: str, label: str, timeout: float = 0.0) -> bool:
    """誰も掴んでいない（＝リネームできる）ことを確認する。

    timeout を指定すると、そうなるまで最大その秒数まで待つ（別プロセスの
    起動完了を待つ用途）。
    """
    deadline = time.monotonic() + timeout
    while True:
        exc = _try_move(folder)
        if exc is None:
            print(f"OK  : {label}")
            results.append(True)
            return True
        if time.monotonic() >= deadline:
            print(f"NG  : {label} -> {exc}")
            results.append(False)
            return False
        time.sleep(0.5)


def check_locked(folder: str, label: str) -> None:
    """逆に「掴まれていて動かせない」ことを確認する（前提条件の検証）。"""
    if _try_move(folder) is None:
        print(f"SKIP: {label}（この環境では CWD ロックが起きない）")
        return
    print(f"OK  : {label}")
    results.append(True)


def make_case(root: str, name: str) -> tuple[str, str]:
    folder = os.path.join(root, name)
    os.makedirs(folder, exist_ok=True)
    pdf = os.path.join(folder, "sample.pdf")
    build_sample(pdf)
    return folder, pdf


def test_document_open(root: str) -> None:
    """PdfDocument で開いてもファイル/フォルダをロックしない。"""
    folder, pdf = make_case(root, "doc_open")
    doc = PdfDocument()
    doc.open(pdf)
    assert doc.page_count == 3, doc.page_count
    check_movable(folder, "PdfDocument で開いた状態でフォルダを移動できる")
    doc.close()


def test_save_and_compress(root: str) -> None:
    """上書き保存・最適化保存の後もフォルダをロックしない。"""
    folder, pdf = make_case(root, "doc_save")
    doc = PdfDocument()
    doc.open(pdf)
    doc.save_as(pdf)  # 自分自身への上書き（一時ファイル経由で置換）
    check_movable(folder, "上書き保存の後にフォルダを移動できる")
    doc.compress_to(pdf)  # 最適化＝自分自身へ上書き
    check_movable(folder, "最適化保存の後にフォルダを移動できる")
    doc.close()


def test_main_window(app: QApplication, root: str) -> None:
    """MainWindow でタブに開いてもフォルダをロックしない。"""
    folder, pdf = make_case(root, "window_open")
    w = MainWindow()
    w.resize(900, 700)
    w.show()
    app.processEvents()
    w.open_path(pdf)
    app.processEvents()
    assert w.doc.page_count == 3, w.doc.page_count
    check_movable(folder, "MainWindow で開いた状態でフォルダを移動できる")
    for i in range(w.tabs.count()):
        t = w.tabs.widget(i)
        if hasattr(t, "doc"):
            t.doc.close()


def test_ensure_safe_cwd(root: str) -> None:
    """CWD がユーザーのフォルダを掴んでいたら安全な場所へ退避する。"""
    folder, _ = make_case(root, "cwd_escape")
    old = os.getcwd()
    try:
        os.chdir(folder)
        check_locked(folder, "（前提）CWD がそのフォルダだと移動できない")
        storage.ensure_safe_cwd()
        check_movable(folder, "ensure_safe_cwd() 後はフォルダを移動できる")
        assert os.path.normcase(os.getcwd()) != os.path.normcase(folder), os.getcwd()
    finally:
        os.chdir(old)


def test_launch_from_folder(root: str) -> None:
    """実際に CWD=そのフォルダ でアプリを起動しても掴んだままにしない。

    エクスプローラのダブルクリック起動と同じ条件。手元で本物のアプリが起動中でも
    その窓に PDF を渡してしまわない（単一インスタンスの待ち受け名は利用者名で決まる
    ので、試験用の名前に変える）。この環境変数を付けないと、起動中の本物のアプリ
    のタブに試験用の PDF が開き、この確認は「別インスタンスが起動中」で省かれていた。
    """
    folder, _ = make_case(root, "launch")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", TRIVREADER_SETTINGS_INI="throwaway",
               USERNAME=f"trv_lock_test_{os.getpid()}")
    for k in ("LOGNAME", "USER", "LNAME"):
        env.pop(k, None)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "main.py"), "sample.pdf"],
        cwd=folder, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(2.0)  # インタプリタと PySide6 の読み込みを待つ
        ok("the app launched from that folder is up", proc.poll() is None,
           f"(exit {proc.poll()})")
        if proc.poll() is not None:
            return
        # 起動途中は CWD を掴んでいて当然なので、退避されるまで待って判定する
        check_movable(folder, "CWD=そのフォルダ で起動してもフォルダを移動できる",
                      timeout=40.0)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def ok(label: str, cond, extra: str = "") -> None:
    print(f"{'OK  ' if cond else 'NG  '}: {label} {extra}")
    results.append(bool(cond))


def test_find_users(app: QApplication, root: str) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox

    from viewer import lockfinder
    from viewer.mw_tools import FolderUsersDialog

    folder = os.path.join(root, "使用中のフォルダ")
    os.makedirs(os.path.join(folder, "sub"))
    build_sample(os.path.join(folder, "申請書.pdf"))
    build_sample(os.path.join(folder, "sub", "控え.pdf"))
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; f = open(sys.argv[1], 'rb'); time.sleep(60)",
         os.path.join(folder, "申請書.pdf")])
    sitter = subprocess.Popen(["cmd", "/c", "ping -n 60 127.0.0.1 >nul"],
                              cwd=os.path.join(folder, "sub"),
                              creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        time.sleep(2)
        result = lockfinder.find(folder)
        by_pid = {f["pid"]: f for f in result["found"]}
        held = by_pid.get(holder.pid)
        ok("finds the app that has a file open, and which file",
           held is not None and any(p.endswith("申請書.pdf") for p in held["files"]),
           str(held))
        ok("finds the app using the folder as its working folder",
           by_pid.get(sitter.pid, {}).get("cwd", "").endswith("sub"), str(by_pid.get(sitter.pid)))
        text = lockfinder.report(result)
        ok("the report names the app, the file and how to free it",
           "申請書.pdf" in text and "cmd.exe" in text and "作業フォルダ" in text
           and "→" in text, text[:200])

        # 画面は固まらない: run() はすぐ戻り、調べている間もイベントが回る
        # （同期で動かしていた版は、ファイル 300 個のフォルダで 5 秒、5000 個で 47 秒、
        #  「応答なし」になった）
        dlg = FolderUsersDialog(folder)
        t0 = time.monotonic()
        dlg.run()
        ok("the lookup runs in the background and the dialog says so",
           dlg.running() and time.monotonic() - t0 < 1.0
           and "調べています" in dlg.text.toPlainText() and not dlg.again.isEnabled())
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(10)
        finished = dlg.wait_until_done()
        timer.stop()
        ok("…and shows the result when it is done",
           finished and "申請書.pdf" in dlg.text.toPlainText() and dlg.again.isEnabled(),
           dlg.text.toPlainText()[:60])
        dlg.deleteLater()

        # 閉じれば止まる。走っている最中に閉じても落ちない
        from viewer import mw_tools
        crowd = os.path.join(root, "多いフォルダ")
        os.makedirs(crowd)
        for i in range(400):
            with open(os.path.join(crowd, f"f{i}.txt"), "w") as fh:
                fh.write("x")
        t0 = time.monotonic()
        stopped = lockfinder.find(crowd, cancelled=lambda: True)
        ok("a lookup that is cancelled returns at once, marked as cancelled",
           stopped["cancelled"] and time.monotonic() - t0 < 5, f"{time.monotonic() - t0:.1f}s")
        dlg = FolderUsersDialog(crowd)
        dlg.run()
        time.sleep(0.3)
        dlg.reject()
        ok("closing the dialog while it looks stops the lookup", not dlg.running())
        for w in list(mw_tools._WORKERS):
            w.wait(60000)
        ok("…and the background thread ends by itself",
           all(w.isFinished() for w in mw_tools._WORKERS))
        dlg.deleteLater()

        # 上書き保存に失敗したときの画面から調べられる
        w = MainWindow()
        w.show()
        app.processEvents()
        w.open_path(os.path.join(folder, "申請書.pdf"))
        app.processEvents()
        w.doc.modified = True
        w.settings.setValue("confirm_overwrite", False)

        def fail(*_a, **_k):
            raise PermissionError("[WinError 32] 別のプロセスが使用中です")
        w.doc.save = fail
        seen = {}

        def click_check():
            box = QApplication.activeModalWidget()
            if isinstance(box, QMessageBox):
                for b in box.buttons():
                    if "使っているアプリ" in b.text():
                        b.click()
                        return
            QTimer.singleShot(100, click_check)

        orig_exec = FolderUsersDialog.exec

        def capture(self):
            self.wait_until_done()   # 調べは別スレッドで動く
            seen["text"] = self.text.toPlainText()
            seen["folder"] = self.folder
            return 0
        FolderUsersDialog.exec = capture
        try:
            QTimer.singleShot(100, click_check)
            w.save()
        finally:
            FolderUsersDialog.exec = orig_exec
        ok("a failed save offers to look up which app is using the folder",
           seen.get("folder") == os.path.normpath(folder) and "申請書.pdf" in seen.get("text", ""),
           str(seen.get("folder")))
        w.doc.modified = False
        w._force_quit = True
        w.close()
    finally:
        holder.kill()
        sitter.kill()
        holder.wait()
        sitter.wait()
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(sitter.pid)],
                       capture_output=True)   # 子の ping も止める


def main() -> int:
    app = QApplication(sys.argv)
    root = tempfile.mkdtemp(prefix="trv_lock_")
    try:
        test_document_open(root)
        test_save_and_compress(root)
        test_main_window(app, root)
        test_ensure_safe_cwd(root)
        test_launch_from_folder(root)
        test_find_users(app, root)
    finally:
        os.chdir(ROOT)
        shutil.rmtree(root, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    raise SystemExit(main())
