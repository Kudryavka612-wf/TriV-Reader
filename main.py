"""自作 PDF エディタ エントリポイント。

実行: python main.py [開きたい.pdf]
"""
from __future__ import annotations

import getpass
import os
import sys


def _selftest(out_path: str) -> None:
    """配布用 exe がそのまま動くかを確かめて、結果を out_path に書く。

    CI がビルドの後に `TriVReader.exe --selftest 結果.txt` で走らせる。凍結した
    exe でしか起きない失敗（同梱漏れ・依存ライブラリの版の違い）は、ソースの
    テストでは見つからない（v1.8.0 は起動直後に落ちた）。読み込みもここで
    行うので、読み込みで落ちても止まる画面を出さずに結果を書いて終わる。
    """
    import tempfile
    import traceback

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")
    lines, code = [], 1
    try:
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter
        from PySide6.QtWidgets import QApplication

        app = QApplication([sys.argv[0]])
        import fitz

        from viewer import svg_print
        from viewer.main_window import MainWindow
        lines.append(f"PyMuPDF {fitz.VersionBind}")

        path = os.path.join(tempfile.mkdtemp(prefix="trv_selftest_"), "selftest.pdf")
        doc = fitz.open()
        doc.new_page().insert_text((72, 100), "TriV-Reader 日本語の確認", fontsize=20,
                                   fontname="japan")
        doc.save(path)
        doc.close()

        w = MainWindow()
        w.open_path(path)
        app.processEvents()
        img = w.doc.render_page_image(0, zoom=1.0)
        lines.append(f"render {img.width()}x{img.height()}")
        page = w.doc._doc.load_page(0)
        canvas = QImage(200, 280, QImage.Format.Format_RGB888)
        canvas.fill(QColor("white"))
        p = QPainter(canvas)
        ok = svg_print.draw_svg(p, page.get_svg_image(text_as_path=True),
                                QRectF(0, 0, 200, 280))
        p.end()
        lines.append(f"vector print path {'ok' if ok else 'fell back'}")
        from py_mini_racer import MiniRacer
        lines.append(f"V8 {MiniRacer().eval('6 * 7')}")
        # 保存（パスワード・最適化）と、画像の再圧縮・傾き補正が使う部品。同梱漏れは
        # 使った人の画面で初めて分かるので、ここで読み込みまで確かめる。軽量版は
        # numpy / Pillow を同梱しない（lite.ini）ので、その 2 つは見ない。
        import pikepdf
        with pikepdf.open(path) as pk:
            lines.append(f"pikepdf {pikepdf.__version__} pages={len(pk.pages)}")
        from viewer import storage
        if not storage.is_lite():
            import numpy
            from PIL import Image
            Image.new("L", (4, 4)).tobytes()
            lines.append(f"numpy {numpy.__version__} / Pillow {Image.__version__}")
        w.doc.modified = False
        w._force_quit = True
        w.close()
        app.processEvents()
        lines.append("OK")
        code = 0
    except BaseException:  # noqa: BLE001  何で落ちても結果を書いて終わる
        lines.append(traceback.format_exc())
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os._exit(code)


if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "--selftest":
    _selftest(sys.argv[2])


def _startup_failed(detail: str) -> None:
    """起動の途中で失敗した（更新で入った版が壊れている等）。

    配布版なら、前の版に戻すか（控えが無ければ配布ページを開くか）を聞く。
    Qt や PyMuPDF が壊れていることもあるので、rollback は標準ライブラリだけで動く。
    """
    if not getattr(sys, "frozen", False):
        return   # ソースから動かしているときは、そのままエラーを出す
    try:
        from viewer import rollback
        from viewer.version import APP_VERSION
        rollback.offer(f"TriV-Reader {APP_VERSION} を起動できませんでした。",
                       APP_VERSION, detail)
    except Exception:  # noqa: BLE001
        return   # 知らせられなかったときだけ、いつものエラー画面に任せる
    # 戻すなら、このプロセスが終わってから書き戻して起動し直す。戻さないときも、
    # 内容はもう知らせて記録したので、英語の技術的なエラー画面は重ねて出さない
    os._exit(1)


try:
    from PySide6.QtCore import QLibraryInfo, Qt, QTimer, QTranslator
    from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication

    from viewer import errorlog, rollback, storage, theme
    from viewer.main_window import MainWindow
    from viewer.version import APP_VERSION
except Exception:  # noqa: BLE001
    import traceback
    _startup_failed(traceback.format_exc())
    raise

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
    # 空のパス（引数なしの再起動）は 0 バイトの書き込みになり、相手に何も届かず
    # ウィンドウが出てこない。改行を 1 つ送って「前面に出すだけ」を伝える
    sock.write((path or "\n").encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        sock.waitForDisconnected(1000)
    return True


_primary_mutex = None   # 主インスタンスの印（プロセスが終わるまで握る）


def _claim_primary() -> bool:
    """このプロセスが主インスタンスになれるか。なれたら、終わるまで印を握る。

    「起動中のインスタンスに接続できなければ自分が主」だけだと、エクスプローラーで
    PDF を複数選んで Enter したときのように、ほぼ同時に起動した複数のプロセスが
    全部「主」になる（先頭が待ち受けを始める前に、みな接続を試すため）。結果、
    ウィンドウもトレイのアイコンも選んだ数だけ出て、後から開いた PDF は最後に
    待ち受けを始めた 1 つにしか届かない。名前付きミューテックスで 1 つだけに決める。
    """
    global _primary_mutex
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k32.CreateMutexW(None, False, "Local\\" + _SERVER_NAME.replace("\\", "_"))
        if not handle:
            return True   # 印が作れないなら、これまで通り自分で動く
        if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS: ほかのプロセスが主
            k32.CloseHandle(handle)
            return False
        _primary_mutex = handle
        return True
    except Exception:  # noqa: BLE001  Windows 以外・取れないときは従来どおり
        return True


def _hand_over_to_starting(path: str, timeout: float = 20.0) -> bool:
    """起動の途中の主インスタンスが待ち受けを始めるのを待って、path を渡す。

    渡せたら True（このプロセスは終わる）。主が終わった／現れなかったなら、
    印を取り直して自分が主になる（False）。
    """
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _send_to_running(path):
            return True
        if _claim_primary():
            return False
        time.sleep(0.15)
    return False   # 主が固まっているときまで待たない（従来どおり自分でも動く）


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
    # 接続できなくても、ほぼ同時に起動した別のプロセスが主になりかけているかもしれない
    if not _claim_primary() and _hand_over_to_starting(file_arg):
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

    if getattr(sys, "frozen", False):
        # 起動が最後まで進んだかの記録（Python ごと落ちて例外にならない失敗に
        # 備える。2 回続けて進まなければ、前の版に戻すかを聞く）
        if rollback.begin_startup(APP_VERSION):
            return 0   # 前の版に戻す（終わったら書き戻して起動し直す）
        app.aboutToQuit.connect(rollback.startup_done)
    window = MainWindow()
    window.show()
    if getattr(sys, "frozen", False):
        QTimer.singleShot(2000, rollback.startup_done)
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
    try:
        _code = main()
    except Exception:  # noqa: BLE001
        import traceback
        _startup_failed(traceback.format_exc())
        raise
    raise SystemExit(_code)
