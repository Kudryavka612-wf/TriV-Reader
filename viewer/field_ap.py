"""入力欄（テキスト・コンボ）の外観を自前で作る。

外観（/AP）は、印刷・保存した PDF・提出先の Acrobat で見える「入力した文字」。
MuPDF に作らせると、官公庁フォームで次の 2 つが起きていた。

- 欄の文字フォントが PDF に埋め込まれていない和文（MS P明朝 / MS ゴシック）だと、
  日本語の行の位置を誤り、文字の下半分が欄の外に出て切れる。
- 和文を中国語フォント（/Song, GB1）で書き込む（jpfonts の説明を参照）。

ここでは MS ゴシック / MS 明朝（Adobe-Japan1・埋め込みなし）で、Acrobat と同じ
置き方をする：1 行の欄は上下中央、コム欄（1 文字 1 マス）はマスの中央、複数行の
欄は上から折り返す。収まらなければ文字を小さくする（欄の文字サイズの設定＝/DA
は書き換えない）。
"""
from __future__ import annotations

import re

import fitz

from . import jpfonts

COMB = 1 << 24
MULTILINE = 1 << 12

ASCENT = 0.859    # MS ゴシック / 明朝の字面（文字サイズに対する比）
DESCENT = 0.141
PAD = 2.0         # 欄の内側の余白（pt）。Acrobat と同じ
LEADING = 1.15    # 複数行の行送り


def em(ch: str) -> float:
    """1 文字の幅（文字サイズに対する比）。半角は 0.5、それ以外は 1。"""
    o = ord(ch)
    if 0x20 <= o <= 0x7E or 0xFF61 <= o <= 0xFF9F:
        return 0.5
    return 1.0


def width(text: str, fs: float) -> float:
    return sum(em(c) for c in text) * fs


def _hex(text: str) -> str:
    """UniJIS-UTF16-H で読める 16 進文字列（UTF-16BE）。"""
    return text.encode("utf-16-be").hex()


def _wrap(text: str, avail: float, fs: float) -> list[str]:
    """複数行欄の折り返し。欧文は語で、和文は文字で折る（元の改行は保つ）。"""
    lines: list[str] = []
    for para in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = ""
        for tok in re.findall(r"[\x21-\x7e]+ *| +|.", para):
            if not line or width((line + tok).rstrip(" "), fs) <= avail:
                line += tok
                continue
            if width(tok.rstrip(" "), fs) > avail:   # 1 語が欄より長い
                for ch in tok:
                    if line and width(line + ch, fs) > avail:
                        lines.append(line.rstrip(" "))
                        line = ""
                    line += ch
                continue
            lines.append(line.rstrip(" "))
            line = tok.lstrip(" ")
        lines.append(line.rstrip(" "))
    return lines


def _align(widget) -> int:
    """/Q（0=左, 1=中央, 2=右）。ウィジェットに無ければ親（フィールド）から。"""
    doc = widget.parent.parent
    xref = widget.xref
    for _ in range(4):
        q = doc.xref_get_key(xref, "Q")
        if q[0] == "int":
            return int(q[1])
        parent = doc.xref_get_key(xref, "Parent")
        if parent[0] != "xref":
            break
        xref = int(parent[1].split()[0])
    return 0


def _rotation(widget) -> int:
    doc = widget.parent.parent
    r = doc.xref_get_key(widget.xref, "MK/R")
    try:
        return int(r[1]) % 360 if r[0] == "int" else 0
    except ValueError:
        return 0


def build(value: str, w: float, h: float, fontsize: float, *, comb: int = 0,
          multiline: bool = False, align: int = 0, color=(0, 0, 0)) -> tuple[str, float]:
    """欄の外観の描画命令（PDF の内容ストリーム）と、実際に使った文字サイズ。"""
    ops: list[str] = []
    avail_w = max(1.0, w - 2 * PAD)
    rgb = " ".join(f"{max(0.0, min(1.0, float(c))):.3f}" for c in (list(color) + [0, 0, 0])[:3])

    if comb:
        cell = w / comb
        chars = list(value)[:comb]
        fs = fontsize if fontsize > 0 else min(h * 0.7, 12.0)
        fs = min(fs, (h - 2) / 1.0, cell * 0.95 / max([em(c) for c in chars] or [0.5]))
        y = (h - fs) / 2 + DESCENT * fs
        # 右寄せ（金額欄など）は右のマスから詰める。中央寄せは真ん中に寄せる
        free = max(0, comb - len(chars))
        start = {1: free // 2, 2: free}.get(align, 0)
        for i, ch in enumerate(chars):
            x = (start + i) * cell + (cell - em(ch) * fs) / 2
            ops.append(f"1 0 0 1 {x:.3f} {y:.3f} Tm <{_hex(ch)}> Tj")
    elif multiline:
        fs = fontsize if fontsize > 0 else 10.0
        while True:
            lines = _wrap(value, avail_w, fs)
            need = len(lines) * fs * LEADING
            if need <= h - 2 * PAD or fs <= 4:
                break
            fs = max(4.0, fs - 0.5)   # 入りきらなければ小さくする（切れるよりよい）
        y = h - PAD - ASCENT * fs
        for line in lines:
            tw = width(line, fs)
            x = PAD if align == 0 else (w - tw) / 2 if align == 1 else w - PAD - tw
            if line:
                ops.append(f"1 0 0 1 {x:.3f} {y:.3f} Tm <{_hex(line)}> Tj")
            y -= fs * LEADING
    else:
        text = value.replace("\r", " ").replace("\n", " ")
        fs = fontsize if fontsize > 0 else min(12.0, (h - 2) * 0.8)
        fs = min(fs, h - 2)
        tw = width(text, fs)
        if tw > avail_w and tw > 0:
            fs = max(4.0, fs * avail_w / tw)   # 欄からはみ出す分だけ縮める
            tw = width(text, fs)
        y = (h - fs) / 2 + DESCENT * fs
        x = PAD if align == 0 else (w - tw) / 2 if align == 1 else w - PAD - tw
        if text:
            ops.append(f"1 0 0 1 {x:.3f} {y:.3f} Tm <{_hex(text)}> Tj")

    body = "\n".join(ops)
    content = (f"/Tx BMC\nq\n1 1 {w - 2:.3f} {h - 2:.3f} re W n\nBT\n/FJ {fs:.2f} Tf\n"
               f"{rgb} rg\n{body}\nET\nQ\nEMC\n")
    return content, fs


def write(widget, serif: bool | None = None) -> bool:
    """テキスト欄・コンボの外観を、今の値で作り直す。作れたら True。

    値の書き込みと /AP の器は widget.update()（MuPDF）に任せ、その後で
    中身を差し替える。回転した欄は MuPDF の外観のままにする。
    """
    if widget.field_type not in (fitz.PDF_WIDGET_TYPE_TEXT, fitz.PDF_WIDGET_TYPE_COMBOBOX):
        return False
    if _rotation(widget):
        return False
    doc = widget.parent.parent
    ap = doc.xref_get_key(widget.xref, "AP/N")
    if ap[0] != "xref":
        return False
    apx = int(ap[1].split()[0])
    rect = widget.rect
    w, h = rect.width, rect.height
    flags = int(widget.field_flags or 0)
    is_text = widget.field_type == fitz.PDF_WIDGET_TYPE_TEXT
    maxlen = int(widget.text_maxlen or 0)
    comb = maxlen if (is_text and flags & COMB and maxlen) else 0
    multiline = bool(is_text and flags & MULTILINE)
    value = widget.field_value
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    value = "" if value is None else str(value)
    color = widget.text_color or (0, 0, 0)
    if serif is None:
        serif = jpfonts.is_mincho(widget.text_font or "")
    content, _fs = build(value, w, h, float(widget.text_fontsize or 0), comb=comb,
                         multiline=multiline, align=_align(widget), color=color)
    font = jpfonts.jp_font_xref(doc, serif)
    doc.update_stream(apx, content.encode("latin-1"))
    doc.xref_set_key(apx, "BBox", f"[0 0 {w:.3f} {h:.3f}]")
    doc.xref_set_key(apx, "Matrix", "[1 0 0 1 0 0]")
    doc.xref_set_key(apx, "Resources", f"<< /Font << /FJ {font} 0 R >> >>")
    return True
