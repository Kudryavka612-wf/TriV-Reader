"""設定ダイアログのテスト（offscreen で実際に開いて反映まで確認する）。

「保存はされるが動作に効かない設定」を作らないための検証。設定を書き換えて
apply() し、ウィンドウ側の状態が実際に変わることまで見る。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from viewer import settings_dialog as prefs
from viewer.document import PdfDocument
from viewer.main_window import MainWindow
from viewer.settings_dialog import SettingsDialog

ROOT = os.path.dirname(os.path.abspath(__file__))
results: list[bool] = []


def check(cond: bool, label: str) -> bool:
    print(("OK  : " if cond else "NG  : ") + label)
    results.append(bool(cond))
    return bool(cond)


def build_sample(path: str, pages: int = 3) -> None:
    doc = fitz.open()
    for n in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 144), f"page {n + 1}", fontsize=24)
    doc.save(path)
    doc.close()


def make_window(app: QApplication, ini: str) -> MainWindow:
    """テスト用の INI に設定を書くウィンドウを作る（実環境を汚さない）。"""
    w = MainWindow()
    w.settings = QSettings(ini, QSettings.Format.IniFormat)
    w.resize(1000, 760)
    w.show()
    app.processEvents()
    return w


def test_dialog_opens(app: QApplication, w: MainWindow) -> SettingsDialog:
    dlg = SettingsDialog(w)
    app.processEvents()
    check(dlg.tabs.count() == 6, f"設定ダイアログに6タブある（{dlg.tabs.count()}）")
    labels = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    check(labels == ["全般", "表示", "注釈", "保存", "更新", "詳細"], f"タブ構成 {labels}")
    return dlg


def test_menu_entry(w: MainWindow) -> None:
    """設定へ辿り着く導線（リボン・メニュー・ショートカット）があるか。"""
    from PySide6.QtWidgets import QMenu, QToolButton

    texts = []
    for menu in w.findChildren(QMenu):
        texts += [a.text() for a in menu.actions()]
    check("設定…" in texts, "ファイルメニューに設定がある")
    # メニューバーは setMenuWidget(ribbon) で隠れているので、リボンが本命の導線
    buttons = [b for b in w.ribbon.findChildren(QToolButton)
               if b.defaultAction() is w.act_settings]
    check(bool(buttons), f"リボンに設定ボタンがある（{len(buttons)}個）")
    check(w.act_settings.shortcut().toString() == "Ctrl+,",
          f"ショートカット Ctrl+, （{w.act_settings.shortcut().toString()}）")


def test_roundtrip(app: QApplication, w: MainWindow) -> None:
    """ダイアログで変えた値が保存され、ウィンドウに反映される。"""
    dlg = SettingsDialog(w)
    dlg.cmb_theme.setCurrentIndex(dlg.cmb_theme.findData(True))   # ダーク
    dlg.cmb_zoom.setCurrentIndex(dlg.cmb_zoom.findData("100"))
    dlg.cb_facing.setChecked(True)
    dlg.sp_recent.setValue(3)
    dlg.ed_author.setText("山田太郎")
    dlg.sp_width.setValue(7)
    dlg.sp_opacity.setValue(55)
    dlg.sp_text.setValue(28)
    dlg.cb_backup.setChecked(True)
    dlg.sp_autosave.setValue(2)
    dlg.cmb_ocr.setCurrentIndex(dlg.cmb_ocr.findData("eng"))
    dlg.apply()
    app.processEvents()

    s = w.settings
    check(prefs.value(s, "dark") is True, "テーマ設定が保存される")
    check(w.act_dark.isChecked(), "ダークテーマが即時反映される")
    check(prefs.value(s, "default_zoom_mode") == "100", "既定ズームが保存される")
    check(w.default_zoom_actions["100"].isChecked(), "既定ズームのメニューが同期する")
    check(w.act_facing.isChecked(), "見開き設定が即時反映される")
    check(PdfDocument.annot_author == "山田太郎", "注釈の作成者名が反映される")
    check(w.width_spin.value() == 7, "線の太さがツールバーに反映される")
    check(w.opacity_spin.value() == 55, "不透明度がツールバーに反映される")
    check(w.text_size_spin.value() == 28, "文字サイズがツールバーに反映される")
    check(w._annot_color.name() == prefs.value(s, "annot/color"), "注釈色が同期する")
    check(w._autosave_timer.isActive() and w._autosave_timer.interval() == 120000,
          f"自動保存タイマーが2分で動く（{w._autosave_timer.interval()}ms）")

    # 開き直しても同じ値が出る
    dlg2 = SettingsDialog(w)
    check(dlg2.sp_width.value() == 7 and dlg2.ed_author.text() == "山田太郎"
          and dlg2.cmb_ocr.currentData() == "eng", "開き直しても値が復元される")


def test_recent_limit(app: QApplication, w: MainWindow, root: str) -> None:
    """履歴の件数制限が効く。"""
    w.settings.setValue("recent/limit", 3)
    paths = []
    for n in range(5):
        p = os.path.join(root, f"recent{n}.pdf")
        build_sample(p, pages=1)
        paths.append(p)
        w._add_recent(p)
    recent = w.settings.value("recent", [], list) or []
    check(len(recent) == 3, f"履歴が3件に制限される（{len(recent)}件）")
    check(recent[0] == paths[-1], "最後に開いたファイルが先頭にくる")


def test_annot_author(app: QApplication, w: MainWindow, root: str) -> None:
    """設定した作成者名が、実際に PDF の注釈へ書き込まれる。"""
    pdf = os.path.join(root, "annot.pdf")
    build_sample(pdf, pages=1)
    PdfDocument.annot_author = "検証者"
    doc = PdfDocument()
    doc.open(pdf)
    doc.add_highlight(0, fitz.Rect(72, 100, 300, 130))
    doc.add_text_note(0, fitz.Point(100, 200), "メモ")
    out = os.path.join(root, "annot_out.pdf")
    doc.save_as(out)
    doc.close()

    with fitz.open(out) as chk:
        authors = [a.info.get("title", "") for a in chk[0].annots()]
    check(authors and all(a == "検証者" for a in authors),
          f"注釈に作成者名が入る（{authors}）")
    PdfDocument.annot_author = ""


def test_backup(app: QApplication, w: MainWindow, root: str) -> None:
    """バックアップ設定が有効なら上書き前に .bak が残る。"""
    pdf = os.path.join(root, "backup.pdf")
    build_sample(pdf, pages=2)
    w.settings.setValue("save/backup", True)
    w._backup_if_enabled(pdf)
    check(os.path.exists(pdf + ".bak"), "バックアップ(.bak)が作られる")

    os.remove(pdf + ".bak")
    w.settings.setValue("save/backup", False)
    w._backup_if_enabled(pdf)
    check(not os.path.exists(pdf + ".bak"), "設定オフならバックアップを作らない")


def test_autosave(app: QApplication, w: MainWindow, root: str) -> None:
    """自動保存が、変更のあるタブを実際に書き戻す。"""
    pdf = os.path.join(root, "autosave.pdf")
    build_sample(pdf, pages=1)
    w.open_path(pdf)
    app.processEvents()
    w.doc.add_highlight(0, fitz.Rect(72, 100, 300, 130))
    check(w.doc.modified, "（前提）注釈を足すと未保存になる")
    w._autosave_tick()
    check(not w.doc.modified, "自動保存で未保存フラグが下りる")
    with fitz.open(pdf) as chk:
        n = len(list(chk[0].annots()))
    check(n == 1, f"ファイルに注釈が書き込まれている（{n}件）")


def test_autosave_backup(app: QApplication, w: MainWindow, root: str) -> None:
    """自動保存は、そのファイルの初回だけ設定に関係なく .bak を残す。"""
    pdf = os.path.join(root, "autosave_bak.pdf")
    build_sample(pdf, pages=1)
    w.settings.setValue("save/backup", False)     # 手動保存のバックアップは切る
    w.settings.setValue("save/autosave_min", 1)
    w.apply_preferences()
    w.open_path(pdf)
    app.processEvents()

    w.doc.add_highlight(0, fitz.Rect(72, 100, 300, 130))
    w._autosave_tick()
    check(os.path.exists(pdf + ".bak"), "自動保存の初回に .bak が残る")
    with fitz.open(pdf + ".bak") as bak:
        n = len(list(bak[0].annots()))
    check(n == 0, f"バックアップは書き換え前の内容（注釈 {n} 件）")

    os.remove(pdf + ".bak")
    w.doc.add_highlight(0, fitz.Rect(72, 200, 300, 230))
    w._autosave_tick()
    check(not os.path.exists(pdf + ".bak"),
          "2回目以降は設定に従う（オフなら作らない）")
    w.settings.setValue("save/autosave_min", 0)
    w.apply_preferences()


def test_session(app: QApplication, w: MainWindow, root: str) -> None:
    """開いていたファイルを憶えて復元する。"""
    w.settings.setValue("startup/restore_session", True)
    w._save_session()
    saved = w.settings.value("session/open_files", [], list) or []
    check(bool(saved), f"開いているファイルを記録する（{len(saved)}件）")

    w2 = MainWindow()
    w2.settings = w.settings
    w2.restore_session()
    app.processEvents()
    check(w2.tabs.count() == len(saved), f"復元でタブが開く（{w2.tabs.count()}）")
    for i in range(w2.tabs.count()):
        t = w2.tabs.widget(i)
        if hasattr(t, "doc"):
            t.doc.close()
    w2.settings.setValue("startup/restore_session", False)


def test_restore_defaults(app: QApplication, w: MainWindow) -> None:
    """初期値に戻すで DEFAULTS の値へ戻る（履歴やウィンドウ位置は残す）。"""
    w.settings.setValue("recent", ["dummy.pdf"])
    prefs.reset_to_defaults(w.settings)
    check(prefs.value(w.settings, "annot/width") == 2, "初期値に戻る（線の太さ）")
    check(prefs.value(w.settings, "dark") is False, "初期値に戻る（テーマ）")
    check((w.settings.value("recent", [], list) or []) == ["dummy.pdf"],
          "履歴は初期化で消えない")


def main() -> int:
    app = QApplication(sys.argv)
    root = tempfile.mkdtemp(prefix="trv_prefs_")
    ini = os.path.join(root, "TriVReader.ini")
    try:
        w = make_window(app, ini)
        test_dialog_opens(app, w)
        test_menu_entry(w)
        test_roundtrip(app, w)
        test_recent_limit(app, w, root)
        test_annot_author(app, w, root)
        test_backup(app, w, root)
        test_autosave(app, w, root)
        test_autosave_backup(app, w, root)
        test_session(app, w, root)
        test_restore_defaults(app, w)
        for i in range(w.tabs.count()):
            t = w.tabs.widget(i)
            if hasattr(t, "doc"):
                t.doc.close()
    finally:
        os.chdir(ROOT)
        shutil.rmtree(root, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    raise SystemExit(main())
