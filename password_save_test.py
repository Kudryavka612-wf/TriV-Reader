"""パスワード付き PDF を開いて保存しても、中身が壊れず、パスワードが付いたままであることの確認。

MuPDF は、認証済みの暗号化文書で needs_pass を読んだり tobytes() を呼んだりすると
復号の鍵を壊す。以前は保存のたびに needs_pass を読んでいたので、パスワード付きの
PDF を開いて保存する（上書き・別名・自動保存・圧縮・印刷やページ操作のあとの保存）と、
全ページ白紙・題名が文字化けのファイルになり、元ファイルが失われた。

既存の acrobat_parity_test は「needs_pass が真」「authenticate が通る」しか見ず、
中身が壊れていても通っていた。ここでは保存したファイルを開き直して、本文・題名・
しおり・権限を比べる。
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

fitz.TOOLS.mupdf_display_errors(False)
results: list[bool] = []
PW = "ひみつ-pw1"
BODY = ["本文 ページ1", "本文 ページ2", "本文 ページ3"]
PERM = fitz.PDF_PERM_PRINT | fitz.PDF_PERM_ACCESSIBILITY


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def make_protected(tmp: str, name: str = "enc.pdf") -> str:
    plain = fitz.open()
    for line in BODY:
        plain.new_page().insert_text((72, 100), line, fontsize=20, fontname="japan")
    plain.set_metadata({"title": "題名-OK"})
    plain.set_toc([[1, "第1章", 1], [1, "第2章", 3]])
    path = os.path.join(tmp, name)
    plain.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=PW, owner_pw="own",
               permissions=PERM)
    plain.close()
    return path


def read_back(path: str, pw: str | None = PW) -> dict:
    """保存したファイルを開き直して、中身を取り出す。"""
    d = fitz.open(path)
    need = bool(d.needs_pass)
    auth = d.authenticate(pw) if (need and pw is not None) else (0 if need else -1)
    out = {"needs_pass": need, "auth": auth}
    if not need or auth > 0:
        out["text"] = [p.get_text().strip() for p in d]
        out["title"] = d.metadata.get("title")
        out["toc"] = [t[1] for t in d.get_toc()]
        out["perm"] = d.permissions
    d.close()
    return out


def raw_perm(path: str) -> int:
    """暗号化辞書に書かれた権限（/P）そのもの。開いた人の立場によらず比べられる。"""
    import pikepdf
    with pikepdf.open(path, password=PW) as pk:
        return int(pk.trailer.Encrypt.P)


def intact(r: dict) -> bool:
    return (r.get("text") == BODY and r.get("title") == "題名-OK"
            and r.get("toc") == ["第1章", "第2章"])


def main() -> int:
    app = QApplication(sys.argv)   # noqa: F841
    tmp = tempfile.mkdtemp(prefix="trv_pwsave_")
    try:
        src = make_protected(tmp)
        src_orig = os.path.join(tmp, "enc_orig_copy.pdf")
        shutil.copy(src, src_orig)
        orig = read_back(src)
        check("(前提) the sample is protected and readable with the password",
              orig["needs_pass"] and intact(orig))

        # --- 開いて、何もせず別名保存 --------------------------------------
        doc = PdfDocument()
        doc.open(src, password=PW)
        check("opening with the password works and the doc is reported as protected",
              doc.page_count == 3 and doc.is_encrypted and not doc.needs_password)
        out = os.path.join(tmp, "saved_as.pdf")
        doc.save_as(out)
        r = read_back(out)
        check("save as keeps the contents (text, title, outline)", intact(r), str(r))
        wrong = fitz.open(out)
        check("…and keeps the password on (a wrong password is still refused)",
              r["needs_pass"] and r["auth"] > 0 and wrong.authenticate("ちがう") == 0)
        wrong.close()
        check("…and keeps the permission flags (print / copy limits)",
              raw_perm(out) == raw_perm(src_orig) != -4,
              f"(/P {raw_perm(out)} vs {raw_perm(src_orig)})")
        check("…and does not open without the password",
              read_back(out, pw=None).get("text") is None)

        # --- 編集してから上書き保存、ページ操作と取り消しのあとの保存 ---------
        doc.add_rect(0, fitz.Rect(50, 50, 120, 90), color=(1, 0, 0), width=2)
        doc.rotate_page(1, 90)
        doc.rotate_page(1, -90)
        doc.undo()
        doc.undo()
        doc.save_as(src)   # 開いているファイル自身へ
        r = read_back(src)
        check("overwrite after annotating, rotating and undoing keeps the contents",
              intact(r) and r["needs_pass"], str(r))
        doc.save()
        r = read_back(src)
        check("a second in-place save is still intact", intact(r), str(r))

        # --- 印刷用の複製を作っても、元の保存は壊れない -----------------------
        cp = doc.print_copy("noannot") if hasattr(doc, "print_copy") else None
        if cp is not None:
            cp.close()
        doc.stripped_copy().close()
        out2 = os.path.join(tmp, "after_print.pdf")
        doc.save_as(out2)
        check("saving after making print copies is intact", intact(read_back(out2)))

        # --- 圧縮 ----------------------------------------------------------
        out3 = os.path.join(tmp, "compressed.pdf")
        doc.compress_to(out3)
        r = read_back(out3)
        check("lossless compress keeps the contents and the password",
              intact(r) and r["needs_pass"], str(r))
        out4 = os.path.join(tmp, "compressed2.pdf")
        doc.compress_to_advanced(out4, max_dpi=150, jpeg_quality=72)
        r = read_back(out4)
        check("image compress keeps the contents and the password",
              intact(r) and r["needs_pass"], str(r))
        doc.compress_to_advanced(src, max_dpi=150, jpeg_quality=72)   # 自分自身へ
        r = read_back(src)
        check("compress over itself keeps the file protected and intact",
              intact(r) and r["needs_pass"], str(r))
        out5 = os.path.join(tmp, "after_compress.pdf")
        doc.save_as(out5)
        check("…and the document stays usable and savable afterwards",
              intact(read_back(out5)) and doc.page_count == 3)

        # --- 抽出・固定保存・分割 ------------------------------------------
        out6 = os.path.join(tmp, "extract.pdf")
        doc.extract_to([2, 0], out6)
        r = read_back(out6)
        check("extracting pages from a protected doc keeps the password",
              r["needs_pass"] and r.get("text") == [BODY[2], BODY[0]], str(r))
        out7 = os.path.join(tmp, "flat.pdf")
        doc.flatten_to(out7)
        r = read_back(out7)
        check("saving a fixed copy keeps the password", r["needs_pass"] and r.get("text") == BODY,
              str(r))

        # --- 保護の解除・付け直し -------------------------------------------
        out8 = os.path.join(tmp, "decrypted.pdf")
        doc.save_decrypted(out8)
        r = read_back(out8, pw=None)
        check("removing the protection gives a readable file without a password",
              not r["needs_pass"] and intact(r), str(r))
        out9 = os.path.join(tmp, "reenc.pdf")
        doc.save_encrypted(out9, "新しい-pw")
        r = read_back(out9, pw="新しい-pw")
        check("protecting with a new password works and keeps the contents",
              r["needs_pass"] and intact(r), str(r))
        if os.path.exists(PdfDocument.SRGB_ICC):
            out10 = os.path.join(tmp, "pdfa.pdf")
            try:
                doc.export_pdfa(out10)
                check("PDF/A export of a protected doc works (and is not encrypted)",
                      not read_back(out10, pw=None)["needs_pass"])
            except Exception as exc:  # noqa: BLE001
                check("PDF/A export of a protected doc works", False, repr(exc))
        doc.close()

        # --- 開いたあとで認証する経路（open → authenticate） -----------------
        src2 = make_protected(tmp, "enc2.pdf")
        d2 = PdfDocument()
        d2.open(src2)
        check("opened without a password, it asks for one", d2.needs_password)
        check("a wrong password is refused", not d2.authenticate("ちがう"))
        ok = d2.authenticate(PW)
        out11 = os.path.join(tmp, "via_auth.pdf")
        d2.save_as(out11)
        check("authenticate() after open also leads to a safe save",
              ok and intact(read_back(out11)) and read_back(out11)["needs_pass"])
        d2.close()

        # --- 保護のない文書は、今までどおり保護なしで保存される -------------------
        plain = os.path.join(tmp, "plain.pdf")
        pd = fitz.open()
        pd.new_page().insert_text((72, 100), "plain", fontsize=20)
        pd.save(plain)
        pd.close()
        d3 = PdfDocument()
        d3.open(plain)
        outp = os.path.join(tmp, "plain_saved.pdf")
        d3.save_as(outp)
        r = read_back(outp, pw=None)
        check("an unprotected doc is still saved unprotected",
              not r["needs_pass"] and r["text"] == ["plain"] and not d3.is_encrypted)
        d3.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
