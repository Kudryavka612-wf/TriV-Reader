"""TriV-Reader を .pdf に関連付ける（開発者向けCLI・管理者不要）。

使い方:
    python associate_pdf.py "C:\\path\\to\\TriVReader.exe"
    python associate_pdf.py            # 既定パス(%USERPROFILE%\\TriVReader_dist)を探す
    python associate_pdf.py --remove   # 関連付けを解除

「プログラムから開く」に TriV-Reader が追加されます。既定アプリにするには
Windows の［設定 > アプリ > 既定のアプリ］、または PDF を右クリック →
「プログラムから開く > 別のプログラムを選択」から TriV-Reader を選んでください。
（Windows 10/11 では既定アプリの強制設定はOS側で制限されているため）

実際の登録処理は viewer/associate.py にある（アプリ本体の「既定のPDFアプリに
設定」からも同じ関数を呼ぶ）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from viewer import associate


def main() -> int:
    args = sys.argv[1:]
    if "--remove" in args:
        associate.unregister()
        print("関連付けを解除しました。")
        return 0
    exe = next((a for a in args if a.lower().endswith(".exe")), None) or associate._default_exe()
    if not exe or not os.path.exists(exe):
        print("TriVReader.exe が見つかりません。先に build_exe.py でビルドするか、"
              "exe のパスを引数で指定してください。", file=sys.stderr)
        return 1
    exe = os.path.abspath(exe)
    associate.register(exe)
    print("関連付けを登録しました:", exe)
    print("PDF を右クリック →「プログラムから開く」に TriV-Reader が出ます。")
    print("既定にするには Windows の［既定のアプリ］から TriV-Reader を選択してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
