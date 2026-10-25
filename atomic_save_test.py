"""書き出しの途中で失敗しても、保存先にあった既存のファイルが壊れないことの確認。

以前は、別名保存・抽出・分割・固定保存・暗号化などが保存先へ直接書いていた。容量不足・
USB を抜いた・ネットワークが切れた等で途中失敗すると、そこにあった別のファイルが切り詰め
られて開けなくなった。上書き保存の失敗では「元.pdf.tmp_save」も残った。一時ファイルに
書き切ってから置き換え、失敗したら一時ファイルも消す。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz
from PySide6.QtWidgets import QApplication

from viewer.document import PdfDocument

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def make_pdf(path: str, pages: int = 3) -> None:
    d = fitz.open()
    for i in range(pages):
        d.new_page().insert_text((72, 100), f"page {i + 1}", fontsize=20)
    d.save(path)
    d.close()


def readable(path: str, pages: int) -> bool:
    try:
        d = fitz.open(path)
        ok = d.page_count == pages
        d.close()
        return ok
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    app = QApplication(sys.argv)   # noqa: F841
    tmp = tempfile.mkdtemp(prefix="trv_atomic_")
    try:
        src = os.path.join(tmp, "src.pdf")
        make_pdf(src, 3)
        doc = PdfDocument()
        doc.open(src)

        # 保存の途中でディスクがいっぱいになる状況を作る（書きかけを残して例外）
        real_save = fitz.Document.save

        def failing_save(self, filename, *a, **k):
            with open(filename, "wb") as f:
                f.write(b"%PDF-1.7 truncated")
            raise OSError(28, "No space left on device")

        ops = {
            "save as": lambda p: doc.save_as(p),
            "extract": lambda p: doc.extract_to([0, 1], p),
            "fixed copy": lambda p: doc.flatten_to(p),
            "protect": lambda p: doc.save_encrypted(p, "pw"),
            "unprotect": lambda p: doc.save_decrypted(p),
            "compress": lambda p: doc.compress_to(p),
            "image compress": lambda p: doc.compress_to_advanced(p, max_dpi=150, jpeg_quality=70),
        }
        for name, op in ops.items():
            victim = os.path.join(tmp, f"existing_{name.replace(' ', '_')}.pdf")
            make_pdf(victim, 2)
            before = open(victim, "rb").read()
            fitz.Document.save = failing_save
            try:
                op(victim)
                raised = False
            except Exception:  # noqa: BLE001
                raised = True
            finally:
                fitz.Document.save = real_save
            left = [f for f in os.listdir(tmp) if f.endswith(".tmp_save")]
            check(f"{name}: a failed write raises, and the file that was there is untouched",
                  raised and open(victim, "rb").read() == before and readable(victim, 2),
                  f"(raised={raised})")
            check(f"{name}: …and leaves no temporary file behind", not left, str(left))

        # 上書き保存の失敗でも、元のファイルも文書の状態も無事
        doc.add_rect(0, fitz.Rect(10, 10, 60, 60))
        before = open(src, "rb").read()
        fitz.Document.save = failing_save
        try:
            doc.save()
            raised = False
        except Exception:  # noqa: BLE001
            raised = True
        finally:
            fitz.Document.save = real_save
        check("overwrite: a failed save keeps the original file and the unsaved edit",
              raised and open(src, "rb").read() == before and doc.modified
              and not [f for f in os.listdir(tmp) if f.endswith(".tmp_save")])

        # 失敗しなければ、ふつうに置き換わる
        out = os.path.join(tmp, "ok.pdf")
        make_pdf(out, 1)
        doc.save_as(out)
        check("a normal save replaces the file and leaves no temporary file",
              readable(out, 3) and not [f for f in os.listdir(tmp) if f.endswith(".tmp_save")])
        doc.close()
    finally:
        fitz.Document.save = real_save if "real_save" in dir() else fitz.Document.save
        shutil.rmtree(tmp, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
