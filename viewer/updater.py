"""自動アップデータ（Windows・onedir 配布向け）。

公開された update.json を取得し、現在版より新しければビルド済み zip を
ダウンロード→展開→この PC で起動を試す→今の版を控える→アプリフォルダを
入れ替え→再起動する。起動を試して失敗した版は入れず、その版は自動では勧めない
（rollback.skip_version）。控えた前の版には、起動できなかったときに戻せる
（rollback.py）。

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

from . import rollback, storage
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


def check(url: str, timeout: int = 10, include_skipped: bool = False) -> dict | None:
    """update.json を取得。新しい版があれば dict、無ければ None。

    この PC で起動できずに見送った版は、include_skipped でなければ勧めない
    （戻した後に、同じ壊れた版をまた勧めないため）。そのときは "skipped" が入る。
    """
    if not url:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "TriVReader-Updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("version") and is_newer(data["version"]):
        if data["version"] == rollback.skipped_version():
            if not include_skipped:
                return None
            data["skipped"] = True
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


def extract(zip_path: str) -> str:
    """zip を展開して、新しい版一式のフォルダを返す。"""
    return _extract(zip_path)


def trial_run(src_dir: str, timeout: float = 180.0, poll=None,
              exe: str | None = None) -> tuple[bool, str]:
    """入れ替える前に、新しい版がこの PC で起動できるかを試す。(できたか, 内容)。

    新しい版の `--selftest` を走らせる（読み込み・ページの表示・印刷の描画・V8。
    画面は出さない）。poll は待つ間に呼ぶ（画面を止めないため）。False を返すと
    打ち切る。v1.8.0 は配った exe が起動直後に落ちたが、これがあれば入れ替える
    前に止まっていた。
    """
    import subprocess
    import time

    exe = exe or os.path.join(src_dir, os.path.basename(sys.executable))
    if not os.path.exists(exe):
        return False, "新しい版のフォルダに実行ファイルがありません"
    out = os.path.join(tempfile.gettempdir(), f"trivreader_trial_{os.getpid()}.txt")
    try:
        os.remove(out)
    except OSError:
        pass
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", TRIVREADER_SETTINGS_INI="throwaway")
    try:
        proc = subprocess.Popen([exe, "--selftest", out], cwd=tempfile.gettempdir(), env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        return False, f"新しい版を起動できません: {exc}"
    end = time.monotonic() + timeout
    while proc.poll() is None:
        if time.monotonic() > end or (poll is not None and poll() is False):
            proc.kill()
            return False, "新しい版の起動の確認が終わりませんでした（止まっている）"
        time.sleep(0.1)
    try:
        with open(out, encoding="utf-8") as f:
            result = f.read().strip()
    except OSError:
        result = ""
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    ok = proc.returncode == 0 and result.splitlines()[-1:] == ["OK"]
    return ok, result or f"終了コード {proc.returncode}"


def apply_and_restart(zip_path: str, extracted: str | None = None,
                      app_dir: str | None = None, exe_path: str | None = None,
                      wait_pid: int | None = None, restart: bool = True,
                      max_wait: int = 60) -> None:
    """新バージョンを展開し、終了後に入れ替えるヘルパーを起動する。

    入れ替える前に、今の版を rollback.backup_dir() へ控える（起動できない版が
    入っても戻せるように）。extracted は展開済みのフォルダ（trial_run で試した物）。
    呼び出し側は、アプリが本当に終わると決まってから（未保存の確認を済ませ、
    トレイに隠れるだけにせず）これを呼ぶこと。max_wait 秒たってもアプリが終わら
    ないときは、動いたままの版を上書きして壊さないよう、何もせずに諦める。
    """
    src_dir = extracted or _extract(zip_path)
    app_dir = app_dir or storage.base_dir()
    exe_path = exe_path or sys.executable
    exe_name = os.path.basename(exe_path)
    pid = os.getpid() if wait_pid is None else wait_pid
    helper = os.path.join(tempfile.gettempdir(), "trivreader_update.cmd")

    # 終了待ちは PID だけで判定すると、終了後に別プロセスへ同じ PID が
    # 再利用された場合に「まだ起動中」と誤認し、永久に待ち続けてしまう。
    # IMAGENAME も併せて確認し、万一に備え最大60秒で強制的に先へ進める。
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
    #
    # 重要: exe 自身は実行中に上書きしようとすると（ウイルス対策ソフトの
    # スキャン等で）ごく短時間ロックされ、robocopy がその瞬間だけ exe の
    # コピーに失敗することがある。他のファイルは成功するため見た目上は
    # 「更新完了」に見えるが、実際は古い exe のまま再起動してしまい、
    # 次回の更新確認でも同じことを繰り返す（更新できているように見えて
    # 実際には何度やっても反映されない）。これを防ぐため、コピー後に
    # exe のサイズを比較し、一致するまで robocopy を最大5回リトライする。
    sysdir = r"%SystemRoot%\System32"
    # 今の版の控え。控えが最後まで取れたときだけ「戻せる」と記録する
    backup = rollback.backup_dir()
    info_tmp = os.path.join(tempfile.gettempdir(), "trivreader_previous.json")
    with open(info_tmp, "w", encoding="utf-8") as f:
        json.dump({"version": APP_VERSION, "app_dir": app_dir}, f, ensure_ascii=False)
    keep_dirs = " ".join(f'"{d}"' for d in rollback.KEEP_DIRS)
    keep_files = " ".join(f'"{x}"' for x in rollback.KEEP_FILES)
    new_exe = os.path.join(src_dir, exe_name)
    expected_size = os.path.getsize(new_exe) if os.path.exists(new_exe) else -1
    dest_exe = os.path.join(app_dir, exe_name)
    relaunch = 'start "" "%TRV_EXE%"' if restart else "rem no relaunch (test)"
    # パスはスクリプトに埋めず環境変数で渡す（rollback.spawn_helper を参照: 日本語の
    # パスが cmd で化ける）。遅延展開（!変数!）も使わない: パスに ! があると消える。
    # ループは行ごとに読まれるので、%変数% のままで毎回の値になる
    script = f"""@echo off
set _wait=0
:waitloop
{sysdir}\\tasklist.exe /FI "PID eq {pid}" /FI "IMAGENAME eq %TRV_EXENAME%" 2>nul | {sysdir}\\find.exe /I "%TRV_EXENAME%" >nul
if errorlevel 1 goto docopy
set /a _wait+=1
if %_wait% GEQ {max_wait} goto aborted
{sysdir}\\ping.exe -n 2 127.0.0.1 >nul
goto waitloop
:docopy
del "%TRV_INFO%" 2>nul
{sysdir}\\robocopy.exe "%TRV_APP%" "%TRV_BACKUP%" /MIR /NFL /NDL /NJH /NJS /NP /R:2 /W:1 /XD {keep_dirs} /XF {keep_files} >nul
if errorlevel 8 goto backup_done
copy /Y "%TRV_INFO_TMP%" "%TRV_INFO%" >nul
:backup_done
set _copytry=0
:copyloop
{sysdir}\\robocopy.exe "%TRV_SRC%" "%TRV_APP%" /E /NFL /NDL /NJH /NJS /NP /R:5 /W:1 /XD "TriVReader_data" /XF "portable.ini" "lite.ini" "TriVReader.ini" >nul
set _sz=
for %%A in ("%TRV_DEST_EXE%") do set _sz=%%~zA
if "%_sz%"=="{expected_size}" goto docopy_done
set /a _copytry+=1
if %_copytry% GEQ 5 goto docopy_done
{sysdir}\\ping.exe -n 3 127.0.0.1 >nul
goto copyloop
:docopy_done
{relaunch}
:aborted
del "%~f0"
"""
    rollback.spawn_helper(helper, script, {
        "TRV_EXENAME": exe_name, "TRV_EXE": exe_path, "TRV_APP": os.path.normpath(app_dir),
        "TRV_SRC": os.path.normpath(src_dir), "TRV_BACKUP": backup,
        "TRV_INFO": rollback.info_path(), "TRV_INFO_TMP": info_tmp,
        "TRV_DEST_EXE": dest_exe})
