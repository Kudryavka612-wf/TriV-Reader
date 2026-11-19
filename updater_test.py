"""自動アップデータのロジック検証（取得・比較・DL・展開）。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from viewer import updater
# このテストは署名の無い file:// の更新を使う。署名の確かめは signed_update_test.py で見る
updater._ver.UPDATE_PUBLIC_KEY = ""

TMP = tempfile.gettempdir()


def main() -> int:
    # --- バージョン比較 ---------------------------------------------
    assert updater.is_newer("1.0.1", "1.0.0")
    assert updater.is_newer("1.2.0", "1.1.9")
    assert not updater.is_newer("1.0.0", "1.0.0")
    assert not updater.is_newer("0.9.9", "1.0.0")
    print("OK: バージョン比較")

    # --- ダミーの新バージョン zip を作る ---------------------------
    exe_name = os.path.basename(sys.executable)  # テスト環境では python.exe
    stage = os.path.join(TMP, "_upd_stage", "TriVReader")
    os.makedirs(stage, exist_ok=True)
    with open(os.path.join(stage, exe_name), "w") as f:
        f.write("dummy exe")
    with open(os.path.join(stage, "data.txt"), "w") as f:
        f.write("payload")
    zip_path = os.path.join(TMP, "_upd_new.zip")
    with zipfile.ZipFile(zip_path, "w") as z:
        z.write(os.path.join(stage, exe_name), f"TriVReader/{exe_name}")
        z.write(os.path.join(stage, "data.txt"), "TriVReader/data.txt")

    # --- update.json を file:// で配信して check() --------------------
    manifest = {"version": "9.9.9", "url": Path(zip_path).as_uri(), "notes": "テスト更新"}
    mpath = os.path.join(TMP, "_upd.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    info = updater.check(Path(mpath).as_uri())
    assert info and info["version"] == "9.9.9", info
    print("OK: update.json 取得＆新バージョン検出 ->", info["version"])

    # 同バージョンでは None
    manifest2 = dict(manifest, version=updater.APP_VERSION)
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest2, f)
    assert updater.check(Path(mpath).as_uri()) is None
    print("OK: 最新時は更新なし(None)")

    # --- ダウンロード＆展開 -----------------------------------------
    got = []
    dl = updater.download(info["url"], progress=lambda d, t: got.append((d, t)) or True)
    assert os.path.exists(dl)
    extracted = updater._extract(dl)
    assert os.path.exists(os.path.join(extracted, exe_name)), os.listdir(extracted)
    assert os.path.exists(os.path.join(extracted, "data.txt"))
    print("OK: ダウンロード＆展開（exe を含むフォルダを検出）")

    # --- SHA256 検証 -------------------------------------------------
    digest = updater.sha256(zip_path)
    assert updater.verify(zip_path, digest)          # 一致
    assert updater.verify(zip_path, "")              # 未指定は常にOK
    assert not updater.verify(zip_path, "deadbeef")  # 不一致は False
    print("OK: SHA256 検証（一致/未指定/不一致）")

    # --- GITHUB_REPO からの URL 組み立て ----------------------------
    from viewer import version as ver
    old = ver.GITHUB_REPO
    ver.GITHUB_REPO = "yamada/trivreader"
    assert updater.manifest_url() == \
        "https://github.com/yamada/trivreader/releases/latest/download/update.json"
    ver.GITHUB_REPO = old
    print("OK: GITHUB_REPO から更新元URLを生成")

    for p in (zip_path, mpath, dl):
        if os.path.exists(p):
            os.remove(p)
    print("ALL OK: 自動アップデータ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
