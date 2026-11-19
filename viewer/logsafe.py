"""記録（エラーログ・起動の失敗の記録）に個人情報を残さないための伏せ字。

事務所の PC では、フォルダ名・ファイル名に顧問先の会社名や氏名が入る
（例: C:\\Users\\職員\\Documents\\株式会社〇〇\\山田太郎_給与.pdf）。例外の文面や
トレースバックにはそのパスがそのまま出るし、例外の文面には入力欄の値が入る
こともある。そこで書く前に、
  - ドライブ（C:\\…）と UNC（\\\\サーバー\\共有\\…）のパスは <パス>.拡張子 にする
    （拡張子だけは調べるのに役立つので残す）
  - ただし、このアプリ自身や Python・site-packages のソース（.py など）の場所は
    読めるまま残す（<app>\\viewer\\document.py のように、利用者名の入る前の部分は落とす）
  - マイナンバーや口座番号のような長い数字は <番号> にする
  - 1 行と全体の長さに上限を付ける（長い文面に入力値が紛れ込んでいても、量を抑える）
をする。標準ライブラリだけで動く（起動に失敗しているときの rollback からも使う）。
"""
from __future__ import annotations

import os
import re
import sys

PLACEHOLDER = "<パス>"
NUMBER_PLACEHOLDER = "<番号>"
MAX_LINE = 500          # 1 行の上限（文字）
MAX_TOTAL = 20000       # 1 件の上限（文字）。超えたら頭と終わりだけ残す
_HEAD_KEEP = 4000

_CODE_EXTS = {".py", ".pyc", ".pyw", ".pyd", ".dll", ".so"}

# パスの始まり: ドライブ（C:\ C:/ と、repr で \ が二重の C:\\）、UNC（\\server・
# repr の \\\\server）。Qt は UNC を //server と書くので、それも見る（https:// の
# ような URL と区別するため、: の直後は除く）
_DRIVE_UNC = r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\\\\(?=[^\\\s])|\\\\\\\\(?=[^\\\s])"
_SLASH_UNC = r"|(?<![:\w/])//(?=[^/\s])"


def _patterns(start: str) -> tuple:
    start = "(?:" + start + ")"
    # 引用符で囲まれたパス（トレースバックの File "…"、例外の '…'）
    quoted = re.compile(r"(['\"])(" + start + r"(?:(?!\1)[^\r\n])*)\1")
    # 囲まれていないパス: 行の終わりか、パスに使えない文字の手前まで
    bare = re.compile(start + r"[^\r\n\"<>|*?]*")
    return quoted, bare


_QUOTED, _BARE = _patterns(_DRIVE_UNC + _SLASH_UNC)
# // を見ない版（JavaScript のように // がコメントになる文面向け）
_QUOTED_NOSLASH, _BARE_NOSLASH = _patterns(_DRIVE_UNC)
# 拡張子とみなす: . の後に英数字 1〜8 文字、その後が区切り
_EXT_AT = re.compile(r"\.([A-Za-z0-9]{1,8})(?=$|[\s'\"),;:\]>}、。）])")
_EXT_END = re.compile(r"\.([A-Za-z0-9]{1,8})$")

_NUMBERS = [
    re.compile(r"(?<![0-9A-Za-z_.])\d{7,}(?![0-9A-Za-z_])"),            # 口座番号・マイナンバー等
    re.compile(r"(?<![0-9])\d{4}[ \-]\d{4}[ \-]\d{4}(?![0-9])"),        # マイナンバー（区切りあり）
    re.compile(r"(?<![0-9])\d{4}-\d{6}(?:-\d)?(?![0-9])"),              # 基礎年金番号・雇用保険番号
    re.compile(r"(?<![0-9])0\d{1,4}-\d{1,4}-\d{3,4}(?![0-9])"),         # 電話番号
]


def _roots() -> list[tuple[str, str]]:
    """読めるまま残してよいソースの置き場所（小文字・区切りは \\）と表示名。"""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    roots = [(here, "<app>"), (getattr(sys, "_MEIPASS", ""), "<app>"),
             (sys.base_prefix, "<python>"), (sys.prefix, "<python>"),
             (sys.exec_prefix, "<python>")]
    out = []
    for path, label in roots:
        if path:
            out.append((os.path.normpath(path).replace("/", "\\").lower().rstrip("\\"), label))
    # 長い（深い）ものから当てる
    out.sort(key=lambda r: len(r[0]), reverse=True)
    return out


_ROOTS = _roots()


def _ext_of(path: str) -> str:
    tail = re.split(r"[\\/]", path.rstrip("\\/ "))[-1]
    m = _EXT_END.search(tail)
    return "." + m.group(1).lower() if m else ""


def _replace_path(path: str) -> str:
    """1 つのパスを伏せる（ソースの場所なら読めるまま、利用者名の前は落とす）。"""
    norm = re.sub(r"\\\\", r"\\", path).replace("/", "\\")
    low = norm.lower()
    ext = _ext_of(norm)
    i = low.find("\\site-packages\\")
    if i >= 0:
        return "<site-packages>\\" + norm[i + len("\\site-packages\\"):]
    if ext in _CODE_EXTS:
        for root, label in _ROOTS:
            if low.startswith(root + "\\"):
                return label + norm[len(root):]
    return PLACEHOLDER + ext


def _bare(m: re.Match) -> str:
    """囲まれていないパス。どこで終わるか分からないので、拡張子で区切る。

    後ろにまだパスの区切り（\\ /）が残る区切り方はしない（ファイル名が残るため）。
    区切れなければ、行の終わりまで伏せる。
    """
    seg = m.group(0)
    for em in _EXT_AT.finditer(seg):
        rest = seg[em.end():]
        if "\\" not in rest and "/" not in rest:
            return _replace_path(seg[:em.end()]) + rest
    return PLACEHOLDER


def redact_paths(text: str, slash_unc: bool = True) -> str:
    """ドライブと UNC のパスを伏せる。slash_unc=False なら //server の形は見ない。"""
    quoted, bare = (_QUOTED, _BARE) if slash_unc else (_QUOTED_NOSLASH, _BARE_NOSLASH)
    text = quoted.sub(lambda m: m.group(1) + _replace_path(m.group(2)) + m.group(1), text)
    return bare.sub(_bare, text)


def redact_numbers(text: str) -> str:
    for pat in _NUMBERS:
        text = pat.sub(NUMBER_PLACEHOLDER, text)
    return text


def _cap(text: str, max_line: int, max_total: int) -> str:
    lines = []
    for line in text.split("\n"):
        if len(line) > max_line:
            line = line[:max_line] + f"…（{len(line) - max_line} 文字省略）"
        lines.append(line)
    text = "\n".join(lines)
    if len(text) > max_total:
        head = min(_HEAD_KEEP, max_total // 4)
        tail = max_total - head
        text = (text[:head] + f"\n…（{len(text) - max_total} 文字省略）…\n" + text[-tail:])
    return text


def redact(text, max_line: int = MAX_LINE, max_total: int = MAX_TOTAL) -> str:
    """記録に書く前の文面を伏せる（パス → 番号 → 長さの順）。"""
    text = "" if text is None else str(text)
    return _cap(redact_numbers(redact_paths(text)), max_line, max_total)
