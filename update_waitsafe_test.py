"""入れ替え役が「アプリが終わった」と取り違えないことの回帰テスト（Windows）。

入れ替え役（.cmd）は tasklist でアプリが終わるのを待つ。以前は
`tasklist … | find` が見つからなければ「終わった」としていたので、tasklist
そのものが失敗すると（WMI の不調・実行の禁止など）、動いているアプリの上に
新しい版（戻しのときは前の版）を書き込んでいた。いまは tasklist が動いたことを
確かめられないときは「まだ動いている」とみなし、待ちきれたら何も書き換えない。

ここでは System32 の一部の写しを作り、入れ替え役に SystemRoot としてそれを渡して、
tasklist が失敗する PC をまねる（写しの上では tasklist が WMI の部品を読めずに終了
コード 1 で失敗する。robocopy などは動く）。すべて一時フォルダで行う。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="trv_waitsafe_")
os.environ["LOCALAPPDATA"] = os.path.join(TMP, "localappdata")   # 控え・記録の置き場所
os.makedirs(os.environ["LOCALAPPDATA"], exist_ok=True)

from viewer import rollback, updater  # noqa: E402

results: list[bool] = []
SYS32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")


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


def fake_root() -> str:
    """System32 の写し（tasklist・find・findstr・robocopy・ping）。

    SystemRoot をここに向けると、tasklist は WMI の部品を読めずに失敗する（終了コード 1、
    「指定されたモジュールが見つかりません」）。robocopy などはそのまま動く。
    """
    root = os.path.join(TMP, "fake_root")
    sys32 = os.path.join(root, "System32")
    os.makedirs(sys32, exist_ok=True)
    for name in ("tasklist.exe", "find.exe", "findstr.exe", "robocopy.exe", "ping.exe"):
        dst = os.path.join(sys32, name)
        if not os.path.exists(dst):
            shutil.copy2(os.path.join(SYS32, name), dst)
    return root


class SystemRoot:
    """入れ替え役（子プロセス）にだけ、別の SystemRoot を渡す。"""

    def __init__(self, root: str) -> None:
        self.root = root

    def __enter__(self):
        self.saved = {k: v for k, v in os.environ.items() if k.upper() == "SYSTEMROOT"}
        for k in self.saved:
            del os.environ[k]
        os.environ["SystemRoot"] = self.root
        return self

    def __exit__(self, *exc):
        for k in [k for k in os.environ if k.upper() == "SYSTEMROOT"]:
            del os.environ[k]
        os.environ.update(self.saved)


def running_app():
    """動き続けるアプリの代わり（cmd.exe。待つ相手の名前を cmd.exe にする）。"""
    return subprocess.Popen(["cmd", "/c", "ping -n 90 127.0.0.1 >nul"],
                            creationflags=subprocess.CREATE_NO_WINDOW)


def stop(proc) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    proc.wait()


def test_update_does_not_overwrite_when_tasklist_fails() -> None:
    root = fake_root()
    app = os.path.join(TMP, "app_upd")
    write(os.path.join(app, "cmd.exe"), "exe old")
    write(os.path.join(app, "_internal", "lib.txt"), "lib old")
    new = os.path.join(TMP, "new_upd")
    write(os.path.join(new, "cmd.exe"), "exe new")
    write(os.path.join(new, "_internal", "lib.txt"), "lib new")
    helper = os.path.join(tempfile.gettempdir(), "trivreader_update.cmd")
    proc = running_app()
    try:
        with SystemRoot(root):
            updater.apply_and_restart("", extracted=new, app_dir=app,
                                      exe_path=os.path.join(app, "cmd.exe"),
                                      wait_pid=proc.pid, restart=False, max_wait=3)
        gone = wait_for(lambda: not os.path.exists(helper), 40)
        check("[update] the helper finishes when tasklist itself fails", gone)
        check("[update] …and does not write over the running app",
              read(os.path.join(app, "_internal", "lib.txt")) == "lib old"
              and read(os.path.join(app, "cmd.exe")) == "exe old",
              read(os.path.join(app, "_internal", "lib.txt")))
    finally:
        stop(proc)


def test_tasklist_really_fails_here() -> None:
    """前提: この写しの上では tasklist が失敗する（そうでなければ、このテストは何も見ていない）。"""
    root = fake_root()
    env = {k: v for k, v in os.environ.items() if k.upper() != "SYSTEMROOT"}
    env["SystemRoot"] = root
    r = subprocess.run([os.path.join(root, "System32", "tasklist.exe"), "/NH", "/FO", "CSV"],
                       env=env, capture_output=True, timeout=60)
    check("[premise] tasklist fails under the copied SystemRoot", r.returncode != 0,
          f"(exit {r.returncode})")


def test_rollback_does_not_overwrite_when_tasklist_fails() -> None:
    root = fake_root()
    app = os.path.join(TMP, "app_rb")
    write(os.path.join(app, "cmd.exe"), "exe broken")
    write(os.path.join(app, "_internal", "lib.txt"), "lib broken")
    write(os.path.join(rollback.backup_dir(), "cmd.exe"), "exe previous")
    write(os.path.join(rollback.backup_dir(), "_internal", "lib.txt"), "lib previous")
    proc = running_app()
    helper = os.path.join(tempfile.gettempdir(), f"trivreader_rollback_{os.getpid()}.cmd")
    import inspect
    # 待つ回数を短くできる版なら短くする（既定は 60 回＝約 1 分）
    kw = {"max_wait": 3} if "max_wait" in inspect.signature(
        rollback.restore_and_restart).parameters else {}
    try:
        with SystemRoot(root):
            rollback.restore_and_restart(target_dir=app, exe=os.path.join(app, "cmd.exe"),
                                         wait_pid=proc.pid, **kw)
        gone = wait_for(lambda: not os.path.exists(helper), 40)
        check("[rollback] the helper finishes when tasklist itself fails", gone)
        check("[rollback] …and does not write over the running app",
              read(os.path.join(app, "_internal", "lib.txt")) == "lib broken",
              read(os.path.join(app, "_internal", "lib.txt")))
    finally:
        stop(proc)


def main() -> int:
    try:
        for t in (test_tasklist_really_fails_here,
                  test_update_does_not_overwrite_when_tasklist_fails,
                  test_rollback_does_not_overwrite_when_tasklist_fails):
            try:
                t()
            except Exception as exc:  # noqa: BLE001
                check(f"{t.__name__} ran to the end", False, f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
