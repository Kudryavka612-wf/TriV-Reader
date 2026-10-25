"""「日本語が中国語になる」と、入力欄の文字が切れる不具合の回帰テスト。

- テキスト注釈・入力欄の和文は、日本語（Adobe-Japan1）のフォント（MS-Gothic /
  MS-Mincho）で書き込まれる（以前は中国語の /Song, UniGB だった）
- 入力欄の文字設定（/DA）を書き換えない（以前は /Helv にされていた）
- 埋め込みの無い和文フォントの欄でも、文字が欄の中に収まる（以前は下半分が切れた）
- コム欄はマスの中に 1 文字ずつ、複数行は欄の中で折り返す
- 前の版で中国語フォントで書き込まれた外観も、保存の前に直る
- 埋め込みの無い和文は Windows の和文フォントで描く（あれば）

実在の官公庁フォーム（tests/fixtures/6010_form_sample.pdf）を使う。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 利用者本人の設定（レジストリ）を書き換えないよう、使い捨ての INI で動かす
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")

import fitz

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from viewer import field_ap, jpfonts  # noqa: E402
from viewer.document import PdfDocument  # noqa: E402

FORM = os.path.join(ROOT, "tests", "fixtures", "6010_form_sample.pdf")
results: list[bool] = []


def check(name: str, ok, extra: str = "") -> bool:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    return bool(ok)


def ap_fonts(d: fitz.Document, xref: int) -> list[tuple]:
    """外観が使うフォントの (BaseFont, Encoding, Ordering)。"""
    ap = d.xref_get_key(xref, "AP/N")
    if ap[0] != "xref":
        return []
    apx = int(ap[1].split()[0])
    res = d.xref_get_key(apx, "Resources/Font")
    out = []
    for _n, o in re.findall(r"/([^\s/<>]+)\s+(\d+)\s+0\s+R", res[1] if res[0] == "dict" else ""):
        o = int(o)
        desc = d.xref_get_key(o, "DescendantFonts")
        m = re.search(r"(\d+) 0 R", desc[1]) if desc[0] != "null" else None
        ordering = d.xref_get_key(int(m.group(1)), "CIDSystemInfo/Ordering")[1] if m else ""
        out.append((d.xref_get_key(o, "BaseFont")[1], d.xref_get_key(o, "Encoding")[1],
                    ordering))
    return out


def widget_by_name(d: fitz.Document, name: str):
    for pno in range(d.page_count):
        page = d.load_page(pno)
        for w in page.widgets():
            if w.field_name == name:
                return pno, page, w
    raise KeyError(name)


def ink_rows(pix: fitz.Pixmap, base: fitz.Pixmap | None = None) -> list[int]:
    """暗い画素のある行。base を渡すと、base で既に暗かった画素は数えない。"""
    rows = []
    for y in range(pix.height):
        lo, hi = y * pix.stride, y * pix.stride + pix.width * pix.n
        row = pix.samples[lo:hi]
        old = base.samples[lo:hi] if base is not None else None
        if any(row[i] < 110 and (old is None or old[i] >= 110)
               for i in range(0, len(row), pix.n)):
            rows.append(y)
    return rows


def test_freetext(root: str) -> None:
    src = os.path.join(root, "blank.pdf")
    d = fitz.open()
    d.new_page(width=400, height=300)
    d.save(src)
    doc = PdfDocument()
    doc.open(src)
    g = doc.add_freetext(0, fitz.Rect(20, 20, 380, 60), "直骨 令和 テスト", fontsize=16)
    m = doc.add_freetext(0, fitz.Rect(20, 80, 380, 120), "直骨 令和 明朝", fontsize=16,
                         fontname="tiro")
    s = doc.add_stamp(0, fitz.Point(40, 200), "承認")
    fg, fm, fs = (ap_fonts(doc._doc, x) for x in (g, m, s))
    check("Japanese in a text annotation is written with a Japanese font (gothic)",
          ("/MS-Gothic", "/UniJIS-UTF16-H", "Japan1") in fg, str(fg))
    check("…the 明朝 choice writes MS-Mincho", any(f[0] == "/MS-Mincho" for f in fm), str(fm))
    check("stamps too", any(f[0] == "/MS-Gothic" for f in fs), str(fs))
    check("no Chinese font (/Song, GB1) is written",
          not any(f[0] == "/Song" or f[2] == "GB1" for f in fg + fm + fs))
    moved = doc.move_annot(0, g, 10, 10)
    check("moving keeps the Japanese font", any(f[0] == "/MS-Gothic"
                                                 for f in ap_fonts(doc._doc, moved)))
    # 以前の版が書いた外観（中国語フォント）は、保存の前に直る
    page = doc._doc.load_page(0)
    old = page.add_freetext_annot(fitz.Rect(20, 240, 380, 280), "旧版の注釈", fontsize=14)
    old.update()
    check("(an old-style annotation really uses /Song)",
          any(f[0] == "/Song" for f in ap_fonts(doc._doc, old.xref)))
    out = os.path.join(root, "ft_out.pdf")
    doc.save_as(out)
    s2 = fitz.open(out)
    bad = []
    for a in s2[0].annots():
        bad += [f for f in ap_fonts(s2, a.xref) if f[0] == "/Song" or f[2] == "GB1"]
    check("saving rewrites older Chinese-font appearances to Japanese", not bad, str(bad))
    s2.close()
    doc.close()


def test_form() -> None:
    doc = PdfDocument()
    doc.open(FORM)
    d = doc._doc
    # 入れる前の紙面（罫線や見出し）を控えておき、入れた文字の分だけを比べる
    pno, page, w = widget_by_name(d, "FRM6010-01-008")
    r = fitz.Rect(w.rect)
    z = 3
    clip = fitz.Rect(r.x0 + 1, r.y0 - 2, r.x0 + 200, r.y1 + 12)
    empty = page.get_pixmap(matrix=fitz.Matrix(z, z), clip=clip)
    cases = {
        "FRM6010-01-001": "山田タロウ123",                      # Arial Narrow のコム欄
        "FRM6010-01-008": "東京都千代田区霞が関１丁目 ABC-123",  # MS P明朝 の 1 行欄
        "FRM6010-01-015": ("令和7年10月から体調不良のため休業。医師の指示により"
                           "自宅療養しています。Test 123"),       # MS P明朝 の複数行欄
        "FRM6010-03-086": "東京都千代田区霞が関１丁目 ABC-123",  # Arial Narrow 8pt
    }
    das = {}
    for name, val in cases.items():
        pno, page, w = widget_by_name(d, name)
        das[name] = d.xref_get_key(w.xref, "DA")[1]
        check(f"{name}: value is set", doc.set_field_value(pno, w.xref, val))

    for name, val in cases.items():
        pno, page, w = widget_by_name(d, name)
        fonts = ap_fonts(d, w.xref)
        check(f"{name}: written with a Japanese font", fonts and all(
            f[2] == "Japan1" and f[0] in ("/MS-Gothic", "/MS-Mincho") for f in fonts),
            str(fonts))
        check(f"{name}: the field's own font setting (/DA) is untouched",
              d.xref_get_key(w.xref, "DA")[1] == das[name],
              f"{das[name]!r} -> {d.xref_get_key(w.xref, 'DA')[1]!r}")
        check(f"{name}: the value is stored", w.field_value == val)
        want = "/MS-Mincho" if "Mincho" in das[name] else "/MS-Gothic"
        check(f"{name}: 明朝 fields stay 明朝, others ゴシック",
              fonts and fonts[0][0] == want, str(fonts))

    # 文字が欄の中に収まる（以前は MS P明朝 の欄で下半分が欄の外に出て切れた）
    pno, page, w = widget_by_name(d, "FRM6010-01-008")
    band = page.get_pixmap(matrix=fitz.Matrix(z, z), clip=clip)
    rows = ink_rows(band, empty)
    top_px, bottom_px = 2 * z, (r.height + 2) * z
    check("single-line Japanese text sits inside the field (not cut off at the bottom)",
          rows and min(rows) > top_px and max(rows) < bottom_px,
          f"ink rows {min(rows) if rows else None}..{max(rows) if rows else None}"
          f" vs field {top_px}..{bottom_px:.0f}")
    if rows:
        mid = (min(rows) + max(rows)) / 2
        check("…and roughly centred vertically",
              abs(mid - (top_px + bottom_px) / 2) < r.height * z * 0.2,
              f"(text mid {mid:.0f}, field mid {(top_px + bottom_px) / 2:.0f})")

    # コム欄: マスごとに 1 文字（Tm の x がそのマスの中）
    pno, page, w = widget_by_name(d, "FRM6010-01-001")
    apx = int(d.xref_get_key(w.xref, "AP/N")[1].split()[0])
    xs = [float(x) for x in re.findall(r"1 0 0 1 ([\d.]+) [\d.]+ Tm", d.xref_stream(apx).decode())]
    cell = w.rect.width / w.text_maxlen
    check("comb field: one character per box, inside its box",
          len(xs) == len(cases["FRM6010-01-001"])
          and all(i * cell <= x < (i + 1) * cell for i, x in enumerate(xs)), str(xs))

    # 複数行: 欄の中で折り返し、最後の行まで欄の中
    pno, page, w = widget_by_name(d, "FRM6010-01-015")
    apx = int(d.xref_get_key(w.xref, "AP/N")[1].split()[0])
    ys = [float(y) for y in re.findall(r"1 0 0 1 [\d.]+ ([\d.-]+) Tm", d.xref_stream(apx).decode())]
    check("multi-line field wraps into several lines that all fit",
          len(ys) >= 2 and min(ys) > 0, str(ys))

    # 保存・開き直しで値と外観が残る
    out = os.path.join(tempfile.gettempdir(), "_jp_font_form.pdf")
    doc.save_as(out)
    s = fitz.open(out)
    pno, page, w = widget_by_name(s, "FRM6010-01-008")
    check("after save + reopen the value and Japanese appearance remain",
          w.field_value == cases["FRM6010-01-008"]
          and any(f[0] == "/MS-Mincho" for f in ap_fonts(s, w.xref)))
    s.close()
    os.remove(out)
    doc.close()


def test_build_layout() -> None:
    content, fs = field_ap.build("あいうABC", 100, 20, 0)
    check("auto size picks a size that fits the height", 4 <= fs <= 16, f"({fs})")
    content, fs = field_ap.build("あ" * 15, 100, 20, 12)
    check("text longer than the field is shrunk to fit (not cut off)",
          fs < 12 and field_ap.width("あ" * 15, fs) <= 100 - 2 * field_ap.PAD + 0.01,
          f"({fs:.2f})")


def test_system_fonts() -> None:
    check("the Japanese-font hook is installed", jpfonts.install())
    found = jpfonts.resolve("MS-Mincho")
    if found is None:
        print("[SKIP] no Japanese system fonts on this machine")
        return
    check("MS-Mincho resolves to a mincho file", "min" in os.path.basename(found[0]).lower(),
          str(found))
    check("MS-PGothic resolves to the proportional face",
          jpfonts.resolve("MS-PGothic") == (found[0].replace(os.path.basename(found[0]),
                                                             "msgothic.ttc"), 2)
          or jpfonts.resolve("MS-PGothic") is not None)
    check("Shift_JIS font names are understood",
          jpfonts.resolve("ＭＳ 明朝".encode("cp932").decode("latin-1")) is not None)
    check("non-Japanese names are left to MuPDF", jpfonts.resolve("Helvetica") is None)
    # 埋め込みなしの Japan1 フォントで描いた字が、内蔵の中国語フォントと違う形になる
    d = fitz.open()
    page = d.new_page(width=120, height=60)
    xm = jpfonts.jp_font_xref(d, True)
    cont = b"BT /F 40 Tf 10 15 Td <76F4> Tj ET"   # 「直」
    x = d.get_new_xref()
    d.update_object(x, "<<>>")
    d.update_stream(x, cont)
    d.xref_set_key(page.xref, "Contents", f"{x} 0 R")
    d.xref_set_key(page.xref, "Resources", f"<< /Font << /F {xm} 0 R >> >>")
    ours = d[0].get_pixmap().samples
    fb = fitz.open()
    fp = fb.new_page(width=120, height=60)
    fp.insert_font(fontname="cjkf", fontbuffer=fitz.Font("cjk").buffer)
    fp.insert_text((10, 45), "直", fontname="cjkf", fontsize=40)
    builtin = fb[0].get_pixmap().samples
    check("non-embedded Japanese is drawn with a Japanese system font, not the "
          "built-in Chinese one", ours != builtin)


def main() -> int:
    root = tempfile.mkdtemp(prefix="trv_jpfont_")
    test_system_fonts()
    test_freetext(root)
    test_form()
    test_build_layout()
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
