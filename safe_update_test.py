"""更新で壊れた版が入っても使い続けられる仕組みの回帰テスト（Windows）。

② 入れ替える前に試す: 新しい版の --selftest が通らなければ入れ替えない。
   試して失敗した版は、自動では勧めない（手動の確認では聞いてから）。
③ 前の版に戻せる: 更新の前に今の版を控え、起動できなければ戻す。戻すときは
   壊れた版だけのファイルを残さず、利用者のデータとアンインストール用の
   ファイルは触らない。起動が 2 回続けて最後まで進まなかったときも戻すかを聞く。

すべて一時フォルダで行う（本物のアプリのフォルダや利用者のデータには触れない）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="trv_safeupd_")
os.environ["LOCALAPPDATA"] = os.path.join(TMP, "localappdata")   # 控え・記録の置き場所
os.makedirs(os.environ["LOCALAPPDATA"], exist_ok=True)

from viewer import rollback, updater  # noqa: E402

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> bool:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    return bool(ok)


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def wait_for(cond, timeout: float = 60.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.3)
    return cond()


def short_lived_pid() -> int:
    """入れ替え役が「アプリの終了」を待つための、すぐ終わるプロセス。"""
    return subprocess.Popen(["cmd", "/c", "exit 0"],
                            creationflags=subprocess.CREATE_NO_WINDOW).pid


def test_trial_run() -> None:
    d = os.path.join(TMP, "trial")
    ok_cmd = os.path.join(d, "ok.cmd")
    write(ok_cmd, "@echo off\r\n(echo render 595x842& echo OK)> %2\r\nexit /b 0\r\n")
    bad_cmd = os.path.join(d, "bad.cmd")
    write(bad_cmd, "@echo off\r\n(echo FileNotFoundError: layout model)> %2\r\nexit /b 1\r\n")
    hang_cmd = os.path.join(d, "hang.cmd")
    write(hang_cmd, "@echo off\r\n%SystemRoot%\\System32\\ping.exe -n 30 127.0.0.1 >nul\r\n")
    ok, detail = updater.trial_run(d, exe=ok_cmd)
    check("a new version whose self-test passes is accepted", ok, repr(detail))
    ok, detail = updater.trial_run(d, exe=bad_cmd)
    check("a new version that fails to start is refused, with the reason",
          not ok and "FileNotFoundError" in detail, repr(detail))
    t0 = time.monotonic()
    ok, detail = updater.trial_run(d, exe=hang_cmd, timeout=2)
    check("a new version that hangs (error dialog) is refused after the time limit",
          not ok and time.monotonic() - t0 < 10, f"({time.monotonic() - t0:.1f}s)")
    ok, _ = updater.trial_run(d, exe=os.path.join(d, "missing.exe"))
    check("a download without the program is refused", not ok)


def test_skip() -> None:
    m = os.path.join(TMP, "update.json")
    write(m, json.dumps({"version": "99.0.0", "url": "file:///nowhere.zip"}))
    url = Path(m).as_uri()
    check("an update is offered", updater.check(url) is not None)
    rollback.skip_version("99.0.0")
    check("a version that failed on this PC is not offered automatically",
          updater.check(url) is None)
    info = updater.check(url, include_skipped=True)
    check("…but a manual check still finds it, marked as skipped",
          info is not None and info.get("skipped") is True)
    write(m, json.dumps({"version": "99.0.1", "url": "file:///nowhere.zip"}))
    check("a newer version is offered again", updater.check(url) is not None)
    os.remove(rollback._skip_path())


def make_app(path: str, version: str) -> None:
    write(os.path.join(path, "TriVReader.exe"), f"exe {version}")
    write(os.path.join(path, "_internal", "lib.txt"), f"lib {version}")


def test_backup_and_restore() -> None:
    app = os.path.join(TMP, "app")
    make_app(app, "old")
    write(os.path.join(app, "TriVReader_data", "user.txt"), "my data")
    write(os.path.join(app, "portable.ini"), "portable")
    write(os.path.join(app, "unins000.exe"), "uninstaller")
    new = os.path.join(TMP, "new", "TriVReader")
    make_app(new, "new")
    write(os.path.join(new, "_internal", "only_in_new.txt"), "broken extra")

    exe = os.path.join(app, "TriVReader.exe")
    updater.apply_and_restart("", extracted=new, app_dir=app, exe_path=exe,
                              wait_pid=short_lived_pid(), restart=False)
    done = wait_for(lambda: read(exe) == "exe new")
    check("the update replaces the program", done)
    backup = rollback.backup_dir()
    check("…after keeping the previous version",
          wait_for(lambda: read(os.path.join(backup, "TriVReader.exe")) == "exe old", 10)
          and read(os.path.join(backup, "_internal", "lib.txt")) == "lib old")
    check("…without copying the user's data or the uninstaller into the backup",
          not os.path.exists(os.path.join(backup, "TriVReader_data"))
          and not os.path.exists(os.path.join(backup, "unins000.exe")))
    info = json.loads(read(rollback.info_path()) or "{}")
    check("…and records which version it is and where it belongs",
          info.get("app_dir") == app and bool(info.get("version")), str(info))
    check("the user's data is untouched by the update",
          read(os.path.join(app, "TriVReader_data", "user.txt")) == "my data"
          and read(os.path.join(app, "portable.ini")) == "portable")

    rollback.restore_and_restart(target_dir=app, exe=exe, wait_pid=short_lived_pid())
    check("going back restores the previous version",
          wait_for(lambda: read(exe) == "exe old")
          and read(os.path.join(app, "_internal", "lib.txt")) == "lib old")
    check("…and leaves nothing of the broken version behind",
          wait_for(lambda: not os.path.exists(
              os.path.join(app, "_internal", "only_in_new.txt")), 10))
    check("…keeping the user's data and the uninstaller",
          read(os.path.join(app, "TriVReader_data", "user.txt")) == "my data"
          and read(os.path.join(app, "portable.ini")) == "portable"
          and read(os.path.join(app, "unins000.exe")) == "uninstaller")


def test_startup_counter() -> None:
    # previous() は「このアプリのフォルダ（＝今の実行ファイルの場所）の控え」だけを見る
    shutil.rmtree(rollback.backup_dir(), ignore_errors=True)
    write(os.path.join(rollback.backup_dir(), os.path.basename(sys.executable)), "old")
    write(rollback.info_path(), json.dumps({"version": "1.0.0", "app_dir": rollback.app_dir()}))
    asked = []
    orig = rollback.offer
    rollback.offer = lambda what, version, detail: asked.append(what) or True
    try:
        rollback.startup_done()
        first = rollback.begin_startup("2.0.0")
        second = rollback.begin_startup("2.0.0")   # 1 回目は最後まで進まなかった
        check("one or two unfinished starts do not ask anything",
              not first and not second and not asked)
        third = rollback.begin_startup("2.0.0")    # 2 回続けて進まなかった
        check("two unfinished starts in a row offer to go back", third and len(asked) == 1,
              str(asked))
        rollback.begin_startup("2.0.0")
        rollback.startup_done()                    # 今度は最後まで進んだ
        asked.clear()
        rollback.begin_startup("2.0.0")
        rollback.begin_startup("2.0.0")
        check("a start that finished resets the count", not asked)
        rollback.startup_done()
        write(rollback.info_path(), json.dumps({"version": "2.0.0",
                                                "app_dir": rollback.app_dir()}))
        for _ in range(4):
            rollback.begin_startup("2.0.0")
        check("nothing is offered when the backup is the same version", not asked)
    finally:
        rollback.offer = orig
        rollback.startup_done()


def main() -> int:
    try:
        test_trial_run()
        test_skip()
        test_backup_and_restore()
        test_startup_counter()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
