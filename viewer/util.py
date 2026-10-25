"""ウィンドウ側で共有する小さなヘルパー。"""
from __future__ import annotations

import re
import unicodedata

# 範囲を表す記号（全角・和文のものも）→ "-"、区切り（読点・全角カンマ等）→ ","
_DASHES = "-‐‑‒–—―ー－～〜~"
_SEPS = ",、，､;；"


def parse_ranges(text: str, page_count: int) -> list[int]:
    """"1-3,5,8" のような 1 始まりの指定を 0 始まりのインデックス列にする。

    順序は入力どおり保持する（並べ替え抽出に使えるよう）。日本語入力のまま
    打たれた「１－３」「1～3」「1-3、5」も受け付ける。「3-」は 3 ページ目から
    最後まで、「-3」は先頭から 3 ページ目まで。誤りは日本語の ValueError。
    """
    text = unicodedata.normalize("NFKC", text or "")
    for ch in _DASHES:
        text = text.replace(ch, "-")
    for ch in _SEPS:
        text = text.replace(ch, ",")
    text = re.sub(r"\s+", "", text)
    indices: list[int] = []
    for token in text.split(","):
        if not token:
            continue
        m = re.fullmatch(r"(\d*)-(\d*)", token)
        if m and (m.group(1) or m.group(2)):
            start = int(m.group(1)) if m.group(1) else 1
            end = int(m.group(2)) if m.group(2) else page_count
            step = 1 if end >= start else -1
            for p in range(start, end + step, step):
                indices.append(p - 1)
        elif token.isdigit():
            indices.append(int(token) - 1)
        else:
            raise ValueError(f"「{token}」はページ番号として読めません（例: 1-3, 5）")
    if not indices:
        raise ValueError("ページが指定されていません")
    for i in indices:
        if i < 0 or i >= page_count:
            raise ValueError(f"範囲外のページ番号です（1〜{page_count}）")
    return indices
