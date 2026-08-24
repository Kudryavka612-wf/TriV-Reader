"""設定・データの保存先を一元管理する（通常 / ポータブル両対応）。

ポータブル判定: exe（またはプロジェクト）と同じ場所に portable.ini があれば
ポータブルモード。設定は exe 隣の TriVReader.ini、OCR 言語データなどは
exe 隣の TriVReader_data フォルダに保存され、フォルダごと持ち運べる。

通常モードでは設定はレジストリ、データは LOCALAPPDATA 配下の trivreader。
"""
from __future__ import annotations

import os
import sys


def base_dir() -> str:
    """実行ファイル（凍結時）またはプロジェクトルートのディレクトリ。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # viewer/ の親（プロジェクトルート）
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_portable() -> bool:
    return os.path.exists(os.path.join(base_dir(), "portable.ini"))


def is_lite() -> bool:
    """ライト版（OCR・傾き補正を省いた軽量ビルド）かどうか。"""
    return os.path.exists(os.path.join(base_dir(), "lite.ini"))


def data_dir() -> str:
    """書き込み可能なデータ保存ディレクトリを返す（存在しなければ作成）。"""
    if is_portable():
        d = os.path.join(base_dir(), "TriVReader_data")
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError:
            pass  # exe 隣が書込不可ならフォールバック
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "trivreader")
    os.makedirs(d, exist_ok=True)
    return d


def tessdata_dir() -> str:
    return os.path.join(data_dir(), "tessdata")


def make_settings():
    """QSettings を返す（ポータブル時は INI ファイル、通常時はレジストリ）。"""
    from PySide6.QtCore import QSettings

    if is_portable():
        ini = os.path.join(base_dir(), "TriVReader.ini")
        return QSettings(ini, QSettings.Format.IniFormat)
    return QSettings("trivreader", "trivreader")


def safe_cwd() -> str:
    """プロセスのカレントディレクトリとして掴んでよい場所を返す。

    エクスプローラから PDF をダブルクリックして起動すると、その PDF が入って
    いるフォルダがプロセスのカレントディレクトリになる。Windows は CWD にして
    いるフォルダの削除/移動を拒否する（WinError 32）ため、閲覧しただけで
    フォルダを整理できなくなる。トレイ常駐でプロセスが残る分、症状も長引く。
    """
    try:
        return data_dir()
    except OSError:
        return os.path.expanduser("~")


def ensure_safe_cwd() -> None:
    """CWD がユーザーのフォルダを掴んでいたら、安全な場所へ移す。"""
    target = safe_cwd()
    try:
        if os.path.normcase(os.path.abspath(os.getcwd())) == os.path.normcase(target):
            return
    except OSError:
        pass  # CWD 自体が消えている場合も chdir で復帰させる
    try:
        os.chdir(target)
    except OSError:
        pass
