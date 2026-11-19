"""配布元の署名つき更新の回帰テスト（viewer/updater.py・viewer/mw_update.py）。

以前は update.json の sha256 も zip も同じリリースから来て、sha256 は任意だった
ので、リリースを書き換えられる人が両方を差し替えれば、どの PC にもコードを配れた
（zip の exe は入れる前の --selftest で実行もされる）。公開鍵が入っているときは、
  - update.json の署名（update.json.sig）が無い・合わない → 入れない（入れるかも聞かない）
  - 更新元が https でない・sha256 が無い・zip が配布元のリリースに無い → 入れない
  - zip が署名された sha256 と合わない → 展開も試しの起動もしない
  - 設定の「更新元URL」で上書きしても、署名は要る。http は使わない
  - ダウンロードが途中で止まっても、いつまでも待たない
を確かめる。通信は偽の応答（urllib の OpenerDirector.open を差し替え）で行い、
外へは出ない。試しの鍵は、その場で作った使い捨て（本物の鍵ではない）。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="trv_signed_")
os.environ["LOCALAPPDATA"] = os.path.join(TMP, "localappdata")   # 見送りの記録などの置き場所
os.makedirs(os.environ["LOCALAPPDATA"], exist_ok=True)

from viewer import ed25519, updater  # noqa: E402
from viewer import version as ver  # noqa: E402

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> bool:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    return bool(ok)


# --- 使い捨ての鍵と、偽の配布元 ---------------------------------------------
SEED = hashlib.sha256(b"trivreader signed_update_test throwaway key").digest()
PUB = base64.b64encode(ed25519.public_key(SEED)).decode("ascii")
OTHER_SEED = hashlib.sha256(b"someone else").digest()
REPO = "Kudryavka612-wf/TriV-Reader"
MANIFEST_URL = f"https://github.com/{REPO}/releases/latest/download/update.json"
ZIP_URL = f"https://github.com/{REPO}/releases/download/v99.0.0/TriVReader-99.0.0.zip"

SERVED: dict[str, bytes] = {}      # URL → 中身（無い URL は 404）
REQUESTED: list[str] = []


class _Resp(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


_real_open = urllib.request.OpenerDirector.open


def _fake_open(self, req, data=None, timeout=None):
    url = req if isinstance(req, str) else req.full_url
    if not url.startswith("https://"):
        return _real_open(self, req, data, timeout) if timeout is not None \
            else _real_open(self, req, data)
    REQUESTED.append(url)
    if url not in SERVED:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    return _Resp(SERVED[url])


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"TriVReader/{os.path.basename(sys.executable)}", "dummy exe")
    return buf.getvalue()


ZIP = make_zip()


def publish(manifest: dict | bytes, sign_with: bytes | None = SEED, sig: bytes | None = None,
            url: str = MANIFEST_URL) -> None:
    """偽の配布元に update.json（と署名）を置く。"""
    SERVED.clear()
    raw = manifest if isinstance(manifest, bytes) else \
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    SERVED[url] = raw
    if sig is not None:
        SERVED[url + ".sig"] = sig
    elif sign_with is not None:
        SERVED[url + ".sig"] = base64.b64encode(ed25519.sign(sign_with, raw)) + b"\n"
    SERVED[ZIP_URL] = ZIP


def good_manifest(**over) -> dict:
    m = {"version": "99.0.0", "url": ZIP_URL, "sha256": hashlib.sha256(ZIP).hexdigest(),
         "notes": "テスト"}
    m.update(over)
    return {k: v for k, v in m.items() if v is not None}


def rejected(fn) -> str:
    """fn() が UpdateRejected を投げたら、その理由。投げなければ ""。"""
    cls = getattr(updater, "UpdateRejected", None)
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        if cls is not None and isinstance(exc, cls):
            return getattr(exc, "reason", "") or "rejected"
        return ""
    return ""


def test_signed_manifest() -> None:
    publish(good_manifest())
    info = updater.check(MANIFEST_URL)
    check("a correctly signed update is offered", info and info.get("version") == "99.0.0",
          str(info))

    publish(good_manifest(), sign_with=None)
    check("an update without a signature (update.json.sig) is refused",
          rejected(lambda: updater.check(MANIFEST_URL)))

    publish(good_manifest(), sign_with=OTHER_SEED)
    check("an update signed with another key is refused",
          rejected(lambda: updater.check(MANIFEST_URL)))

    publish(good_manifest(), sig=b"not base64 !!")
    check("a broken signature is refused", rejected(lambda: updater.check(MANIFEST_URL)))

    # 署名した後で中身を書き換えた（zip の URL と sha256 を差し替える攻撃）
    publish(good_manifest())
    SERVED[MANIFEST_URL] = SERVED[MANIFEST_URL].replace(b"TriVReader-99.0.0.zip", b"TriVReader-99.0.1.zip")
    check("a manifest changed after signing is refused",
          rejected(lambda: updater.check(MANIFEST_URL)))

    publish(good_manifest(sha256=None))
    check("a signed manifest without sha256 is refused",
          rejected(lambda: updater.check(MANIFEST_URL)))

    for bad in ("http://github.com/" + REPO + "/releases/download/v99.0.0/x.zip",
                "https://github.com/evil/TriV-Reader/releases/download/v99.0.0/x.zip",
                "https://example.com/" + REPO + "/releases/download/v99.0.0/x.zip",
                "https://github.com/" + REPO + "/releases/download/../../../evil/x.zip",
                "https://github.com/" + REPO + "/archive/x.zip",
                "file:///C:/x.zip"):
        publish(good_manifest(url=bad))
        check(f"a signed manifest whose zip is not on the release is refused ({bad[:40]}…)",
              rejected(lambda: updater.check(MANIFEST_URL)))

    http_url = MANIFEST_URL.replace("https://", "http://")
    check("an update source that is not https is refused even when signed",
          rejected(lambda: updater.check(http_url)))

    # 同じ版（新しくない）でも、署名が合わなければ「最新です」とは言わず断る
    publish(good_manifest(version=updater.APP_VERSION), sign_with=OTHER_SEED)
    check("the signature is checked before anything in the manifest is trusted",
          rejected(lambda: updater.check(MANIFEST_URL)))

    check("the message for the user says the signature could not be confirmed",
          getattr(updater, "REJECT_MESSAGE", "")
          == "この更新は配布元の署名を確かめられないため入れませんでした。")


def test_download_and_extract() -> None:
    publish(good_manifest())
    info = updater.check(MANIFEST_URL)
    path = updater.download_verified(info)
    check("a zip that matches the signed sha256 is accepted",
          os.path.exists(path) and open(path, "rb").read() == ZIP)
    extracted = updater.extract(path)
    check("…and can be extracted",
          os.path.exists(os.path.join(extracted, os.path.basename(sys.executable))))

    publish(good_manifest(sha256="0" * 64))
    info = updater.check(MANIFEST_URL)
    reason = rejected(lambda: updater.download_verified(info))
    check("a zip that does not match the signed sha256 is refused", reason, reason)
    check("…and the refused download is removed",
          not os.path.exists(os.path.join(tempfile.gettempdir(), "trivreader_update.zip")))

    # 確かめていない zip を展開しない（展開すると、次は中の exe を試しに起動する）
    stray = os.path.join(TMP, "stray.zip")
    with open(stray, "wb") as f:
        f.write(ZIP)
    check("an unverified zip is not extracted", rejected(lambda: updater.extract(stray)))

    # info を後から書き換えても（別の URL）、取りに行かない
    publish(good_manifest())
    info = dict(updater.check(MANIFEST_URL), url="https://example.com/x.zip")
    REQUESTED.clear()
    check("a download outside the release is refused before it is fetched",
          rejected(lambda: updater.download_verified(info))
          and "https://example.com/x.zip" not in REQUESTED)


def test_override_and_bad_key() -> None:
    class Settings:
        def __init__(self, value: str) -> None:
            self._v = value

        def value(self, key, default="", typ=None):
            return self._v if key == "update_url" else default

    default = updater.manifest_url()
    check("an http update URL in the settings is not used",
          updater.manifest_url(Settings("http://192.0.2.1/update.json")) == default)
    check("a local-file update URL in the settings is not used",
          updater.manifest_url(Settings("file:///C:/temp/update.json")) == default)
    mirror = "https://mirror.example/trivreader/update.json"
    check("an https update URL in the settings is used",
          updater.manifest_url(Settings(mirror)) == mirror)
    # 上書きした先でも、配布元の鍵で署名していなければ入れない
    publish(good_manifest(), sign_with=OTHER_SEED, url=mirror)
    check("an overridden update source still needs the distributor's signature",
          rejected(lambda: updater.check(mirror)))
    publish(good_manifest(), url=mirror)
    check("…and is accepted when it serves the same signed files",
          bool(updater.check(mirror)))

    saved = ver.UPDATE_PUBLIC_KEY
    try:
        ver.UPDATE_PUBLIC_KEY = "これは鍵ではない"
        publish(good_manifest())
        check("a malformed public key refuses every update (fails closed)",
              rejected(lambda: updater.check(MANIFEST_URL)))
    finally:
        ver.UPDATE_PUBLIC_KEY = saved


def test_unset_key_behaves_as_before() -> None:
    saved = ver.UPDATE_PUBLIC_KEY
    ver.UPDATE_PUBLIC_KEY = ""
    try:
        zpath = os.path.join(TMP, "legacy.zip")
        with open(zpath, "wb") as f:
            f.write(ZIP)
        mpath = os.path.join(TMP, "legacy.json")
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump({"version": "99.0.0", "url": "file:///" + zpath.replace("\\", "/")}, f)
        info = updater.check("file:///" + mpath.replace("\\", "/"))
        check("without a public key, an unsigned update is offered as before",
              info and info.get("version") == "99.0.0")
    finally:
        ver.UPDATE_PUBLIC_KEY = saved


def test_download_timeout() -> None:
    """止まった接続で、いつまでも待たない（以前は時間の上限が無かった）。"""
    for mode in ("silent", "partial"):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        conns = []

        def serve() -> None:
            try:
                c, _ = srv.accept()
                conns.append(c)
                if mode == "partial":
                    c.recv(4096)
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n" + b"x" * 1000)
                time.sleep(30)   # ここで止まる
            except OSError:
                pass

        threading.Thread(target=serve, daemon=True).start()
        box: dict = {}

        def run() -> None:
            try:
                updater.download(f"http://127.0.0.1:{port}/TriVReader.zip")
                box["result"] = "finished"
            except Exception as exc:  # noqa: BLE001
                box["result"] = type(exc).__name__

        saved = getattr(updater, "DOWNLOAD_TIMEOUT", None)
        updater.DOWNLOAD_TIMEOUT = 1
        t0 = time.monotonic()
        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(10)
        took = time.monotonic() - t0
        if saved is not None:
            updater.DOWNLOAD_TIMEOUT = saved
        check(f"a stalled download gives up instead of hanging ({mode})",
              not th.is_alive() and box.get("result") not in (None, "finished"),
              f"({took:.1f}s, {box.get('result')})")
        for c in conns:
            c.close()
        srv.close()


def test_main_window_flow() -> None:
    """画面の流れ: 断った更新は、入れるかを聞かず、展開も試しの起動もしない。"""
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    from viewer import mw_update

    app = QApplication.instance() or QApplication(sys.argv)

    class Win(mw_update.UpdateMixin, QWidget):
        def __init__(self) -> None:
            QWidget.__init__(self)
            self.settings = QSettings(os.path.join(TMP, "s.ini"), QSettings.Format.IniFormat)

        def _pump(self, text, fn, dialog=None):
            return fn()

    shown: list[tuple[str, str]] = []
    asked: list[str] = []
    calls: list[str] = []
    saved = (QMessageBox.warning, QMessageBox.critical, QMessageBox.question,
             QMessageBox.information, updater.is_frozen, updater.extract, updater.trial_run)
    QMessageBox.warning = staticmethod(lambda p, t, x, *a, **k: shown.append((t, x)) or 0)
    QMessageBox.critical = staticmethod(lambda p, t, x, *a, **k: shown.append((t, x)) or 0)
    QMessageBox.information = staticmethod(lambda p, t, x, *a, **k: shown.append((t, x)) or 0)
    QMessageBox.question = staticmethod(
        lambda p, t, x, *a, **k: asked.append(x) or QMessageBox.StandardButton.Yes)
    updater.is_frozen = lambda: True
    updater.extract = lambda *a, **k: calls.append("extract") or TMP
    updater.trial_run = lambda *a, **k: calls.append("trial_run") or (False, "x")
    try:
        w = Win()
        publish(good_manifest(), sign_with=OTHER_SEED)
        w.check_for_update()
        msg = " ".join(x for _t, x in shown)
        check("[window] a refused update shows the signature message",
              "この更新は配布元の署名を確かめられないため入れませんでした" in msg, msg[:80])
        check("[window] …and does not offer to install it", not asked, str(asked)[:80])

        shown.clear()
        asked.clear()
        publish(good_manifest(sha256="0" * 64))
        w.check_for_update()
        msg = " ".join(x for _t, x in shown)
        check("[window] a download that does not match is refused with the message",
              "この更新は配布元の署名を確かめられないため入れませんでした" in msg, msg[:80])
        check("[window] …and nothing is extracted or started", not calls, str(calls))
        w.deleteLater()
        app.processEvents()
    finally:
        (QMessageBox.warning, QMessageBox.critical, QMessageBox.question,
         QMessageBox.information, updater.is_frozen, updater.extract,
         updater.trial_run) = saved


def main() -> int:
    saved_key = getattr(ver, "UPDATE_PUBLIC_KEY", None)
    saved_repo = ver.GITHUB_REPO
    ver.UPDATE_PUBLIC_KEY = PUB          # 使い捨ての鍵で「署名を確かめる」設定にする
    ver.GITHUB_REPO = REPO
    urllib.request.OpenerDirector.open = _fake_open
    try:
        for t in (test_signed_manifest, test_download_and_extract, test_override_and_bad_key,
                  test_unset_key_behaves_as_before, test_download_timeout, test_main_window_flow):
            try:
                t()
            except Exception as exc:  # noqa: BLE001  1 つが落ちても残りは試す
                check(f"{t.__name__} ran to the end", False, f"{type(exc).__name__}: {exc}")
    finally:
        urllib.request.OpenerDirector.open = _real_open
        ver.GITHUB_REPO = saved_repo
        if saved_key is None:
            del ver.UPDATE_PUBLIC_KEY
        else:
            ver.UPDATE_PUBLIC_KEY = saved_key
        shutil.rmtree(TMP, ignore_errors=True)
        try:
            os.remove(os.path.join(tempfile.gettempdir(), "trivreader_update.zip"))
        except OSError:
            pass
        shutil.rmtree(os.path.join(tempfile.gettempdir(), "trivreader_update_extract"),
                      ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
