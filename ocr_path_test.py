"""OCR の言語データ（tessdata）の置き場所と取得の確認。

- PyMuPDF 内蔵の Tesseract は tessdata のフォルダを ANSI の fopen で開く。利用者名が日本語
  （C:/Users/山田太郎 など）や、日本語のフォルダのポータブル版では、OCR が一切動かなかった
  （「Tesseract language initialisation failed」。利用者には直せない）
- 言語データの取得が途中で切れると、不完全なファイルが最終の名前で残り、以後ずっと OCR が
  失敗した。通信が止まっても待ち続け、固まった
"""
from __future__ import annotations

import http.server
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz

from viewer import ocr

results: list[bool] = []
REAL_ENG = os.path.join(ocr.INSTALL_TESSDATA, "eng.traineddata")


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def serve(handler_cls):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="trv_ocrpath_")
    try:
        # --- 日本語を含む tessdata の場所 --------------------------------------
        if os.path.exists(REAL_ENG):
            page = fitz.open().new_page()
            page.insert_text((72, 100), "Hello world invoice", fontsize=30)
            pix = page.get_pixmap(dpi=150)
            home = os.path.join(tmp, "山田太郎", "tessdata")
            os.makedirs(home)
            shutil.copy(REAL_ENG, os.path.join(home, "eng.traineddata"))
            # 共有の場所（%PUBLIC%）は、この PC を汚さないよう一時フォルダに差し替える
            pub = os.path.join(tmp, "public")
            os.makedirs(pub)
            os.environ["PUBLIC"] = pub
            safe = ocr.ascii_dir(home)
            check("a tessdata folder with Japanese in its path gets an ASCII-only equivalent",
                  safe.isascii() and os.path.isdir(safe), safe)
            shutil.copy(REAL_ENG, os.path.join(safe, "eng.traineddata"))
            try:
                b = pix.pdfocr_tobytes(language="eng", tessdata=safe)
                text = fitz.open("pdf", b)[0].get_text().strip()
            except Exception as exc:  # noqa: BLE001
                text = f"ERROR {exc}"
            check("…and OCR actually runs with it", "Hello" in text, repr(text))

            from viewer import storage
            real_dir = storage.tessdata_dir
            storage.tessdata_dir = lambda: home   # 利用者名が日本語の PC のつもり
            try:
                got = ocr.ensure_languages(["eng"])
            finally:
                storage.tessdata_dir = real_dir
            check("ensure_languages returns an ASCII-only folder that has the language",
                  got.isascii() and os.path.exists(os.path.join(got, "eng.traineddata")), got)
            try:
                b = pix.pdfocr_tobytes(language="eng", tessdata=got)
                text = fitz.open("pdf", b)[0].get_text().strip()
            except Exception as exc:  # noqa: BLE001
                text = f"ERROR {exc}"
            check("…and OCR runs with the folder it returns", "Hello" in text, repr(text))
        else:
            print("SKIP: 手元の Tesseract が無いので、日本語パスでの OCR の確認を省略")

        # --- 取得が途中で切れる ------------------------------------------------
        class Truncated(http.server.BaseHTTPRequestHandler):
            def do_GET(self):   # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "500000")
                self.end_headers()
                self.wfile.write(b"x" * 150000)   # 約束より短いところで切る
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, *a):
                pass

        srv = serve(Truncated)
        target = os.path.join(tmp, "jpn.traineddata")
        try:
            ocr.fetch(f"http://127.0.0.1:{srv.server_port}/x", target, timeout=5)
            raised = False
        except Exception as exc:  # noqa: BLE001
            raised = True
            msg = str(exc)
        left = [f for f in os.listdir(tmp) if f.startswith("jpn")]
        check("a download that is cut off raises, and leaves no partial file behind",
              raised and not left, f"({left})")
        srv.shutdown()

        class Whole(http.server.BaseHTTPRequestHandler):
            def do_GET(self):   # noqa: N802
                body = b"y" * 200000
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = serve(Whole)
        ocr.fetch(f"http://127.0.0.1:{srv.server_port}/x", target, timeout=5)
        check("a complete download is put in place", os.path.getsize(target) == 200000
              and not os.path.exists(target + ".part"))
        srv.shutdown()

        # --- 通信が止まっても待ち続けない --------------------------------------
        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen(5)
        port = silent.getsockname()[1]
        t0 = time.monotonic()
        try:
            ocr.fetch(f"http://127.0.0.1:{port}/x", os.path.join(tmp, "eng2.traineddata"),
                      timeout=1.5)
            raised = False
        except Exception:  # noqa: BLE001
            raised = True
        check("a connection that goes silent gives up after the timeout",
              raised and time.monotonic() - t0 < 6, f"({time.monotonic() - t0:.1f}s)")
        silent.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
