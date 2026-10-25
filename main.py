"""自作 PDF エディタ エントリポイント。

実行: python main.py [開きたい.pdf]
"""
from __future__ import annotations

import getpass
import os
import sys

from PySide6.QtCore import QLibraryInfo, Qt, QTranslator
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from viewer import errorlog, storage, theme
from viewer.main_window import MainWindow

# ユーザー毎に一意なサーバー名（同一PCの複数ユーザーでも衝突しない）
try:
    _SERVER_NAME = f"TriVReader-SingleInstance-{getpass.getuser()}"
except Exception:  # noqa: BLE001
    _SERVER_NAME = "TriVReader-SingleInstance"


def _install_japanese(app: QApplication) -> list:
    """Qt 標準UI（保存/キャンセル/OK 等のボタン）を日本語化する。"""
    tr_dir = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    keep = []
    for name in ("qtbase_ja", "qt_ja"):
        tr = QTranslator(app)
        if tr.load(name, tr_dir):
            app.installTranslator(tr)
            keep.append(tr)  # GC されないよう参照を保持
    return keep


def _resource_path(rel: str) -> str:
    """同梱リソースの絶対パス（PyInstaller 凍結時は _MEIPASS 配下）。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _app_icon() -> QIcon:
    """アプリアイコン。assets/app_icon.(ico|png) があればそれを使う。"""
    for name in ("assets/app_icon.ico", "assets/app_icon.png"):
        p = _resource_path(name)
        if os.path.exists(p):
            icon = QIcon(p)
            if not icon.isNull():
                return icon
    # フォールバック: 画像が無ければ青い角丸＋TR を描画
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#2563eb"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(6, 6, 52, 52, 12, 12)
    p.setPen(QColor("#ffffff"))
    f = QFont()
    f.setBold(True)
    f.setPointSize(15)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "TR")
    p.end()
    return QIcon(pm)


def _allow_running_to_take_focus() -> None:
    """起動中のインスタンスが、ウィンドウを前面に出せるようにする。

    Windows は裏にいるプロセスが勝手に前面へ出ることを禁じている（タスク
    バーが点滅するだけ）。エクスプローラーから起動されたこのプロセスは前面に
    出る権利を持っているので、それを起動中のインスタンスに譲る。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        allow = ctypes.windll.user32.AllowSetForegroundWindow
        allow.argtypes = [wintypes.DWORD]
        allow(0xFFFFFFFF)   # ASFW_ANY
    except Exception:  # noqa: BLE001  前面に出なくてもファイルは開ける
        pass


def _send_to_running(path: str) -> bool:
    """既に起動中のインスタンスがあれば path を送り、True を返す。"""
    _allow_running_to_take_focus()
    sock = QLocalSocket()
    sock.connectToServer(_SERVER_NAME)
    if not sock.waitForConnected(300):
        return False
    sock.write((path or "").encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        sock.waitForDisconnected(1000)
    return True


def _start_server(window: MainWindow) -> QLocalServer:
    """このインスタンスを主インスタンスとして待ち受ける。"""
    QLocalServer.removeServer(_SERVER_NAME)  # 前回クラッシュ等の残骸を掃除
    server = QLocalServer()
    server.listen(_SERVER_NAME)

    def _on_new_connection() -> None:
        conn = server.nextPendingConnection()
        if conn is None:
            return

        def _on_ready() -> None:
            path = bytes(conn.readAll()).decode("utf-8", "ignore").strip()
            window.activate_and_open(path)
            conn.disconnectFromServer()

        conn.readyRead.connect(_on_ready)

    server.newConnection.connect(_on_new_connection)
    return server


def main() -> int:
    app = QApplication(sys.argv)
    # 何より先に。ここから後の例外は必ずログに残る（--windowed の exe は
    # 標準出力の行き先が無く、入れておかないと落ちても何も残らない）。
    errorlog.install()

    # 引数の相対パスは CWD を移す前に解決しておく。
    file_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if file_arg:
        file_arg = os.path.abspath(file_arg)
    # 起動したフォルダを掴んだままにしない。エクスプローラから PDF を開くと
    # CWD がその PDF のフォルダになり、Windows ではそのフォルダを削除/移動
    # できなくなる（トレイ常駐でプロセスが残る分、症状が長引く）。
    storage.ensure_safe_cwd()

    # 既に起動中なら、そのインスタンスにパスを渡して即終了（同一ウィンドウのタブで開く）
    if _send_to_running(file_arg):
        return 0

    app.setApplicationName("TriV-Reader")
    app.setOrganizationName("trivreader")
    app.setApplicationDisplayName("TriV-Reader")
    app.setWindowIcon(_app_icon())
    # ウィンドウを閉じてもアプリは終了させない（トレイ常駐＝次回を高速化）。
    # 実際の終了は MainWindow 側で QApplication.quit() を明示的に呼ぶ。
    app.setQuitOnLastWindowClosed(False)
    app._jp_translators = _install_japanese(app)  # 標準ボタン等を日本語化
    # 保存済みのテーマで最初から描く。ライトで作ってから切り替えると、
    # 起動のたびに白い画面が一瞬見えるうえ、全ウィジェットの再スタイルで
    # 起動が 50ms ほど遅くなる。
    try:
        _dark = storage.make_settings().value("dark", False, bool)
    except Exception:  # noqa: BLE001  設定が読めなければライトで出す
        _dark = False
    theme.apply_theme(app, dark=bool(_dark))

    window = MainWindow()
    window.show()
    app._single_server = _start_server(window)  # GC されないよう参照を保持

    # 前回開いていたファイルを復元（設定が有効なときだけ）
    window.restore_session()
    # コマンドライン引数で PDF を渡されたら開く
    if file_arg:
        window.open_path(file_arg)
    # 起動時の更新確認（設定が有効なときだけ。通信は別スレッド）
    window.check_update_on_startup()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
