"""予期しないエラーの記録と通知。

--windowed で固めた exe には標準出力の行き先が無いため、例外が出ても画面にも
ファイルにも何も残らず、「たまに落ちる」を追う手掛かりがゼロになる。ここで
未処理例外を受け止めてログに書き、利用者にも短く知らせる。

ログ: %LOCALAPPDATA%\\trivreader\\logs\\error.log （ポータブル時は
TriVReader_data\\logs）。512KB を超えたら 1 世代だけ error.log.1 へ回す。
"""
from __future__ import annotations

import datetime
import os
import sys
import threading
import time
import traceback

from . import storage

MAX_BYTES = 512 * 1024      # これを超えたら 1 世代だけ回す
MAX_DIALOGS = 3             # この時間内にダイアログを出す上限（連発を防ぐ）
WINDOW_SEC = 600            # 上限を数える時間（秒）。常駐で何日も動くので、累計では数えない

_lock = threading.Lock()
_shown: list[float] = []    # ダイアログを出した時刻（time.monotonic）
_installed = False


def log_dir() -> str:
    d = os.path.join(storage.data_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def log_path() -> str:
    return os.path.join(log_dir(), "error.log")


def _environment() -> str:
    """調査に要る版数をまとめる（報告してもらうときに効く）。"""
    parts = []
    try:
        from .version import APP_VERSION

        parts.append(f"TriV-Reader {APP_VERSION}")
    except Exception:  # noqa: BLE001
        pass
    parts.append(f"Python {sys.version.split()[0]}")
    try:
        import PySide6

        parts.append(f"PySide6 {PySide6.__version__}")
    except Exception:  # noqa: BLE001
        pass
    try:
        import fitz

        parts.append(f"PyMuPDF {fitz.pymupdf_version}")
    except Exception:  # noqa: BLE001
        pass
    parts.append("frozen" if getattr(sys, "frozen", False) else "source")
    return " / ".join(parts)


def _rotate(path: str) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) > MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass  # 回せなくてもログ自体は続ける


def write(text: str, context: str = "") -> str:
    """1 件書き込んでログのパスを返す。書けなくても例外は投げない。"""
    path = log_path()
    _rotate(path)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    head = f"===== {stamp} | {_environment()}"
    if context:
        head += f" | {context}"
    try:
        with _lock, open(path, "a", encoding="utf-8") as f:
            f.write(f"{head} =====\n{text.rstrip()}\n")
    except OSError:
        pass
    return path


def log_exception(exc: BaseException, context: str = "") -> str:
    """捕まえた例外を記録する（処理は続けたいが記録は残したい場面用）。"""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return write(text, context)


def open_log() -> bool:
    """既定のアプリでログを開く。開けたら True。"""
    path = log_path()
    if not os.path.exists(path):
        return False
    try:
        os.startfile(path)  # noqa: S606  Windows 標準の関連付けで開く
        return True
    except Exception:  # noqa: BLE001
        return False


def _notify(path: str, summary: str) -> None:
    """利用者に短く知らせる。GUI が無ければ何もしない。"""
    now = time.monotonic()
    _shown[:] = [t for t in _shown if now - t < WINDOW_SEC]
    if len(_shown) >= MAX_DIALOGS:
        return   # 連発のときだけ止める。累計で数えると、常駐で動き続けるうちに 3 回で
                 # 以後は何も表示されず、ボタンが効かないように見えた
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QMessageBox
    except Exception:  # noqa: BLE001
        return
    app = QApplication.instance()
    if app is None:
        return
    _shown.append(now)

    def show() -> None:
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("エラーが発生しました")
        box.setText("予期しないエラーが発生しました。\n"
                    "作業中の内容は残っていることが多いので、"
                    "まず「別名保存」で保存してください。")
        box.setInformativeText(f"内容をログに記録しました:\n{path}\n\n{summary}")
        open_btn = box.addButton("ログを開く", QMessageBox.ButtonRole.ActionRole)
        box.addButton("閉じる", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            open_log()

    # 例外は描画やイベント処理の途中で飛んでくる。その場でダイアログを出すと
    # 入れ子になって二次被害が出るので、次のループに回す。
    QTimer.singleShot(0, show)


def _hook(etype, value, tb) -> None:
    if issubclass(etype, KeyboardInterrupt):
        sys.__excepthook__(etype, value, tb)
        return
    text = "".join(traceback.format_exception(etype, value, tb))
    path = write(text, "unhandled")
    _notify(path, f"{etype.__name__}: {value}")


def _thread_hook(args) -> None:
    if issubclass(args.exc_type, SystemExit):
        return
    text = "".join(traceback.format_exception(
        args.exc_type, args.exc_value, args.exc_traceback))
    write(text, f"thread:{getattr(args.thread, 'name', '?')}")


def install() -> None:
    """未処理例外の受け止めを有効にする（多重登録はしない）。"""
    global _installed
    if _installed:
        return
    sys.excepthook = _hook
    try:
        threading.excepthook = _thread_hook
    except Exception:  # noqa: BLE001  古い Python には無い
        pass
    _installed = True
