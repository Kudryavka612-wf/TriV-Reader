"""更新で入った版が起動できないときに、前の版へ戻す（Windows の配布版だけ）。

自動更新は入れ替える前に、今の版を previous_version フォルダへ控えておく
（updater.apply_and_restart）。新しい版が起動の途中で失敗したら、ここで
「前の版に戻しますか」と聞いて戻す。v1.8.0 は起動直後に落ち、自動更新も
動かないため、利用者がインストーラーを手で入れ直すしかなかった。

起動に失敗しているときは Qt や PyMuPDF そのものが壊れていることがあるので、
このファイルは標準ライブラリと Windows のメッセージボックスだけで動く。

起動の途中で Python ごと落ちる（例外にならない）場合に備え、起動が最後まで
進んだかも記録する。同じ版で 2 回続けて最後まで進まなかったら、戻すかを聞く。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

# 試験用: 聞かずに「はい」と答え、戻した後に起動し直さない
_TEST = os.environ.get("TRIVREADER_TEST_ROLLBACK") == "1"

_MB_OK = 0x0
_MB_YESNO = 0x4
_MB_ICONERROR = 0x10
_MB_ICONWARNING = 0x30
_MB_TOPMOST = 0x40000
_MB_SETFOREGROUND = 0x10000
_IDYES = 6

# 戻すとき・控えるときに触らないもの: 利用者のデータ（ポータブル版の設定など）と、
# インストーラーが置いたアンインストール用のファイル（消すと削除できなくなる）
KEEP_DIRS = ("TriVReader_data",)
KEEP_FILES = ("portable.ini", "lite.ini", "TriVReader.ini", "unins*.*")


def state_dir() -> str:
    """前の版の控えと起動の記録を置く所（この PC の利用者ごと）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "trivreader")
    os.makedirs(d, exist_ok=True)
    return d


def backup_dir() -> str:
    return os.path.join(state_dir(), "previous_version")


def info_path() -> str:
    return os.path.join(state_dir(), "previous_version.json")


def _pending_path() -> str:
    return os.path.join(state_dir(), "startup_pending.json")


def _skip_path() -> str:
    return os.path.join(state_dir(), "skip_update.txt")


def app_dir() -> str:
    return os.path.dirname(os.path.abspath(sys.executable))


def previous() -> dict | None:
    """戻せる前の版の情報（{"version", "app_dir"}）。このアプリの分が無ければ None。"""
    try:
        with open(info_path(), encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    exe = os.path.join(backup_dir(), os.path.basename(sys.executable))
    if (os.path.normcase(os.path.abspath(info.get("app_dir", "")))
            != os.path.normcase(app_dir()) or not os.path.exists(exe)):
        return None
    return info


# --- 見送った版（この PC で起動できなかった版は、自動では勧めない）---------
def skipped_version() -> str:
    try:
        with open(_skip_path(), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def skip_version(version: str) -> None:
    try:
        with open(_skip_path(), "w", encoding="utf-8") as f:
            f.write(version)
    except OSError:
        pass


# --- 起動が最後まで進んだかの記録 -------------------------------------
def begin_startup(version: str) -> bool:
    """起動の始まり（ウィンドウを作る前）に呼ぶ。True なら前の版に戻すので終わること。

    同じ版で前回・前々回と起動が最後まで進まず、別の版の控えがあるなら、
    戻すかを聞く。
    """
    count = 0
    try:
        with open(_pending_path(), encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == version:
            count = int(data.get("count", 0))
    except (OSError, ValueError):
        pass
    if count >= 2:
        prev = previous()
        if prev and prev.get("version") != version:
            if offer(f"TriV-Reader {version} の起動が、続けて {count} 回、最後まで"
                     "進みませんでした。", version, ""):
                startup_done()
                return True
        count = 0   # 戻さないなら数え直す（毎回は聞かない）
    try:
        with open(_pending_path(), "w", encoding="utf-8") as f:
            json.dump({"version": version, "count": count + 1}, f)
    except OSError:
        pass
    return False


def startup_done() -> None:
    """ウィンドウが出て動き始めた（または普通に終わった）ら呼ぶ。"""
    try:
        os.remove(_pending_path())
    except OSError:
        pass


# --- 知らせる・戻す -----------------------------------------------------
def _message(text: str, flags: int) -> int:
    if _TEST:
        return _IDYES
    try:
        import ctypes
        return ctypes.windll.user32.MessageBoxW(
            None, text, "TriV-Reader", flags | _MB_TOPMOST | _MB_SETFOREGROUND)
    except Exception:  # noqa: BLE001
        return 0


def _log(text: str) -> None:
    try:
        d = os.path.join(state_dir(), "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "startup_error.log"), "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n\n")
    except OSError:
        pass


def offer(what: str, version: str, detail: str) -> bool:
    """起動できなかったことを知らせ、前の版があれば戻すかを聞く。戻すなら True。

    戻すときは、このプロセスが終わってから書き戻して起動し直す。呼び出し側は
    True が返ったらすぐ終わること。
    """
    _log(f"{what}\n{detail}")
    last = (detail.strip().splitlines() or [""])[-1][:300]
    tail = f"\n\n（詳しい内容: {last}）" if last else ""
    prev = previous()
    if prev and prev.get("version") != version:
        ans = _message(
            f"{what}\n\n前の版（{prev.get('version')}）に戻しますか？\n"
            "戻すと、更新する前の状態で起動します。この版は自動では勧めなくなります。"
            + tail, _MB_YESNO | _MB_ICONERROR)
        if ans == _IDYES:
            skip_version(version)
            restore_and_restart()
            return True
        return False
    from .version import GITHUB_REPO
    ans = _message(
        f"{what}\n\n最新のインストーラーで入れ直すと直ることがあります。"
        "配布ページを開きますか？" + tail, _MB_YESNO | _MB_ICONWARNING)
    if ans == _IDYES and not _TEST:
        try:
            os.startfile(f"https://github.com/{GITHUB_REPO}/releases/latest")  # noqa: S606
        except OSError:
            pass
    return False


def restore_and_restart(target_dir: str | None = None, exe: str | None = None,
                        wait_pid: int | None = None) -> None:
    """前の版をアプリのフォルダへ書き戻し、起動し直す（このプロセスの終了後に）。

    /MIR で書き戻すのは、壊れた版だけにあるファイルを残さないため（残ると前の版が
    それを読んでしまうことがある）。利用者のデータと設定のファイルは触らない。
    """
    target_dir = target_dir or app_dir()
    exe = exe or sys.executable
    exe_name = os.path.basename(exe)
    pid = os.getpid() if wait_pid is None else wait_pid
    sysdir = r"%SystemRoot%\System32"
    helper = os.path.join(tempfile.gettempdir(), f"trivreader_rollback_{os.getpid()}.cmd")
    keep_dirs = " ".join(f'"{d}"' for d in KEEP_DIRS)
    keep_files = " ".join(f'"{f}"' for f in KEEP_FILES)
    restart = "" if _TEST else f'start "" "{exe}"\n'
    script = f"""@echo off
setlocal enabledelayedexpansion
set _wait=0
:waitloop
{sysdir}\\tasklist.exe /FI "PID eq {pid}" /FI "IMAGENAME eq {exe_name}" 2>nul | {sysdir}\\find.exe /I "{exe_name}" >nul
if errorlevel 1 goto restore
set /a _wait+=1
if !_wait! GEQ 60 goto restore
{sysdir}\\ping.exe -n 2 127.0.0.1 >nul
goto waitloop
:restore
{sysdir}\\robocopy.exe "{backup_dir()}" "{target_dir}" /MIR /NFL /NDL /NJH /NJS /NP /R:5 /W:1 /XD {keep_dirs} /XF {keep_files} >nul
{restart}del "%~f0"
"""
    with open(helper, "w", encoding="utf-8") as f:
        f.write(script)
    subprocess.Popen(["cmd", "/c", helper],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     close_fds=True)
