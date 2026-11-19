"""記録に個人情報を残さないことの回帰テスト（viewer/errorlog.py・viewer/rollback.py）。

事務所の PC では、フォルダ名・ファイル名に顧問先の会社名や氏名が入る。以前は
例外の文面とトレースバックをそのまま error.log / startup_error.log に書いていた
（startup_error.log には大きさの上限も無かった）。ここでは
  - ドライブ・UNC のパスは <パス>.拡張子 になり、名前は残らない
  - アプリ自身・site-packages のソースの場所は読めるまま残る
  - マイナンバーのような長い番号は残らない
  - 長い文面は切り詰める。startup_error.log も大きくなったら回す
を確かめる。記録は一時フォルダに書く（本物の記録には触れない）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="trv_logsafe_")
os.environ["LOCALAPPDATA"] = os.path.join(TMP, "localappdata")   # 記録の置き場所
os.makedirs(os.environ["LOCALAPPDATA"], exist_ok=True)

from viewer import errorlog, rollback  # noqa: E402

results: list[bool] = []

# 記録に残ってはいけない言葉（試しのダミー。実在の人・会社ではない）
SECRETS = ("株式会社テスト商事", "山田太郎", "算定基礎届", "職員花子", "sangou-nas", "顧問先",
           "123456789012", "1234-5678-9012")
CLIENT_PDF = r"C:\Users\職員花子\Documents\顧問先\株式会社テスト商事\山田太郎_算定基礎届.pdf"
CLIENT_UNC = r"\\sangou-nas\共有\顧問先\株式会社テスト商事\山田太郎.xlsx"


def check(name: str, ok, extra: str = "") -> bool:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    return bool(ok)


def read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def leaked(text: str) -> list[str]:
    return [s for s in SECRETS if s in text]


def test_errorlog() -> None:
    path = errorlog.log_path()
    check("the error log is written under the test folder", path.startswith(TMP), path)
    if os.path.exists(path):
        os.remove(path)
    # 本物の例外（open が repr でパスを出す）
    try:
        open(CLIENT_PDF, encoding="utf-8")
    except OSError as exc:
        errorlog.log_exception(exc, context=f"開けなかった: {CLIENT_PDF}")
    # 呼び出し側が文面をそのまま渡す場合（mw_print など）
    app_src = os.path.join(ROOT, "viewer", "document.py")
    site = os.path.join(sys.prefix, "Lib", "site-packages", "fitz", "__init__.py")
    errorlog.write(
        "Traceback (most recent call last):\n"
        f'  File "{app_src}", line 10, in open\n'
        f'  File "{site}", line 20, in load\n'
        f"PermissionError: [WinError 5] アクセスが拒否されました。: '{CLIENT_UNC}'\n"
        f"保存先 {CLIENT_PDF} を確認してください\n"
        "値: 123456789012 / 1234-5678-9012\n"
        "長い値: " + "あ" * 3000,
        context="印刷の途中で失敗")
    text = read(path)
    check("nothing of the client paths, names or numbers is left in error.log",
          not leaked(text), str(leaked(text)))
    check("…paths are replaced, keeping only the extension",
          "<パス>.pdf" in text and "<パス>.xlsx" in text)
    check("…the exception and the traceback are still recorded",
          "Traceback" in text and "FileNotFoundError" in text and "PermissionError" in text)
    check("…the app's own source path stays readable (without the user folder)",
          r"<app>\viewer\document.py" in text, "")
    check("…site-packages paths stay readable",
          r"<site-packages>\fitz\__init__.py" in text)
    check("…a very long line is cut short",
          "あ" * 3000 not in text and "文字省略" in text)


def test_unhandled_summary() -> None:
    shown: list[str] = []
    orig = errorlog._notify
    errorlog._notify = lambda path, summary: shown.append(summary)
    try:
        try:
            raise ValueError(f"読めません: {CLIENT_PDF}")
        except ValueError:
            errorlog._hook(*sys.exc_info())
    finally:
        errorlog._notify = orig
    check("the on-screen summary of an unhandled error hides the path too",
          shown and not leaked(shown[0]) and "<パス>.pdf" in shown[0], str(shown)[:120])
    text = read(errorlog.log_path())
    check("…and the log of the unhandled error does as well", not leaked(text))


def test_startup_log() -> None:
    rollback._log(f"TriV-Reader を起動できませんでした。\n"
                  f"Traceback (most recent call last):\n  File \"{CLIENT_PDF}\", line 1\n"
                  f"OSError: '{CLIENT_UNC}' 123456789012")
    path = os.path.join(rollback.state_dir(), "logs", "startup_error.log")
    text = read(path)
    check("startup_error.log is written", "起動できませんでした" in text, path)
    check("…without the client paths, names or numbers", not leaked(text), str(leaked(text)))
    # 大きさの上限（以前は無く、起動の失敗が続くと際限なく大きくなった）
    with open(path, "a", encoding="utf-8") as f:
        f.write("x" * (600 * 1024))
    rollback._log("次の記録")
    size = os.path.getsize(path) if os.path.exists(path) else 0
    check("startup_error.log is rotated when it grows large",
          os.path.exists(path + ".1") and size < 64 * 1024, f"({size} bytes)")


def main() -> int:
    try:
        for t in (test_errorlog, test_unhandled_summary, test_startup_log):
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
