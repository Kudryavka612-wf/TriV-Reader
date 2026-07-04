"""自動アップデータ（Windows・onedir 配布向け）。

公開された update.json を取得し、現在版より新しければビルド済み zip を
ダウンロード→展開→アプリフォルダを入れ替え→再起動する。

凍結 exe は自分自身を上書きできないため、アプリ終了後に置換するヘルパー
バッチ(.cmd)をデタッチ起動して実現する。ユーザーデータ（TriVReader_data,
*.ini マーカー）は保持する。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.request
import zipfile

from . import storage
from . import version as _ver
from .version import APP_VERSION


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def manifest_url(settings=None) -> str:
    """更新元URLを決める。優先順: 設定の上書き → 直接指定 → GITHUB_REPO 由来。"""
    if settings is not None:
        u = settings.value("update_url", "", str)
        if u:
            return u
    if _ver.UPDATE_MANIFEST_URL:
        return _ver.UPDATE_MANIFEST_URL
    return _ver.github_manifest_url()


def _parse_version(v: str) -> tuple:
    parts = []
    for token in str(v).strip().split("."):
        num = "".join(ch for ch in token if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


def is_newer(remote: str, local: str = APP_VERSION) -> bool:
    return _parse_version(remote) > _parse_version(local)


def check(url: str, timeout: int = 10) -> dict | None:
    """update.json を取得。新しい版があれば dict、無ければ None。"""
    if not url:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "TriVReader-Updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("version") and is_newer(data["version"]):
        return data
    return None


def download(url: str, progress=None) -> str:
    """zip をダウンロードして一時パスを返す。progress(done,total)->bool で中断可。"""
    dst = os.path.join(tempfile.gettempdir(), "trivreader_update.zip")

    def _hook(block, blocksize, total):
        if progress:
            done = block * blocksize
            if not progress(min(done, total) if total > 0 else done, total):
                raise RuntimeError("キャンセルされました")

    req = urllib.request.Request(url, headers={"User-Agent": "TriVReader-Updater"})
    with urllib.request.urlopen(req) as resp, open(dst, "wb") as f:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress and not progress(done, total):
                raise RuntimeError("キャンセルされました")
    return dst


def sha256(path: str) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(path: str, expected: str) -> bool:
    """expected が指定されていれば SHA256 を照合（未指定なら True）。"""
    if not expected:
        return True
    return sha256(path).lower() == expected.strip().lower()


def _extract(zip_path: str) -> str:
    """zip を展開し、exe を含むフォルダ（新バージョン一式）のパスを返す。"""
    out = os.path.join(tempfile.gettempdir(), "trivreader_update_extract")
    if os.path.isdir(out):
        import shutil
        shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(out)
    exe_name = os.path.basename(sys.executable)
    for root, _dirs, files in os.walk(out):
        if exe_name in files:
            return root
    # 見つからなければ展開直下を返す
    return out


def apply_and_restart(zip_path: str) -> None:
    """新バージョンを展開し、終了後に入れ替えるヘルパーを起動してアプリを終了させる。

    呼び出し側は本関数の後に QApplication.quit() すること。
    """
    src_dir = _extract(zip_path)
    app_dir = storage.base_dir()
    exe_path = sys.executable
    exe_name = os.path.basename(exe_path)
    pid = os.getpid()
    helper = os.path.join(tempfile.gettempdir(), "trivreader_update.cmd")

    # 終了待ちは PID だけで判定すると、終了後に別プロセスへ同じ PID が
    # 再利用された場合に「まだ起動中」と誤認し、永久に待ち続けてしまう。
    # IMAGENAME も併せて確認し、万一に備え最大30秒で強制的に先へ進める。
    #
    # robocopy /E: 上書きコピー（/MIR と違い既存のユーザーデータを消さない）
    # /XD: ユーザーデータフォルダ除外、 /XF: マーカー/設定の保持
    # tasklist/find/robocopy/timeout はフルパス指定にする。開発機などで
    # Git for Windows 等の Unix 版コマンド（find 等）が PATH 上で標準の
    # System32 版より手前に来ていると、動作が変わって既定の判定が壊れるため。
    # 待機は timeout ではなく ping を使う。timeout はコンソール/標準入力に依存し、
    # 下の CREATE_NO_WINDOW（コンソール窓なし）起動下では
    # 「Input redirection is not supported」で失敗して待たずに突き進むことがある。
    # ping -n 2 127.0.0.1 は約1秒待ち、コンソールに依存しない。
    sysdir = r"%SystemRoot%\System32"
    script = f"""@echo off
setlocal enabledelayedexpansion
set _wait=0
:waitloop
{sysdir}\\tasklist.exe /FI "PID eq {pid}" /FI "IMAGENAME eq {exe_name}" 2>nul | {sysdir}\\find.exe /I "{exe_name}" >nul
if errorlevel 1 goto docopy
set /a _wait+=1
if !_wait! GEQ 30 goto docopy
{sysdir}\\ping.exe -n 2 127.0.0.1 >nul
goto waitloop
:docopy
{sysdir}\\robocopy.exe "{src_dir}" "{app_dir}" /E /NFL /NDL /NJH /NJS /NP /R:3 /W:1 /XD "TriVReader_data" /XF "portable.ini" "lite.ini" "TriVReader.ini" >nul
start "" "{exe_path}"
del "%~f0"
"""
    with open(helper, "w", encoding="utf-8") as f:
        f.write(script)

    import subprocess
    # コンソール窓を出さずに起動する（更新中に黒いコマンドプロンプトを見せない）。
    # CREATE_NO_WINDOW は「コンソールアプリを窓なしで実行」するフラグで、親（GUI）
    # 終了後もこの子プロセスは生存し、待機→入れ替え→再起動を続ける。
    # 注意: CREATE_NO_WINDOW / CREATE_NEW_CONSOLE / DETACHED_PROCESS はいずれも
    # コンソール生成の指定で相互排他。併用すると CreateProcess が WinError 87
    # （パラメーターが間違っています）になるため、単独で指定する。
    subprocess.Popen(
        ["cmd", "/c", helper],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )
