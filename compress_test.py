"""PDF 圧縮（画像の再圧縮）の検証。

- 標準プリセットでスキャン風 PDF が実際に縮むこと
- テキスト・ページ数・注釈が壊れないこと
- グレースケール / 無劣化 / キャンセル / 自分自身への上書きの各経路
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

import fitz
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(__file__))
from viewer.document import PdfDocument

TMP = tempfile.gettempdir()


def _scan_image(w: int = 1700, h: int = 2200) -> bytes:
    """スキャン原稿らしい画像（白地＋文字状の濃い帯＋読み取りノイズ）の PNG。

    のっぺりした合成画像だと PNG のままの方が小さく JPEG が選ばれないため、
    実際の原稿に近いノイズを載せる。
    """
    rng = np.random.default_rng(7)
    base = np.full((h, w, 3), 245, np.int16)
    for i in range(18):
        y0 = 120 + i * 110
        base[y0:y0 + 30, 150:1500] = 45          # 文字行に見立てた濃い帯
    base[300:900, 900:1600] = rng.integers(60, 200, (600, 700, 3))  # 写真部分
    noise = rng.integers(-12, 13, (h, w, 1))
    img = Image.fromarray(np.clip(base + noise, 0, 255).astype(np.uint8), "RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_scan(path: str, pages: int = 2, px: tuple[int, int] = (1700, 2200)) -> None:
    """A4 全面に高解像度画像を貼った「スキャン風」PDF（テキストも載せる）。"""
    data = _scan_image(*px)
    doc = fitz.open()
    for n in range(pages):
        p = doc.new_page(width=595, height=842)
        p.insert_image(fitz.Rect(0, 0, 595, 842), stream=data)
        p.insert_text((72, 100), f"PAGE {n + 1} TEXT", fontsize=18)
    doc.save(path, deflate=True)
    doc.close()


def image_infos(path: str) -> list[dict]:
    out = []
    with fitz.open(path) as d:
        for pno in range(d.page_count):
            for info in d.load_page(pno).get_images(full=True):
                meta = d.extract_image(info[0])
                out.append({k: meta[k] for k in ("width", "height", "ext", "colorspace")})
    return out


def main() -> int:  # noqa: C901
    src = os.path.join(TMP, "_cmp_src.pdf")
    build_scan(src, 2)
    before = os.path.getsize(src)
    print(f"元ファイル: {before:,} バイト / 画像 {image_infos(src)}")

    # --- 標準プリセット（150dpi / q72） --------------------------------
    doc = PdfDocument(); doc.open(src)
    out = os.path.join(TMP, "_cmp_std.pdf")
    st = doc.compress_to_advanced(out, max_dpi=150, jpeg_quality=72)
    doc.close()
    after = os.path.getsize(out)
    assert st["recompressed"] >= 1, st
    assert after < before * 0.7, (before, after)
    infos = image_infos(out)
    assert infos and infos[0]["ext"] == "jpeg", infos
    # A4 幅 595pt = 8.264in → 150dpi なら約 1240px
    assert 1200 <= infos[0]["width"] <= 1280, infos
    with fitz.open(out) as d:
        assert d.page_count == 2
        assert "PAGE 1 TEXT" in d.load_page(0).get_text()
        assert "PAGE 2 TEXT" in d.load_page(1).get_text()
    print(f"OK: 標準圧縮 {before:,}B -> {after:,}B（{100 * after // before}%）"
          f" 画像{infos[0]['width']}px 再圧縮{st['recompressed']}件・テキスト維持")

    # --- 最小サイズは標準より小さいこと --------------------------------
    doc = PdfDocument(); doc.open(src)
    small = os.path.join(TMP, "_cmp_small.pdf")
    doc.compress_to_advanced(small, max_dpi=96, jpeg_quality=45)
    doc.close()
    assert os.path.getsize(small) < after, (after, os.path.getsize(small))
    print(f"OK: 最小サイズ {os.path.getsize(small):,}B < 標準 {after:,}B")

    # --- グレースケール ------------------------------------------------
    doc = PdfDocument(); doc.open(src)
    gray = os.path.join(TMP, "_cmp_gray.pdf")
    doc.compress_to_advanced(gray, max_dpi=150, jpeg_quality=72, grayscale=True)
    doc.close()
    assert image_infos(gray)[0]["colorspace"] == 1, image_infos(gray)
    assert os.path.getsize(gray) < after
    print(f"OK: グレースケール化 {os.path.getsize(gray):,}B（1成分）")

    # --- 無劣化（画像に触らない） --------------------------------------
    doc = PdfDocument(); doc.open(src)
    loss = os.path.join(TMP, "_cmp_lossless.pdf")
    st2 = doc.compress_to_advanced(loss, max_dpi=0)
    doc.close()
    assert st2["recompressed"] == 0 and st2["images"] == 0, st2
    assert image_infos(loss)[0]["width"] == 1700, image_infos(loss)
    assert os.path.getsize(loss) <= before
    print(f"OK: 無劣化は画像そのまま {os.path.getsize(loss):,}B")

    # --- 元文書は劣化させない（作業用コピーで処理している） ------------
    doc = PdfDocument(); doc.open(src)
    doc.compress_to_advanced(os.path.join(TMP, "_cmp_tmp.pdf"), max_dpi=96)
    keep = os.path.join(TMP, "_cmp_keep.pdf")
    doc.save_as(keep)
    doc.close()
    assert image_infos(keep)[0]["width"] == 1700, image_infos(keep)
    print("OK: 圧縮しても開いている文書は原寸のまま")

    # --- キャンセル -----------------------------------------------------
    doc = PdfDocument(); doc.open(src)
    canceled = os.path.join(TMP, "_cmp_cancel.pdf")
    if os.path.exists(canceled):
        os.remove(canceled)
    st3 = doc.compress_to_advanced(canceled, max_dpi=150,
                                   progress=lambda d, t: False)
    doc.close()
    assert st3["canceled"] and not os.path.exists(canceled), st3
    print("OK: キャンセルで何も書き出さない")

    # --- 自分自身への上書き（保存後もロックしない） --------------------
    selfp = os.path.join(TMP, "_cmp_self.pdf")
    build_scan(selfp, 1)
    doc = PdfDocument(); doc.open(selfp)
    doc.compress_to_advanced(selfp, max_dpi=150, jpeg_quality=72)
    assert doc.page_count == 1 and "PAGE 1 TEXT" in doc.full_text()
    os.replace(selfp, selfp + ".moved")  # 開いたままでも動かせる＝ロックなし
    os.replace(selfp + ".moved", selfp)
    doc.close()
    assert image_infos(selfp)[0]["ext"] == "jpeg"
    print("OK: 自分自身へ上書き圧縮（ファイルをロックしない）")

    # --- 画像のない PDF でも失敗しない ---------------------------------
    txt = os.path.join(TMP, "_cmp_txt.pdf")
    d = fitz.open()
    d.new_page().insert_text((72, 100), "hello", fontsize=20)
    d.save(txt); d.close()
    doc = PdfDocument(); doc.open(txt)
    out2 = os.path.join(TMP, "_cmp_txt_out.pdf")
    st4 = doc.compress_to_advanced(out2, max_dpi=150)
    doc.close()
    assert st4["images"] == 0 and os.path.exists(out2), st4
    print("OK: 画像なし PDF も処理できる")

    # --- 線画（色数が少ない画像）は PNG のまま縮める ---------------------
    art = os.path.join(TMP, "_cmp_art.pdf")
    line = Image.new("RGB", (4000, 5200), "white")
    draw = ImageDraw.Draw(line)
    for i in range(120):
        draw.rectangle([160, 40 * i + 40, 3800, 40 * i + 68], outline="black", width=6)
    lb = io.BytesIO(); line.save(lb, format="PNG")
    d = fitz.open()
    pg = d.new_page(width=595, height=842)
    pg.insert_image(fitz.Rect(0, 0, 595, 842), stream=lb.getvalue())
    d.save(art, deflate=True); d.close()
    doc = PdfDocument(); doc.open(art)
    art_out = os.path.join(TMP, "_cmp_art_out.pdf")
    doc.compress_to_advanced(art_out, max_dpi=150, jpeg_quality=72)
    doc.close()
    ai = image_infos(art_out)[0]
    assert ai["ext"] == "png", f"線画が JPEG 化された（にじむ）: {ai}"
    assert ai["width"] <= 1280, ai  # 解像度は落ちている
    assert os.path.getsize(art_out) < os.path.getsize(art) * 0.8
    print(f"OK: 線画は PNG のまま {os.path.getsize(art):,}B -> "
          f"{os.path.getsize(art_out):,}B")

    for f in (src, out, small, gray, loss, keep, selfp, txt, out2, art, art_out,
              os.path.join(TMP, "_cmp_tmp.pdf")):
        if os.path.exists(f):
            os.remove(f)
    print("ALL OK: PDF 圧縮")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
