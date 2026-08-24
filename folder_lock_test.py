"""PDF を開いてもフォルダを掴んだままにしないことを確認するテスト。

エクスプローラから PDF をダブルクリックすると、その PDF が入っているフォルダが
プロセスの CWD になる。Windows は CWD にしているフォルダの削除/移動を拒否する
（WinError 32）ため、閲覧しただけでフォルダを整理できなくなっていた。
トレイ常駐でプロセスが残る分、症状も長引く。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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

    エクスプローラのダブルクリック起動と同じ条件。既にアプリが起動中だと
    単一インスタンス機構で即終了してしまうため、その場合はスキップする。
    """
    folder, _ = make_case(root, "launch")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "main.py"), "sample.pdf"],
        cwd=folder, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(2.0)  # インタプリタと PySide6 の読み込みを待つ
        if proc.poll() is not None:
            print("SKIP: 別インスタンスが起動中のため実プロセス検証を省略")
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


def main() -> int:
    app = QApplication(sys.argv)
    root = tempfile.mkdtemp(prefix="trv_lock_")
    try:
        test_document_open(root)
        test_save_and_compress(root)
        test_main_window(app, root)
        test_ensure_safe_cwd(root)
        test_launch_from_folder(root)
    finally:
        os.chdir(ROOT)
        shutil.rmtree(root, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    raise SystemExit(main())
