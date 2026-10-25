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
            # 全部を並べる前に端を確かめる。「1-99999999」のような指定で、巨大なリストを
            # 作ってから範囲外と分かると、キー 1 つごとに数秒固まっていた
            if not (1 <= start <= page_count and 1 <= end <= page_count):
                raise ValueError(f"範囲外のページ番号です（1〜{page_count}）")
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


def unique_path(path: str) -> str:
    """path が既にあれば「名前 (2).pdf」「名前 (3).pdf」…と、まだ無い名前にする。

    一括処理の出力先は、別のフォルダの同じ名前のファイルや前回の出力と重なると、
    確認なしに上書きして、先に処理した分が消えていた。
    """
    import os

    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def exists_fast(path: str) -> bool:
    """path があるか。ネットワーク上（UNC・ネットワークドライブ）は確かめずに「ある」とする。

    つながっていない共有への os.path.exists は、Windows で 1 件あたり数十秒止まる。
    起動直後に「最近使ったファイル」を全部確かめると、窓が出る前にその分固まっていた。
    ネットワーク上のものは、開くときに確かめる（開けなければそこで知らせる）。
    """
    import os

    try:
        if path.startswith("\\\\"):
            return True
        drive = os.path.splitdrive(path)[0]
        if drive and os.name == "nt":
            import ctypes

            if ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4:   # DRIVE_REMOTE
                return True
    except Exception:  # noqa: BLE001
        pass
    return os.path.exists(path)
