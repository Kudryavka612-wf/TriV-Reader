"""OCR 用の tessdata（言語データ）解決とダウンロード。

OCR エンジンは PyMuPDF 内蔵の Tesseract を利用する。必要なのは言語データ
(*.traineddata) の入った tessdata ディレクトリのみ。書き込み可能なユーザー
キャッシュに集約し、不足言語はインストール先からコピー or ダウンロードで補う。
"""
from __future__ import annotations

import os
import shutil

# urllib は起動時間の 1 割ほどを占めるわりに、言語データが足りないときにしか
# 使わない。ここでは読み込まず、実際にダウンロードする直前に import する。

TESSDATA_FAST_URL = (
    "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata"
)
# UB-Mannheim 版 Tesseract の既定インストール先
INSTALL_TESSDATA = r"C:\Program Files\Tesseract-OCR\tessdata"

# 表示名 → Tesseract 言語コード（不足分は tessdata_fast から自動DL）
LANGUAGE_CHOICES = {
    "日本語 + 英語": "jpn+eng",
    "日本語": "jpn",
    "日本語(縦書き)": "jpn_vert",
    "英語": "eng",
    "中国語(簡体)": "chi_sim",
    "中国語(繁体)": "chi_tra",
    "韓国語": "kor",
    "フランス語": "fra",
    "ドイツ語": "deu",
}


def cache_dir() -> str:
    """ダウンロードした言語データを置く書き込み可能ディレクトリ。

    ポータブル時は exe 隣の TriVReader_data\\tessdata、通常時は
    %LOCALAPPDATA%\\trivreader\\tessdata（storage が判定）。
    """
    from . import storage

    return storage.tessdata_dir()


def ascii_dir(path: str) -> str:
    """Tesseract に渡せる（ASCII だけの）フォルダを返す。

    PyMuPDF 内蔵の Tesseract は言語データのフォルダを ANSI の fopen で開く。利用者名が
    日本語（C:/Users/山田太郎 など）や、日本語のフォルダに置いたポータブル版では、開けず
    「Tesseract language initialisation failed」になり、OCR が一切動かなかった。
    パスが ASCII だけでなければ、8.3 の短い名前、それも無ければ ASCII だけの共有の場所
    （%PUBLIC%）を使う。
    """
    if path.isascii():
        return path
    os.makedirs(path, exist_ok=True)
    try:   # 8.3 の短い名前（無効にしているボリュームでは得られない）
        import ctypes

        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf)) and buf.value.isascii():
            return buf.value
    except Exception:  # noqa: BLE001
        pass
    for base in (os.environ.get("PUBLIC"), os.environ.get("ProgramData"), "C:\\Users\\Public"):
        if base and base.isascii():
            d = os.path.join(base, "TriVReader", "tessdata")
            try:
                os.makedirs(d, exist_ok=True)
                return d
            except OSError:
                continue
    return path


def fetch(url: str, target: str, timeout: float = 30.0) -> None:
    """言語データを取得して target に置く。途中で切れたものを残さない。

    最終の名前へ直接書くと、通信が切れたとき不完全なファイルが「ある」ものとして残り、
    以後ずっと OCR が失敗した（利用者は手で消すしかない）。一時ファイルに書き切り、
    大きさを確かめてから置き換える。通信が止まっても timeout 秒で諦める。
    """
    import urllib.request

    part = target + ".part"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp, open(part, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
        if total and got != total:
            raise OSError(f"ダウンロードが途中で切れました（{got} / {total} バイト）")
        if got < 100_000:
            raise OSError("言語データが小さすぎます（取得に失敗した可能性があります）")
        os.replace(part, target)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise


def ensure_languages(langs: list[str], on_download=None) -> str:
    """必要な言語を含む tessdata ディレクトリを用意してパスを返す。

    - eng は常に確保（Tesseract の基本）
    - インストール先にあればコピー、無ければ tessdata_fast から取得
    on_download(lang) はダウンロード開始時に呼ばれる（UI 通知用）。
    返すパスは ASCII だけ（ascii_dir）。
    """
    home = cache_dir()
    os.makedirs(home, exist_ok=True)
    dst = ascii_dir(home)
    os.makedirs(dst, exist_ok=True)
    for lang in dict.fromkeys(list(langs) + ["eng"]):  # 重複除去・順序維持
        target = os.path.join(dst, f"{lang}.traineddata")
        if os.path.exists(target):
            continue
        earlier = os.path.join(home, f"{lang}.traineddata")   # 以前に取得した分
        if os.path.normcase(earlier) != os.path.normcase(target) and os.path.exists(earlier):
            shutil.copy(earlier, target)
            continue
        installed = os.path.join(INSTALL_TESSDATA, f"{lang}.traineddata")
        if os.path.exists(installed):
            shutil.copy(installed, target)
            continue
        if on_download:
            on_download(lang)
        fetch(TESSDATA_FAST_URL.format(lang=lang), target)
    return dst


def available_languages(tessdata: str) -> list[str]:
    """tessdata 内の利用可能言語コード一覧（osd を除く）。"""
    if not tessdata or not os.path.isdir(tessdata):
        return []
    return sorted(
        f[: -len(".traineddata")]
        for f in os.listdir(tessdata)
        if f.endswith(".traineddata") and f != "osd.traineddata"
    )
