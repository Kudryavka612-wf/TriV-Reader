"""PDF を複数選んで同時に起動しても、ウィンドウは 1 つで全部のタブが開くことの確認。

エクスプローラーで PDF を複数選んで Enter すると、選んだ数のプロセスがほぼ同時に
起動する。「起動中のインスタンスに接続できなければ自分が主」だけだと、先頭が待ち
受けを始める前に全員が接続に失敗し、全員が主になっていた（ウィンドウもトレイの
アイコンも選んだ数だけ出て、PDF は最後に待ち受けを始めた 1 つにしか届かない）。

本物の起動（main.main）を子プロセスで動かし、開いた PDF を記録して確かめる。
利用者名を変えて待ち受けの名前を分けるので、手元で起動中の本物のアプリには届かない。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def _child(log: str, pdf: str) -> None:
    """子プロセス: 本物の main() を動かし、開いた PDF だけ記録する。"""
    sys.argv = [sys.argv[0], pdf]   # main() は argv[1] を開く PDF として読む
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
    import main as app_main
    from viewer.main_window import MainWindow

    def _record(self, path, *a, **k):   # noqa: ARG001
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\t{os.path.basename(path)}\n")
        return True

    MainWindow.open_path = _record
    raise SystemExit(app_main.main())


if len(sys.argv) > 3 and sys.argv[1] == "--child":
    _child(sys.argv[2], sys.argv[3])
    raise SystemExit(0)


results: list[bool] = []


def check(name: str, ok, extra: str = "") -> bool:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    return bool(ok)


def wait_for(cond, timeout: float = 30.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.2)
    return cond()


def main() -> int:
    import fitz

    tmp = tempfile.mkdtemp(prefix="trv_single_")
    log = os.path.join(tmp, "opened.log")
    names = [f"文書{i}.pdf" for i in range(4)]
    for n in names:
        d = fitz.open()
        d.new_page().insert_text((72, 100), n, fontsize=20, fontname="japan")
        d.save(os.path.join(tmp, n))
        d.close()
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", TRIVREADER_SETTINGS_INI="throwaway",
               PYTHONIOENCODING="utf-8", USERNAME=f"trv_single_{os.getpid()}")
    for k in ("LOGNAME", "USER", "LNAME"):
        env.pop(k, None)

    def launch(name: str) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, os.path.abspath(__file__), "--child", log,
                                 os.path.join(tmp, name)], env=env, cwd=ROOT)

    def opened() -> list[str]:
        try:
            with open(log, encoding="utf-8") as f:
                return [ln.rstrip("\n").split("\t")[1] for ln in f if "\t" in ln]
        except OSError:
            return []

    procs: list[subprocess.Popen] = []
    try:
        # 3 つをほぼ同時に（エクスプローラーで 3 つ選んで Enter）
        procs = [launch(n) for n in names[:3]]
        got = wait_for(lambda: sorted(opened()) == sorted(names[:3]), 40)
        check("all three PDFs open, each exactly once", got, str(opened()))
        wait_for(lambda: sum(p.poll() is None for p in procs) == 1, 15)
        alive = [p for p in procs if p.poll() is None]
        check("only one instance stays running", len(alive) == 1,
              f"({len(alive)} running)")
        check("the others hand over and exit normally",
              all(p.returncode == 0 for p in procs if p.poll() is not None))

        # 起動済みの主へ、あとから開くのは従来どおり渡る
        late = launch(names[3])
        procs.append(late)
        check("a later launch hands its PDF to the running instance",
              wait_for(lambda: names[3] in opened(), 20) and wait_for(
                  lambda: late.poll() == 0, 10))

        # 主が終わったら、次の起動が新しい主になる（印が残らない）
        for p in alive:
            p.kill()
            p.wait(10)
        again = launch(names[0])
        procs.append(again)
        check("after the main instance is gone, the next launch becomes the main one",
              wait_for(lambda: opened().count(names[0]) == 2, 30)
              and again.poll() is None)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                pass
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
