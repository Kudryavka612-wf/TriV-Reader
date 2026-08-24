"""ウィンドウ側で共有する小さなヘルパー。"""
from __future__ import annotations


def parse_ranges(text: str, page_count: int) -> list[int]:
    """"1-3,5,8" のような 1 始まりの指定を 0 始まりのインデックス列にする。

    順序は入力どおり保持する（並べ替え抽出に使えるよう）。
    """
    indices: list[int] = []
    for token in text.replace(" ", "").split(","):
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            start, end = int(a), int(b)
            step = 1 if end >= start else -1
            for p in range(start, end + step, step):
                indices.append(p - 1)
        else:
            indices.append(int(token) - 1)
    if not indices:
        raise ValueError("ページが指定されていません")
    for i in indices:
        if i < 0 or i >= page_count:
            raise ValueError(f"範囲外のページ番号です（1〜{page_count}）")
    return indices
