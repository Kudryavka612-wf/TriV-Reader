"""日本語を日本語の字形で描き、日本語として書き込む。

「日本語が中国語になる」の対策。原因は 2 つあった。

1. 表示・印刷: PyMuPDF に入っている CJK フォントは中国語向けの 1 本（Droid Sans
   Fallback）だけで、PDF に埋め込まれていない和文（役所の PDF に多い「MS 明朝」
   など）もこれで描かれ、漢字が中国語の字形になる。→ MuPDF のシステムフォント
   読み込みを差し替えて、Windows の和文フォント（MS ゴシック / MS 明朝 など）で
   描く（install）。
2. 書き込み: 入力欄や注釈の外観を作るとき、MuPDF は言語の指定が無いと簡体中国語
   として扱い、和文を中国語フォント（/Song, GB1）で書き込む。提出先の Acrobat でも
   中国語の字形になる（中国語フォントの追加を求められることもある）。→ 外観の
   和文フォントを日本語（Adobe-Japan1）の MS-Gothic / MS-Mincho に差し替える
   （japanize_ap）。埋め込みはしないので、受け取った側ではその PC の和文フォント
   （Acrobat なら小塚 / MS フォント）で描かれる。
"""
from __future__ import annotations

import os
import re

import fitz

# --- 表示・印刷：埋め込まれていない和文を Windows の和文フォントで描く -------
# (探すファイル名, TTC 内の番号) の候補。上から順に、ある物を使う。
_GOTHIC = [("msgothic.ttc", 0), ("BIZ-UDGothicR.ttc", 0), ("YuGothR.ttc", 0),
           ("meiryo.ttc", 0)]
_PGOTHIC = [("msgothic.ttc", 2), ("BIZ-UDGothicR.ttc", 1)] + _GOTHIC
_UIGOTHIC = [("msgothic.ttc", 1)] + _GOTHIC
_MINCHO = [("msmincho.ttc", 0), ("BIZ-UDMinchoM.ttc", 0), ("yumin.ttf", 0)]
_PMINCHO = [("msmincho.ttc", 1), ("BIZ-UDMinchoM.ttc", 1)] + _MINCHO
_MEIRYO = [("meiryo.ttc", 0)] + _GOTHIC
_YUGOTHIC = [("YuGothR.ttc", 0), ("YuGothM.ttc", 0)] + _GOTHIC
_YUMINCHO = [("yumin.ttf", 0)] + _MINCHO
_UDGOTHIC = [("BIZ-UDGothicR.ttc", 0)] + _GOTHIC
_UDPGOTHIC = [("BIZ-UDGothicR.ttc", 1)] + _PGOTHIC
_UDMINCHO = [("BIZ-UDMinchoM.ttc", 0)] + _MINCHO
_UDPMINCHO = [("BIZ-UDMinchoM.ttc", 1)] + _PMINCHO

# フォント名（空白・記号を除いて小文字にしたもの）に含まれる語 → 候補。
# 上から順に見る（「mspgothic」を「msgothic」より先に当てる）。
_BY_NAME = [
    ("mspgothic", _PGOTHIC), ("msuigothic", _UIGOTHIC), ("msgothic", _GOTHIC),
    ("mspmincho", _PMINCHO), ("msmincho", _MINCHO),
    ("bizudpgothic", _UDPGOTHIC), ("bizudgothic", _UDGOTHIC),
    ("bizudpmincho", _UDPMINCHO), ("bizudmincho", _UDMINCHO),
    ("meiryo", _MEIRYO), ("yugothic", _YUGOTHIC), ("yugoth", _YUGOTHIC),
    ("yumincho", _YUMINCHO), ("yumin", _YUMINCHO),
    # 和文の文字（Shift_JIS / UTF-16 の名前が読めた場合）
    ("ｐゴシック", _PGOTHIC), ("ゴシック", _GOTHIC), ("ｐ明朝", _PMINCHO),
    ("明朝", _MINCHO), ("メイリオ", _MEIRYO),
    # Acrobat / 日本の PDF 作成ソフトが使う名前
    ("kozmin", _MINCHO), ("ryumin", _MINCHO), ("heiseimin", _MINCHO),
    ("kozgo", _GOTHIC), ("gothicbbb", _GOTHIC), ("heiseikakugo", _GOTHIC),
    ("midashigo", _GOTHIC), ("jun101", _GOTHIC), ("hggothic", _GOTHIC),
    ("hgmincho", _MINCHO),
]

_FONT_DIRS = [
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
]

_cache: dict = {}
_installed = False


def _norm(name: str) -> str:
    """フォント名を照合用に整える（サブセット接頭辞・空白・記号を除いて小文字）。"""
    name = name or ""
    if re.match(r"^[A-Z]{6}\+", name):
        name = name[7:]
    try:   # Shift_JIS の名前が Latin-1 として渡ってくることがある
        decoded = name.encode("latin-1").decode("cp932")
        if decoded != name:
            name = decoded
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    name = name.split(",")[0]
    return re.sub(r"[\s\-_#]", "", name).lower()


def _find(candidates) -> tuple | None:
    for fname, index in candidates:
        for d in _FONT_DIRS:
            path = os.path.join(d, fname)
            if d and os.path.exists(path):
                return path, index
    return None


def resolve(name: str, serif: bool | None = None) -> tuple | None:
    """フォント名から、使う和文フォントの (パス, TTC 番号) を決める。

    名前で分からなければ serif（True=明朝 / False=ゴシック）で決める。どちらも
    決まらなければ None（＝和文フォントではない）。
    """
    key = _norm(name)
    for word, candidates in _BY_NAME:
        if word in key:
            return _find(candidates)
    if serif is None:
        return None
    return _find(_MINCHO if serif else _GOTHIC)


def _load(found):
    """FzFont を作る（同じファイルは 1 回だけ読む。MS 明朝は 1 本 9MB ある）。"""
    if found is None:
        return None
    if found not in _cache:
        import pymupdf.mupdf as m
        try:
            _cache[found] = m.FzFont(None, found[0], found[1], 0)
        except Exception:  # noqa: BLE001  壊れたフォントは使わない（内蔵に任せる）
            _cache[found] = None
    return _cache[found]


def _on_font(name, bold, italic, needs_exact_metrics):
    # 埋め込みの無い単純フォント（TrueType 等）。和文の名前だけ引き受ける。
    return _load(resolve(name))


def _on_cjk_font(name, ordering, serif):
    import pymupdf.mupdf as m

    found = resolve(name)
    if found is None:
        if ordering == m.FZ_ADOBE_JAPAN:
            found = resolve("", bool(serif))
        elif ordering == m.FZ_ADOBE_GB and name == "Song":
            # MuPDF 自身が付ける名前。以前の TriV-Reader が入力欄・注釈に
            # 書き込んだ和文はこれなので、日本語の字形で描く
            found = resolve("", True)
    return _load(found)


def _on_fallback(script, language, serif, bold, italic):
    import pymupdf.mupdf as m

    if script not in (m.UCDN_SCRIPT_HAN, m.UCDN_SCRIPT_HIRAGANA,
                      m.UCDN_SCRIPT_KATAKANA):
        return None
    if language in (m.FZ_LANG_zh, m.FZ_LANG_zh_Hans, m.FZ_LANG_zh_Hant, m.FZ_LANG_ko):
        return None   # 中国語・韓国語と明示されたものはそのまま
    return _load(resolve("", bool(serif)))


def install() -> bool:
    """MuPDF に、和文フォントの探し方を教える（何度呼んでもよい）。"""
    global _installed
    if _installed:
        return True
    try:
        import pymupdf.mupdf as m
        m.fz_install_load_system_font_funcs(_on_font, _on_cjk_font, _on_fallback)
    except Exception:  # noqa: BLE001  使えない版の PyMuPDF なら従来どおり描く
        return False
    _installed = True
    return True


# --- 書き込み：外観の和文フォントを日本語（Japan1）にする ----------------------
JP_GOTHIC = "MS-Gothic"
JP_MINCHO = "MS-Mincho"
# MuPDF が外観に入れる CJK フォントの名前（言語で変わる）
_MUPDF_CJK = {"/Song", "/Mincho", "/Gothic", "/Hei", "/Ming", "/Batang", "/Dotum",
              "/Kai", "/Fangsong"}


def is_mincho(fontname: str) -> bool:
    """フォント名が明朝（セリフ）系か。入力欄の和文を明朝/ゴシックのどちらで書くかに使う。"""
    key = _norm(fontname)
    return any(w in key for w in ("mincho", "明朝", "kozmin", "ryumin", "heiseimin",
                                  "yumin", "times", "tiro", "serif", "song"))


def update_widget(widget) -> None:
    """入力欄の値を書き込み、外観を作り直す（和文は日本語のフォントで）。

    PyMuPDF の widget.update() は、欄の文字設定（/DA）を「/Helv」に書き換えて
    しまう（MS P明朝の欄が Helvetica の欄になり、受け取った人が Acrobat で
    直すと和文が化ける）。元の /DA を控えて戻す。
    """
    doc = widget.parent.parent
    xref = widget.xref
    try:
        da = doc.xref_get_key(xref, "DA")
    except Exception:  # noqa: BLE001
        da = ("null", "null")
    font = widget.text_font or ""
    try:   # MuPDF は注釈ごとの /Lang で和文の扱いを決める（無いと簡体中国語）
        doc.xref_set_key(xref, "Lang", "(ja)")
    except Exception:  # noqa: BLE001
        pass
    widget.update()
    try:
        if da[0] == "string":
            doc.xref_set_key(xref, "DA", fitz.get_pdf_str(da[1]))
        elif da[0] == "null":
            doc.xref_set_key(xref, "DA", "null")   # 親の設定を引き継ぐ欄だった
    except Exception:  # noqa: BLE001
        pass
    serif = is_mincho(font)
    from . import field_ap   # 循環 import を避けてここで
    if not field_ap.write(widget, serif=serif):
        japanize_ap(doc, xref, serif=serif)


def jp_font_xref(doc: "fitz.Document", serif: bool) -> int:
    """文書の中の MS-Mincho / MS-Gothic（和文・埋め込みなし）の xref。無ければ作る。

    UTF-16 のまま書かれた本文をそのまま読めるよう、符号化は UniJIS-UTF16-H
    （MuPDF の外観と同じ）。字幅は全角 1 em、半角（英数字・半角カナ）0.5 em。
    """
    base = JP_MINCHO if serif else JP_GOTHIC
    cache = getattr(doc, "_trv_jp_fonts", None)
    if cache is None:
        cache = {}
        try:
            doc._trv_jp_fonts = cache
        except AttributeError:
            pass
    x = cache.get(base)
    if x and x < doc.xref_length():
        try:
            if doc.xref_get_key(x, "BaseFont")[1] == "/" + base:
                return x
        except Exception:  # noqa: BLE001
            pass
    fd = doc.get_new_xref()
    doc.update_object(fd, (
        "<< /Type /FontDescriptor /FontName /%s /Flags %d"
        " /FontBBox [ 0 -141 1000 859 ] /ItalicAngle 0 /Ascent 859 /Descent -141"
        " /CapHeight 769 /StemV 80 >>") % (base, 6 if serif else 4))
    cid = doc.get_new_xref()
    doc.update_object(cid, (
        "<< /Type /Font /Subtype /CIDFontType0 /BaseFont /%s"
        " /CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 6 >>"
        " /FontDescriptor %d 0 R /DW 1000 /W [ 1 95 500 231 632 500 ] >>") % (base, fd))
    top = doc.get_new_xref()
    doc.update_object(top, (
        "<< /Type /Font /Subtype /Type0 /BaseFont /%s /Encoding /UniJIS-UTF16-H"
        " /DescendantFonts [ %d 0 R ] >>") % (base, cid))
    cache[base] = top
    return top


def japanize_ap(doc: "fitz.Document", xref: int, serif: bool = False) -> bool:
    """注釈・入力欄の外観で、和文に使われたフォントを日本語のものに差し替える。

    annot.update() / widget.update() のたびに MuPDF が作り直すので、その後で
    毎回呼ぶ。差し替えたら True。
    """
    try:
        ap = doc.xref_get_key(xref, "AP/N")
    except Exception:  # noqa: BLE001
        return False
    if ap[0] != "xref":
        return False   # チェックボックス等（状態ごとの外観）は文字を持たない
    apx = int(ap[1].split()[0])
    try:
        fonts = doc.xref_get_key(apx, "Resources/Font")
    except Exception:  # noqa: BLE001
        return False
    if fonts[0] != "dict":
        return False
    changed = False
    for res, obj in re.findall(r"/([^\s/<>\[\]()]+)\s+(\d+)\s+0\s+R", fonts[1]):
        obj = int(obj)
        try:
            sub = doc.xref_get_key(obj, "Subtype")[1]
            base = doc.xref_get_key(obj, "BaseFont")[1]
            enc = doc.xref_get_key(obj, "Encoding")[1]
        except Exception:  # noqa: BLE001
            continue
        if sub != "/Type0" or base not in _MUPDF_CJK or "UTF16" not in enc:
            continue
        doc.xref_set_key(apx, "Resources/Font/" + res, "%d 0 R" % jp_font_xref(doc, serif))
        changed = True
    return changed
