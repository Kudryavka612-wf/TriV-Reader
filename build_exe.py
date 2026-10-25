"""TriV-Reader を単体の .exe にビルドする（PyInstaller）。

使い方:
    python build_exe.py            # 既定の出力先にビルド
    python build_exe.py --onefile  # 単一ファイル(.exe1個)でビルド
    python build_exe.py --portable # ポータブル版（設定/データを exe 隣に保存）
    python build_exe.py --lite     # 軽量版（OCR・傾き補正なし／numpy・Pillow非同梱）
    # 併用可: python build_exe.py --lite --portable

出力:
    <出力先>/TriVReader/TriVReader.exe   （onedir, 既定）
    <出力先>/TriVReader.exe             （--onefile 指定時）

既定の出力先は %USERPROFILE%\\TriVReader_dist です。
（プロジェクトが Documents 配下だとフォルダー保護で書き込めないため外に出力）
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def make_icon(path: str) -> None:
    """アプリ用 .ico を生成する。

    assets/app_icon.png があればそれを .ico に変換して使う。
    無ければ青角丸＋TR を描画する（フォールバック）。
    """
    from PIL import Image, ImageDraw, ImageFont

    sizes = [256, 128, 64, 48, 32, 16]

    src = os.path.join(HERE, "assets", "app_icon.png")
    if os.path.exists(src):
        img = Image.open(src).convert("RGBA")
        # 正方形でなければ中央寄せで正方形キャンバスに収める
        if img.width != img.height:
            side = max(img.width, img.height)
            canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
            img = canvas
        img.save(path, format="ICO", sizes=[(s, s) for s in sizes])
        return

    base = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    d.rounded_rectangle([20, 20, 236, 236], radius=44, fill=(37, 99, 235, 255))
    try:
        font = ImageFont.truetype("arialbd.ttf", 76)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
    text = "TR"
    box = d.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    d.text(((256 - tw) / 2 - box[0], (256 - th) / 2 - box[1]), text,
           font=font, fill=(255, 255, 255, 255))
    base.save(path, format="ICO", sizes=[(s, s) for s in sizes])


def main() -> int:
    onefile = "--onefile" in sys.argv
    lite = "--lite" in sys.argv
    name = "TriVReader_Lite" if lite else "TriVReader"
    # ライト版は出力先を分ける（通常版と共存できるように）
    out_root = os.path.join(os.path.expanduser("~"),
                            "TriVReader_Lite_dist" if lite else "TriVReader_dist")
    work = os.path.join(out_root, "_build")
    os.makedirs(work, exist_ok=True)

    icon_path = os.path.join(work, "trivreader.ico")
    make_icon(icon_path)
    print("icon:", icon_path)

    import PyInstaller.__main__ as pyi

    # 未使用の重いモジュールを除外して、サイズと起動を軽くする
    excludes = [
        "tkinter", "matplotlib", "scipy", "IPython", "pytest", "pandas",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
        "PySide6.QtQuickWidgets", "PySide6.Qt3DCore", "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets", "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
        "PySide6.QtDataVisualization", "PySide6.QtSql", "PySide6.QtTest",
        "PySide6.QtBluetooth", "PySide6.QtSensors", "PySide6.QtSerialPort",
        "PySide6.QtPositioning", "PySide6.QtNfc", "PySide6.QtWebSockets",
        "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets",
    ]
    if lite:
        # ライト版は OCR・傾き補正を使わないので numpy/Pillow を同梱しない（大幅減）
        excludes += ["numpy", "PIL", "Pillow"]
    args = [
        os.path.join(HERE, "main.py"),
        "--name", name,
        "--noconfirm",
        "--windowed",                 # コンソール非表示
        "--icon", icon_path,
        "--paths", HERE,              # viewer パッケージを解決
        "--workpath", work,
        "--specpath", work,
        "--distpath", os.path.join(out_root, "dist"),
    ]
    for mod in excludes:
        args += ["--exclude-module", mod]
    # py_mini_racer はネイティブ V8 バイナリをパッケージデータとして同梱しており、
    # PyInstaller の自動解析では拾われないため collect-all で明示的に含める。
    args += ["--collect-all", "py_mini_racer"]
    # 印刷はベクター（SVG 経由）で描く。QtSvg が入っていないと黙ってラスタに
    # フォールバックし、凍結ビルドだけ印刷が粗くなるので明示的に同梱する。
    args += ["--hidden-import", "PySide6.QtSvg"]
    # アイコン画像を同梱（実行時のウィンドウ/タスクバーアイコン用）
    assets_dir = os.path.join(HERE, "assets")
    if os.path.isdir(assets_dir):
        args += ["--add-data", f"{assets_dir}{os.pathsep}assets"]
    if onefile:
        args.append("--onefile")
    print("PyInstaller args:", " ".join(args))
    pyi.run(args)

    dist = os.path.join(out_root, "dist")
    exe_dir = dist if onefile else os.path.join(dist, name)
    exe = os.path.join(exe_dir, f"{name}.exe")

    # 関連付けファイル用の「書類」アイコンを exe の隣に置く（関連付け登録が参照）
    doc_ico = os.path.join(HERE, "assets", "pdf_doc.ico")
    if os.path.exists(doc_ico):
        import shutil
        shutil.copy2(doc_ico, os.path.join(exe_dir, "pdf_doc.ico"))

    if "--portable" in sys.argv:
        # exe 隣にマーカーを置くとアプリがポータブルモード（設定/データを隣に保存）
        with open(os.path.join(exe_dir, "portable.ini"), "w", encoding="utf-8") as f:
            f.write("[portable]\nenabled=1\n")
        print("ポータブルマーカーを作成しました（設定/データは exe 隣に保存されます）")
    if lite:
        with open(os.path.join(exe_dir, "lite.ini"), "w", encoding="utf-8") as f:
            f.write("[lite]\nenabled=1\n")
        print("ライト版マーカーを作成しました（OCR・傾き補正は無効）")

    _sign_if_requested(exe)

    print("\n=== 完了 ===")
    print("実行ファイル:", exe)
    return 0


def _sign_if_requested(exe: str) -> None:
    """--sign <PFX> が指定されていればコード署名する（任意）。

    例: python build_exe.py --sign C:\\path\\cert.pfx
        パスワードは環境変数 CODESIGN_PFX_PASSWORD（互換: TRIVREADER_PFX_PW）で渡す。
    自己署名証明書（社内配布向け）でも SmartScreen の「不明な発行元」を
    抑制できます（証明書を配布先の「信頼されたルート/発行元」に入れた場合）。
    署名の中身は sign_release.py（CI の自動署名と同じ）。
    """
    if "--sign" not in sys.argv:
        return
    i = sys.argv.index("--sign")
    pfx = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
    if not pfx or not os.path.exists(pfx):
        print("署名スキップ: --sign の後に PFX ファイルのパスを指定してください")
        return
    import sign_release

    pw = os.environ.get("CODESIGN_PFX_PASSWORD") or os.environ.get("TRIVREADER_PFX_PW", "")
    try:
        sign_release.sign([exe], pfx, pw)
        print("コード署名しました:", exe)
    except Exception as exc:  # noqa: BLE001
        print("署名に失敗:", exc)


if __name__ == "__main__":
    raise SystemExit(main())
