"""圧縮ダイアログの「試算 → 保存」の使い回しと後始末のテスト。

試算で作った一時ファイルが、同じ設定の保存でそのまま使われること／設定を
変えたら使われないこと／閉じたら残らないことを確認する。
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PIL import Image
from PySide6.QtWidgets import QApplication, QDialog

from viewer.compress_dialog import CompressDialog
from viewer.document import PdfDocument

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def build(path: str) -> None:
    import random

    random.seed(3)
    im = Image.new("RGB", (1800, 1200))
    im.putdata([(random.randrange(256), random.randrange(256), random.randrange(256))
                for _ in range(1800 * 1200)])
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    d = fitz.open()
    p = d.new_page(width=595, height=842)
    p.insert_image(fitz.Rect(40, 40, 555, 400), stream=buf.getvalue())
    p.insert_text((60, 500), "compress cache test", fontsize=18)
    d.save(path)
    d.close()


def main() -> int:
    tmp = tempfile.gettempdir()
    src = os.path.join(tmp, "_compress_cache.pdf")
    build(src)
    app = QApplication(sys.argv[:1])
    doc = PdfDocument()
    doc.open(src)

    dlg = CompressDialog(doc, None)
    check("キャンセルが日本語",
          any(b.text() == "キャンセル" for b in dlg.findChildren(type(dlg.btn_estimate))))

    dlg._radios["standard"].setChecked(True)
    dlg._estimate()
    app.processEvents()
    c = dlg._cache
    check("試算: 一時ファイルを持つ", c is not None and os.path.exists(c["path"]), str(c))
    check("試算: 結果の文に使い回しの案内", "そのまま使います" in dlg.result.text())
    est_size = os.path.getsize(c["path"])

    # 違う設定なら使わない
    other = dict(dlg.options(), max_dpi=96, jpeg_quality=45)
    check("違う設定では None", dlg.take_cached(other) is None)
    check("違う設定を問い合わせても一時ファイルは残る", os.path.exists(c["path"]))

    # 同じ設定なら渡す（渡したら手放す）
    got = dlg.take_cached(dlg.options())
    check("同じ設定なら (path, stats)", got is not None and got[0] == c["path"]
          and got[1].get("recompressed") == 1, str(got))
    check("渡したら手放す", dlg._cache is None)
    out = os.path.join(tmp, "_compress_cache_out.pdf")
    if os.path.exists(out):
        os.remove(out)
    import shutil

    shutil.move(got[0], out)
    check("移動した成果物が開ける・中身が同じ", os.path.getsize(out) == est_size
          and "compress cache test" in fitz.open(out)[0].get_text())
    check("成果物は元より小さい", est_size < os.path.getsize(src))

    # 試算し直すと前の一時ファイルは消える
    dlg._estimate()
    first = dlg._cache["path"]
    dlg._radios["small"].setChecked(True)
    dlg._estimate()
    check("試算し直しで前の一時ファイルが消える", not os.path.exists(first)
          and os.path.exists(dlg._cache["path"]))

    # キャンセルで閉じたら残らない
    leftover = dlg._cache["path"]
    dlg.done(QDialog.DialogCode.Rejected)
    check("キャンセルで一時ファイルが消える", not os.path.exists(leftover))

    # OK で閉じた（保存で使われる）場合は残る → 保存側が move する
    dlg2 = CompressDialog(doc, None)
    dlg2._radios["standard"].setChecked(True)
    dlg2._estimate()
    keep = dlg2._cache["path"]
    dlg2.done(QDialog.DialogCode.Accepted)
    check("OK で閉じた直後は残っている", os.path.exists(keep))
    dlg2._drop_cache()
    check("後始末で消える", not os.path.exists(keep))
    doc.close()

    ok = all(results)
    print("ALL PASS" if ok else f"{results.count(False)} FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
