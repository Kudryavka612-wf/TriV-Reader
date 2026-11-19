"""自動アップデータ（Windows・onedir 配布向け）。

公開された update.json を取得し、現在版より新しければビルド済み zip を
ダウンロード→展開→この PC で起動を試す→今の版を控える→アプリフォルダを
入れ替え→再起動する。起動を試して失敗した版は入れず、その版は自動では勧めない
（rollback.skip_version）。控えた前の版には、起動できなかったときに戻せる
（rollback.py）。

凍結 exe は自分自身を上書きできないため、アプリ終了後に置換するヘルパー
バッチ(.cmd)をデタッチ起動して実現する。ユーザーデータ（TriVReader_data,
*.ini マーカー）は保持する。

配布元の署名（version.UPDATE_PUBLIC_KEY が入っているとき）:
  update.json と zip は同じ GitHub のリリースから来るので、zip の sha256 を
  update.json に載せるだけでは、リリースを書き換えられる人（漏れたトークン・
  アカウント名を引き継いだ人）が両方を差し替えれば全 PC にコードを配れてしまう。
  そこで update.json そのもの（バイト列）に、配布元だけが持つ秘密鍵で Ed25519 の
  署名を付け（update.json.sig・base64。CI が付ける）、アプリに埋めた公開鍵で
  確かめてから中身を信じる。確かめた update.json の sha256（必須）と zip が
  合ったときだけ、展開・試しの起動（--selftest）・入れ替えに進む。
  公開鍵が空のあいだは、これまでどおりの動き（署名を見ない）。
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from . import ed25519, rollback, storage
from . import version as _ver
from .version import APP_VERSION

# 利用者に見せる文面（署名を確かめられなかったとき）
REJECT_MESSAGE = "この更新は配布元の署名を確かめられないため入れませんでした。"

MANIFEST_MAX_BYTES = 256 * 1024   # update.json の上限（これより大きいものは読まない）
SIGNATURE_MAX_BYTES = 1024        # update.json.sig の上限
DOWNLOAD_TIMEOUT = 30             # 秒。通信がこれだけ途切れたら諦める（止まったまま待たない）
ZIP_MAX_BYTES = 2 * 1024 ** 3     # zip の上限（終わりのない送りつけで一時フォルダを埋めない）

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_VERSION_RE = re.compile(r"^\d{1,6}(\.\d{1,6}){0,3}$")

# 確かめた zip（パス → sha256）と、そこから展開したフォルダ。署名を確かめる設定の
# ときは、確かめていない zip を展開しない（extract を参照）
_verified_zips: dict[str, str] = {}


class UpdateRejected(Exception):
    """配布元の署名・中身を確かめられなかった更新。入れない（入れるかを聞かない）。

    str(exc) は利用者に見せる文面、reason は短い理由（個人情報は含まない）。
    """

    def __init__(self, reason: str, message: str = REJECT_MESSAGE) -> None:
        super().__init__(message)
        self.reason = reason


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_https(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return False
    return parts.scheme.lower() == "https" and bool(parts.hostname)


def signing_enforced() -> bool:
    """公開鍵が入っていれば、署名の無い・合わない更新は入れない。"""
    return bool(str(getattr(_ver, "UPDATE_PUBLIC_KEY", "") or "").strip())


def _public_key() -> bytes | None:
    """埋め込んだ公開鍵（32 バイト）。形が崩れていれば None（＝どの更新も入れない）。"""
    text = str(getattr(_ver, "UPDATE_PUBLIC_KEY", "") or "").strip()
    try:
        key = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    return key if len(key) == ed25519.PUBLIC_KEY_BYTES else None


def verify_manifest(raw: bytes, signature_text: bytes) -> bool:
    """update.json のバイト列 raw に、配布元の署名（base64）が合っていれば True。"""
    key = _public_key()
    if key is None:
        return False
    try:
        sig = base64.b64decode(bytes(signature_text).strip(), validate=True)
    except (binascii.Error, ValueError, TypeError):
        return False
    return ed25519.verify(key, bytes(raw), sig)


def signature_url(url: str) -> str:
    """update.json の URL から、署名（update.json.sig）の URL を作る。"""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path + ".sig", parts.query, ""))


def zip_url_allowed(url: str) -> bool:
    """zip の URL が https://github.com/<GITHUB_REPO>/releases/download/... か。"""
    repo = _ver.GITHUB_REPO.strip().strip("/")
    if not repo or "/" not in repo or not isinstance(url, str) or "\\" in url:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    path = parts.path
    return (parts.scheme == "https" and parts.netloc.lower() == "github.com"
            and not parts.query and not parts.fragment
            and "%" not in path and "/../" not in path + "/" and "/./" not in path + "/"
            and path.lower().startswith(f"/{repo.lower()}/releases/download/")
            and len(path) > len(f"/{repo}/releases/download/"))


def manifest_problem(info: dict) -> str:
    """署名を確かめる設定のとき、update.json の中身に足りない所があれば理由を返す。"""
    if not isinstance(info, dict):
        return "更新の情報の形が正しくありません"
    if not isinstance(info.get("version"), str) or not _VERSION_RE.match(info["version"]):
        return "版の書き方が正しくありません"
    if not isinstance(info.get("sha256"), str) or not _SHA256_RE.match(info["sha256"]):
        return "ファイルの照合値（sha256）がありません"
    if not zip_url_allowed(info.get("url")):
        return "ダウンロード先が配布元のリリースではありません"
    return ""


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """https 以外への転送には付いていかない（GitHub の転送先はどれも https）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise urllib.error.URLError("https 以外へ転送されたため取りやめました")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": "TriVReader-Updater"})
    return urllib.request.build_opener(_HttpsOnlyRedirect).open(req, timeout=timeout)


def _fetch(url: str, timeout: float, limit: int) -> bytes:
    """url の中身を読む（limit バイトを超えるものは読まない）。"""
    with _open(url, timeout) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError("更新の情報が大きすぎます")
    return data


def manifest_url(settings=None) -> str:
    """更新元URLを決める。優先順: 設定の上書き → 直接指定 → GITHUB_REPO 由来。

    設定の上書きは https のときだけ使う（http やファイルの場所は使わない）。上書き
    しても、署名を確かめる設定なら配布元の署名が要るのは変わらない（check）。
    """
    if settings is not None:
        u = str(settings.value("update_url", "", str) or "").strip()
        if u and is_https(u):
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

    署名を確かめる設定（version.UPDATE_PUBLIC_KEY）のときは、中身を読む前に
    update.json のバイト列と署名（url + ".sig"）を確かめる。署名が無い・合わない、
    更新元が https でない、sha256 が無い、zip が配布元のリリースに無い、のどれでも
    UpdateRejected を投げる（更新があるとは言わない）。
    """
    if not url:
        return None
    enforced = signing_enforced()
    if enforced and not is_https(url):
        raise UpdateRejected("更新元が https ではありません")
    raw = _fetch(url, timeout, MANIFEST_MAX_BYTES)
    if enforced:
        try:
            sig = _fetch(signature_url(url), timeout, SIGNATURE_MAX_BYTES)
        except urllib.error.HTTPError as exc:   # 404 など: 署名が公開されていない
            raise UpdateRejected("署名がありません") from exc
        except ValueError as exc:
            raise UpdateRejected("署名の形が正しくありません") from exc
        if not verify_manifest(raw, sig):
            raise UpdateRejected("署名が合いません")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        if enforced:
            raise UpdateRejected("更新の情報の形が正しくありません") from exc
        raise
    if not isinstance(data, dict):
        if enforced:
            raise UpdateRejected("更新の情報の形が正しくありません")
        return None
    if enforced:
        problem = manifest_problem(data)
        if problem:
            raise UpdateRejected(problem)
    if data.get("version") and is_newer(data["version"]):
        if data["version"] == rollback.skipped_version():
            if not include_skipped:
                return None
            data["skipped"] = True
        return data
    return None


def download(url: str, progress=None, timeout: float | None = None) -> str:
    """zip をダウンロードして一時パスを返す。progress(done,total)->bool で中断可。

    通信が timeout 秒（既定 DOWNLOAD_TIMEOUT）途切れたら例外にする。以前は時間の
    上限が無く、止まった接続ではいつまでも待ち続けた。
    """
    dst = os.path.join(tempfile.gettempdir(), "trivreader_update.zip")
    _verified_zips.pop(os.path.normcase(os.path.abspath(dst)), None)
    timeout = DOWNLOAD_TIMEOUT if timeout is None else timeout
    with _open(url, timeout) as resp, open(dst, "wb") as f:
        try:
            total = int(resp.headers.get("Content-Length", 0) or 0)
        except ValueError:
            total = 0
        if total > ZIP_MAX_BYTES:
            raise ValueError("更新のファイルが大きすぎます")
        done = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if done > ZIP_MAX_BYTES:
                raise ValueError("更新のファイルが大きすぎます")
            if progress and not progress(done, total):
                raise RuntimeError("キャンセルされました")
    return dst


def download_verified(info: dict, progress=None, timeout: float | None = None) -> str:
    """check() の結果 info の zip を取り、確かめてから一時パスを返す。

    署名を確かめる設定のときは、署名で確かめた update.json の sha256（必須）と
    合わなければ UpdateRejected（ファイルは消す）。展開も試しの起動も、これを
    通った zip だけにする（extract を参照）。署名を確かめない設定では、これまで
    どおり sha256 があれば照合する。
    """
    enforced = signing_enforced()
    if enforced:
        problem = manifest_problem(info)
        if problem:
            raise UpdateRejected(problem)
    path = download(info["url"], progress=progress, timeout=timeout)
    expected = str(info.get("sha256", "") or "")
    actual = sha256(path)
    ok = (actual.lower() == expected.strip().lower()) if (enforced or expected) else True
    if not ok:
        try:
            os.remove(path)
        except OSError:
            pass
        if enforced:
            raise UpdateRejected("ダウンロードしたファイルが署名された内容と合いません")
        raise UpdateRejected("ダウンロードしたファイルの照合値が合いません",
                             "ダウンロードしたファイルの検証(SHA256)に失敗しました。\n"
                             "更新を中止します。")
    _verified_zips[os.path.normcase(os.path.abspath(path))] = actual.lower()
    return path


def sha256(path: str) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(path: str, expected: str) -> bool:
    """expected が指定されていれば SHA256 を照合（未指定なら True）。

    これまでの照合（sha256 は任意）。更新の流れでは download_verified を使う
    （署名を確かめる設定では sha256 が必須）。
    """
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
    """zip を展開して、新しい版一式のフォルダを返す。

    署名を確かめる設定のときは、download_verified で確かめた zip だけを展開する
    （確かめた後に置き換わっていないかも、もう一度照合する）。
    """
    if signing_enforced():
        key = os.path.normcase(os.path.abspath(zip_path))
        expected = _verified_zips.get(key)
        if not expected or sha256(zip_path).lower() != expected:
            raise UpdateRejected("確かめていないファイルは展開しません")
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
    src_dir = extracted or extract(zip_path)
    app_dir = app_dir or storage.base_dir()
    exe_path = exe_path or sys.executable
    exe_name = os.path.basename(exe_path)
    pid = os.getpid() if wait_pid is None else wait_pid
    helper = os.path.join(tempfile.gettempdir(), "trivreader_update.cmd")

    # 終了待ちは rollback.wait_exit_script（PID と実行ファイル名の組で見る。tasklist
    # そのものが失敗したときは「終わった」とみなさず待ち続け、max_wait 秒で諦める）。
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
{rollback.wait_exit_script(pid, max_wait, "docopy", "aborted")}
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
del "%TRV_TASKS%" 2>nul
del "%~f0"
"""
    rollback.spawn_helper(helper, script, {
        "TRV_TASKS": rollback.tasks_snapshot_path(pid),
        "TRV_EXENAME": exe_name, "TRV_EXE": exe_path, "TRV_APP": os.path.normpath(app_dir),
        "TRV_SRC": os.path.normpath(src_dir), "TRV_BACKUP": backup,
        "TRV_INFO": rollback.info_path(), "TRV_INFO_TMP": info_tmp,
        "TRV_DEST_EXE": dest_exe})
