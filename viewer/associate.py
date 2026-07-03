"""TriV-Reader を .pdf に関連付ける（管理者不要・現在のユーザーのみ）。

associate_pdf.py（開発者向けCLI）とアプリ本体（メニューの「既定のPDFアプリに
設定」）の両方から共有するロジック。凍結後の exe にも同梱されるよう
viewer パッケージ配下に置く（associate_pdf.py はここには含まれない）。

Windows 10/11 は既定アプリの乗っ取り防止のため、レジストリ書き込みだけでは
「既定」にはできない（UserChoice はハッシュ保護されており OS のみが書ける）。
このモジュールは「開くアプリの候補」として登録するところまでを行い、実際に
既定へ選ぶには Windows の［既定のアプリ］設定を開いてユーザー自身に選んで
もらう必要がある（open_default_apps_settings 参照）。
"""
from __future__ import annotations

import ctypes
import os
import sys
import winreg

PROGID = "TriVReader.Document"
APP_NAME = "TriV-Reader"
EXE_NAME = "TriVReader.exe"


def _default_exe() -> str | None:
    root = os.path.join(os.path.expanduser("~"), "TriVReader_dist", "dist")
    candidates = [
        os.path.join(root, "TriVReader", EXE_NAME),  # onedir
        os.path.join(root, EXE_NAME),                # onefile
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def current_exe() -> str | None:
    """実行中の exe パス（凍結時のみ）。

    「今動いているアプリ自身」を登録したいので、ソース実行時（開発機に
    たまたま古いビルドが残っている場合など）は None を返し、無関係な exe を
    誤って登録しないようにする。CLI（associate_pdf.py）側で任意のビルド済み
    exe を探したい場合は _default_exe() を直接使う。
    """
    if getattr(sys, "frozen", False):
        return sys.executable
    return None


def _set(key_path: str, name, value) -> None:
    k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
    winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
    winreg.CloseKey(k)


def register(exe: str) -> None:
    icon = f'"{exe}",0'  # アプリ起動アイコン
    # 関連付けファイル（.pdf）は、アプリ起動用とは別の「書類」風アイコンにする
    doc_ico = os.path.join(os.path.dirname(exe), "pdf_doc.ico")
    file_icon = f'"{doc_ico}",0' if os.path.exists(doc_ico) else icon
    cmd = f'"{exe}" "%1"'
    classes = r"Software\Classes"
    # ProgID（このアプリの "ファイルの種類"）: ファイルのアイコンは書類アイコン
    _set(rf"{classes}\{PROGID}", None, "PDF ドキュメント")
    _set(rf"{classes}\{PROGID}\DefaultIcon", None, file_icon)
    _set(rf"{classes}\{PROGID}\shell\open\command", None, cmd)
    # 「プログラムから開く」候補に追加
    _set(rf"{classes}\.pdf\OpenWithProgids", PROGID, "")
    # Applications にも登録（「別のプログラムを選択」「既定のアプリ」に表示）。
    # FriendlyAppName が無いと Windows は実行ファイル名をそのまま表示するため、
    # 「TriVReader.exe」ではなく「TriV-Reader」と表示されるよう明示する。
    appkey = rf"{classes}\Applications\{EXE_NAME}"
    _set(appkey, "FriendlyAppName", APP_NAME)
    _set(rf"{appkey}\shell\open\command", None, cmd)
    _set(rf"{appkey}\DefaultIcon", None, icon)
    _set(rf"{appkey}\SupportedTypes", ".pdf", "")
    _notify()


def is_registered(exe: str) -> bool:
    """このバージョンの exe を指して登録済みか（再登録が必要かの判定用）。"""
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           rf"Software\Classes\{PROGID}\shell\open\command")
        val, _ = winreg.QueryValueEx(k, None)
        winreg.CloseKey(k)
        return os.path.normcase(exe) in os.path.normcase(val)
    except FileNotFoundError:
        return False


def unregister() -> None:
    classes = r"Software\Classes"
    _delete_tree(rf"{classes}\{PROGID}")
    _delete_tree(rf"{classes}\Applications\{EXE_NAME}")
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{classes}\.pdf\OpenWithProgids",
                           0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(k, PROGID)
        winreg.CloseKey(k)
    except FileNotFoundError:
        pass
    _notify()


def _delete_tree(path: str) -> None:
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                           winreg.KEY_ALL_ACCESS)
    except FileNotFoundError:
        return
    while True:
        try:
            sub = winreg.EnumKey(k, 0)
        except OSError:
            break
        _delete_tree(path + "\\" + sub)
    winreg.CloseKey(k)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass


def _notify() -> None:
    # SHCNE_ASSOCCHANGED でシェルに変更を通知
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


def open_default_apps_settings() -> None:
    """Windows の［設定 > アプリ > 既定のアプリ］を開く。

    UserChoice はハッシュ保護されており、プログラムから直接「既定」に
    することはできない（Windows 8 以降のセキュリティ制限）ため、レジストリ
    登録の後はここでユーザー自身に選んでもらう。
    """
    os.startfile("ms-settings:defaultapps")  # noqa: S606
