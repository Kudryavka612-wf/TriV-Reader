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


def ensure_languages(langs: list[str], on_download=None) -> str:
    """必要な言語を含む tessdata ディレクトリを用意してパスを返す。

    - eng は常に確保（Tesseract の基本）
    - インストール先にあればコピー、無ければ tessdata_fast から取得
    on_download(lang) はダウンロード開始時に呼ばれる（UI 通知用）。
    """
    dst = cache_dir()
    os.makedirs(dst, exist_ok=True)
    for lang in dict.fromkeys(list(langs) + ["eng"]):  # 重複除去・順序維持
        target = os.path.join(dst, f"{lang}.traineddata")
        if os.path.exists(target):
            continue
        installed = os.path.join(INSTALL_TESSDATA, f"{lang}.traineddata")
        if os.path.exists(installed):
            shutil.copy(installed, target)
            continue
        if on_download:
            on_download(lang)
        import urllib.request

        urllib.request.urlretrieve(TESSDATA_FAST_URL.format(lang=lang), target)
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
