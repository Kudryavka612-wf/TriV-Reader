"""設定ダイアログと設定値の既定。

これまで表示メニュー・ヘルプメニュー・トレイメニューに散らばっていた設定を
1か所に集約し、一般的な PDF リーダーが持つ項目（起動時の復元、既定の注釈
スタイル、保存時のバックアップ、更新の自動確認など）を足したもの。

既定値は DEFAULTS に集約し、読み出しは必ず value() を通すこと。QSettings は
ポータブル時に INI（＝値がすべて文字列）になるため、型を指定せずに読むと
bool/int が壊れる。
"""
from __future__ import annotations

import os
import subprocess

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import ocr, storage

# 既定値。キーは QSettings のキーと 1:1。型は value() の変換にも使う。
DEFAULTS: dict[str, object] = {
    # --- 全般 ---
    "startup/restore_session": False,  # 前回開いていたファイルを開き直す
    "tray/resident": True,             # 閉じてもトレイに常駐
    "confirm_close": True,             # ウィンドウを閉じるときに尋ねる
    "recent/limit": 8,                 # 最近使ったファイルの記憶件数
    "gesture/enabled": True,           # 右ドラッグのマウスジェスチャー
    "gesture/up": "first_page",        # ジェスチャーの割り当て（向き→操作）
    "gesture/down": "close_tab",
    "gesture/left": "prev_tab",
    "gesture/right": "next_tab",
    # --- 表示 ---
    "dark": False,
    "default_zoom_mode": "fit",
    "view/facing_default": False,      # 見開きで開く
    "compact": False,
    "page_dock_visible": True,
    "toc_dock_visible": True,
    "annot_dock_visible": True,
    # --- 注釈 ---
    "annot/author": "",                # 注釈の作成者名（空なら書き込まない）
    "annot/color": "#ffdb2e",   # 既存の初期色（黄色のハイライト）に合わせる
    "annot/width": 2,
    "annot/opacity": 100,
    "annot/text_size": 14,
    # --- 保存 ---
    "confirm_overwrite": True,
    "save/backup": False,              # 上書き前に .bak を残す
    "save/autosave_min": 0,            # 0 = 自動保存しない
    # --- 更新 ---
    "startup/check_update": False,     # 起動時に更新を確認
    "update_url": "",
    # --- 詳細 ---
    "ocr/lang": "jpn+eng",
}

# マウスジェスチャーに割り当てられる操作（設定値 → 表示名）。
# 実際の処理は MainWindow._on_gesture が持つ。
GESTURE_ACTIONS = {
    "close_tab": "タブを閉じる",
    "prev_tab": "前のタブ",
    "next_tab": "次のタブ",
    "first_page": "先頭ページ",
    "last_page": "最終ページ",
    "prev_page": "前のページ",
    "next_page": "次のページ",
    "open": "ファイルを開く",
    "none": "何もしない",
}

# 設定に出す向きの並びと見出し
GESTURE_DIRECTIONS = [
    ("up", "↑ 上へ"),
    ("down", "↓ 下へ"),
    ("left", "← 左へ"),
    ("right", "→ 右へ"),
]

# 「開いた時の表示倍率」の選択肢（MainWindow のメニューと同じ並び）
ZOOM_CHOICES = [
    ("fit", "幅に合わせる"),
    ("fit_height", "縦に合わせる"),
    ("remember", "前回の倍率を維持"),
    ("50", "50%"),
    ("75", "75%"),
    ("100", "100%"),
    ("125", "125%"),
    ("150", "150%"),
    ("200", "200%"),
]


def value(settings, key: str):
    """既定値の型で設定を読む（INI 保存でも bool/int が壊れないように）。"""
    default = DEFAULTS[key]
    try:
        return settings.value(key, default, type(default))
    except (TypeError, ValueError):
        return default


def reset_to_defaults(settings) -> None:
    """DEFAULTS にあるキーだけ消す（ウィンドウ位置や履歴は残す）。"""
    for key in DEFAULTS:
        settings.remove(key)


class SettingsDialog(QDialog):
    """タブ分けした設定ダイアログ。OK / 適用でその場のウィンドウへ反映する。"""

    def __init__(self, window) -> None:
        super().__init__(window)
        self.window = window
        self.settings = window.settings
        self.setWindowTitle("設定")
        self.setMinimumWidth(560)

        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_general(), "全般")
        self.tabs.addTab(self._build_view(), "表示")
        self.tabs.addTab(self._build_annot(), "注釈")
        self.tabs.addTab(self._build_save(), "保存")
        self.tabs.addTab(self._build_update(), "更新")
        self.tabs.addTab(self._build_advanced(), "詳細")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.RestoreDefaults
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("OK")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText("適用")
        buttons.button(
            QDialogButtonBox.StandardButton.RestoreDefaults).setText("初期値に戻す")
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            self._on_restore_defaults)

        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
        lay.addWidget(buttons)
        self._load()

    @staticmethod
    def _left(*widgets) -> QWidget:
        """ウィジェットを左寄せに並べる（QFormLayout に直接置くと横に伸びる）。"""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        for w in widgets:
            row.addWidget(w)
        row.addStretch(1)
        return holder

    # --- タブの中身 ---------------------------------------------------
    def _build_general(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.cb_restore = QCheckBox("前回開いていたファイルを開き直す")
        self.cb_tray = QCheckBox("ウィンドウを閉じてもトレイに常駐する（次回の起動が速い）")
        self.cb_confirm_close = QCheckBox("ウィンドウを閉じるときに確認する")
        self.cb_gesture = QCheckBox("マウスジェスチャーを使う")
        self.cb_gesture.setToolTip("ページの上で右ボタンを押しながら動かします")
        self.cb_gesture.toggled.connect(self._sync_gesture_rows)
        self.gesture_combos: dict[str, QComboBox] = {}
        self.sp_recent = QSpinBox()
        self.sp_recent.setRange(0, 30)
        self.sp_recent.setSuffix(" 件")
        self.sp_recent.setToolTip("0 にすると履歴を残しません")
        self.sp_recent.setMaximumWidth(120)

        form.addRow("起動時", self.cb_restore)
        form.addRow("", self.cb_tray)
        form.addRow("", self.cb_confirm_close)
        form.addRow("マウス操作", self.cb_gesture)

        self.gesture_box = QGroupBox("ジェスチャーの割り当て")
        grid = QFormLayout(self.gesture_box)
        for key, arrow in GESTURE_DIRECTIONS:
            cmb = QComboBox()
            for action, label in GESTURE_ACTIONS.items():
                cmb.addItem(label, action)
            cmb.setMaximumWidth(200)
            self.gesture_combos[key] = cmb
            grid.addRow(arrow, cmb)
        form.addRow("", self.gesture_box)

        form.addRow("最近使ったファイル", self.sp_recent)

        btn_clear = QPushButton("履歴を消去")
        btn_clear.clicked.connect(self._clear_recent)
        btn_default = QPushButton("既定の PDF アプリにする")
        btn_default.clicked.connect(lambda: self.window.act_set_default_pdf.trigger())
        form.addRow("", self._left(btn_clear, btn_default))
        return w

    def _build_view(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.cmb_theme = QComboBox()
        self.cmb_theme.addItem("ライト", False)
        self.cmb_theme.addItem("ダーク", True)
        self.cmb_zoom = QComboBox()
        for key, label in ZOOM_CHOICES:
            self.cmb_zoom.addItem(label, key)
        self.cb_facing = QCheckBox("見開き（2ページ）で開く")
        self.cb_compact = QCheckBox("ツールバーをコンパクト表示にする")
        self.cb_page_dock = QCheckBox("ページ一覧")
        self.cb_toc_dock = QCheckBox("しおり")
        self.cb_annot_dock = QCheckBox("注釈一覧")

        form.addRow("テーマ", self.cmb_theme)
        form.addRow("開いた時の表示倍率", self.cmb_zoom)
        form.addRow("ページの並べ方", self.cb_facing)
        form.addRow("ツールバー", self.cb_compact)

        box = QGroupBox("起動時に表示するパネル")
        v = QVBoxLayout(box)
        v.addWidget(self.cb_page_dock)
        v.addWidget(self.cb_toc_dock)
        v.addWidget(self.cb_annot_dock)
        form.addRow(box)
        return w

    def _build_annot(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.ed_author = QLineEdit()
        self.ed_author.setPlaceholderText("空欄なら作成者を書き込みません")
        self.ed_author.setToolTip(
            "付箋やハイライトなどの注釈に記録される名前です（PDF の作成者欄）")
        self.btn_color = QPushButton()
        self.btn_color.setFixedWidth(120)
        self.btn_color.clicked.connect(self._pick_color)
        self._color = QColor(str(DEFAULTS["annot/color"]))
        self.sp_width = QSpinBox()
        self.sp_width.setRange(1, 20)
        self.sp_width.setMaximumWidth(120)
        self.sp_width.setSuffix(" px")
        self.sp_opacity = QSpinBox()
        self.sp_opacity.setRange(10, 100)
        self.sp_opacity.setMaximumWidth(120)
        self.sp_opacity.setSuffix(" %")
        self.sp_text = QSpinBox()
        self.sp_text.setRange(6, 96)
        self.sp_text.setMaximumWidth(120)
        self.sp_text.setSuffix(" pt")

        form.addRow("作成者名", self.ed_author)
        form.addRow("既定の色", self._left(self.btn_color))
        form.addRow("線の太さ", self.sp_width)
        form.addRow("不透明度", self.sp_opacity)
        form.addRow("文字サイズ", self.sp_text)
        form.addRow("", QLabel("※ ここで決めた値が、起動時のツールバーの初期値になります"))
        return w

    def _build_save(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.cb_confirm_overwrite = QCheckBox("上書き保存の前に確認する")
        self.cb_backup = QCheckBox("上書きの前に元のファイルを .bak として残す")
        self.sp_autosave = QSpinBox()
        self.sp_autosave.setRange(0, 60)
        self.sp_autosave.setSuffix(" 分ごと")
        self.sp_autosave.setSpecialValueText("しない")
        self.sp_autosave.setToolTip(
            "編集中のファイルを自動で上書き保存します（0 = しない）")
        self.sp_autosave.setMaximumWidth(140)

        form.addRow("上書き", self.cb_confirm_overwrite)
        form.addRow("", self.cb_backup)
        form.addRow("自動保存", self.sp_autosave)
        form.addRow("", QLabel("※ 自動保存は開いているファイル自身を書き換えます"))
        return w

    def _build_update(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.cb_check_update = QCheckBox("起動時に更新を確認する")
        self.ed_update_url = QLineEdit()
        self.ed_update_url.setPlaceholderText(
            "空欄なら既定の配布元（GitHub Releases）を使います")
        self.cb_telemetry = QCheckBox("未対応フォームの匿名レポートを送る")
        self.cb_telemetry.setToolTip(
            "入力値やファイル名は送りません。送信先が未設定のときは無効です。")

        form.addRow("更新", self.cb_check_update)
        form.addRow("更新の確認先 URL", self.ed_update_url)
        btn = QPushButton("今すぐ更新を確認")
        btn.clicked.connect(self._check_update_now)
        form.addRow("", self._left(btn))
        form.addRow("プライバシー", self.cb_telemetry)
        return w

    def _build_advanced(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.cmb_ocr = QComboBox()
        for label, code in ocr.LANGUAGE_CHOICES.items():
            self.cmb_ocr.addItem(label, code)

        form.addRow("OCR の既定言語", self.cmb_ocr)
        form.addRow("設定の保存先", QLabel(
            "ポータブル版: TriVReader.ini" if storage.is_portable()
            else "レジストリ（HKCU\\Software\\trivreader）"))
        form.addRow("データフォルダ", QLabel(storage.data_dir()))
        btn = QPushButton("データフォルダを開く")
        btn.clicked.connect(self._open_data_dir)
        form.addRow("", self._left(btn))
        return w

    # --- 読み書き -------------------------------------------------------
    def _load(self) -> None:
        s = self.settings
        self.cb_restore.setChecked(value(s, "startup/restore_session"))
        self.cb_tray.setChecked(value(s, "tray/resident"))
        self.cb_confirm_close.setChecked(value(s, "confirm_close"))
        self.cb_gesture.setChecked(value(s, "gesture/enabled"))
        for key, cmb in self.gesture_combos.items():
            idx = cmb.findData(value(s, f"gesture/{key}"))
            cmb.setCurrentIndex(idx if idx >= 0 else cmb.findData("none"))
        self._sync_gesture_rows(self.cb_gesture.isChecked())
        self.sp_recent.setValue(value(s, "recent/limit"))

        idx = self.cmb_theme.findData(value(s, "dark"))
        self.cmb_theme.setCurrentIndex(max(0, idx))
        idx = self.cmb_zoom.findData(value(s, "default_zoom_mode"))
        self.cmb_zoom.setCurrentIndex(max(0, idx))
        self.cb_facing.setChecked(value(s, "view/facing_default"))
        self.cb_compact.setChecked(value(s, "compact"))
        self.cb_page_dock.setChecked(value(s, "page_dock_visible"))
        self.cb_toc_dock.setChecked(value(s, "toc_dock_visible"))
        self.cb_annot_dock.setChecked(value(s, "annot_dock_visible"))

        self.ed_author.setText(value(s, "annot/author"))
        self._set_color(QColor(value(s, "annot/color")))
        self.sp_width.setValue(value(s, "annot/width"))
        self.sp_opacity.setValue(value(s, "annot/opacity"))
        self.sp_text.setValue(value(s, "annot/text_size"))

        self.cb_confirm_overwrite.setChecked(value(s, "confirm_overwrite"))
        self.cb_backup.setChecked(value(s, "save/backup"))
        self.sp_autosave.setValue(value(s, "save/autosave_min"))

        self.cb_check_update.setChecked(value(s, "startup/check_update"))
        self.ed_update_url.setText(value(s, "update_url"))
        from . import telemetry
        has_url = bool(self.window._telemetry_url())
        self.cb_telemetry.setEnabled(has_url)
        self.cb_telemetry.setChecked(
            has_url and telemetry.consent_state(s) == "yes")

        idx = self.cmb_ocr.findData(value(s, "ocr/lang"))
        self.cmb_ocr.setCurrentIndex(max(0, idx))

    def _store(self) -> None:
        s = self.settings
        s.setValue("startup/restore_session", self.cb_restore.isChecked())
        s.setValue("tray/resident", self.cb_tray.isChecked())
        s.setValue("confirm_close", self.cb_confirm_close.isChecked())
        s.setValue("gesture/enabled", self.cb_gesture.isChecked())
        for key, cmb in self.gesture_combos.items():
            s.setValue(f"gesture/{key}", cmb.currentData())
        s.setValue("recent/limit", self.sp_recent.value())

        s.setValue("dark", self.cmb_theme.currentData())
        s.setValue("default_zoom_mode", self.cmb_zoom.currentData())
        s.setValue("view/facing_default", self.cb_facing.isChecked())
        s.setValue("compact", self.cb_compact.isChecked())
        s.setValue("page_dock_visible", self.cb_page_dock.isChecked())
        s.setValue("toc_dock_visible", self.cb_toc_dock.isChecked())
        s.setValue("annot_dock_visible", self.cb_annot_dock.isChecked())

        s.setValue("annot/author", self.ed_author.text().strip())
        s.setValue("annot/color", self._color.name())
        s.setValue("annot/width", self.sp_width.value())
        s.setValue("annot/opacity", self.sp_opacity.value())
        s.setValue("annot/text_size", self.sp_text.value())

        s.setValue("confirm_overwrite", self.cb_confirm_overwrite.isChecked())
        s.setValue("save/backup", self.cb_backup.isChecked())
        s.setValue("save/autosave_min", self.sp_autosave.value())

        s.setValue("startup/check_update", self.cb_check_update.isChecked())
        s.setValue("update_url", self.ed_update_url.text().strip())
        if self.cb_telemetry.isEnabled():
            from . import telemetry
            telemetry.set_consent(s, self.cb_telemetry.isChecked())

        s.setValue("ocr/lang", self.cmb_ocr.currentData())

    def apply(self) -> None:
        self._store()
        self.window.apply_preferences()

    def _on_ok(self) -> None:
        self.apply()
        self.accept()

    def _on_restore_defaults(self) -> None:
        if QMessageBox.question(
            self, "初期値に戻す",
            "設定を初期値に戻します。よろしいですか？\n"
            "（最近使ったファイルやウィンドウ位置は消えません）",
        ) != QMessageBox.StandardButton.Yes:
            return
        reset_to_defaults(self.settings)
        self._load()
        self.window.apply_preferences()

    def _sync_gesture_rows(self, on: bool) -> None:
        """ジェスチャーを使わない設定なら、割り当ての選択も触れなくする。"""
        self.gesture_box.setEnabled(bool(on))

    # --- 小物 -----------------------------------------------------------
    def _set_color(self, col: QColor) -> None:
        if not col.isValid():
            return
        self._color = col
        self.btn_color.setStyleSheet(
            f"background:{col.name()}; border:1px solid #888; min-height:22px;")
        self.btn_color.setText(col.name())

    def _pick_color(self) -> None:
        col = QColorDialog.getColor(self._color, self, "注釈の既定色")
        self._set_color(col)

    def _clear_recent(self) -> None:
        self.settings.setValue("recent", [])
        self.window._rebuild_recent_menu()
        QMessageBox.information(self, "履歴", "最近使ったファイルの履歴を消去しました。")

    def _check_update_now(self) -> None:
        self._store()
        self.window.check_for_update()

    def _open_data_dir(self) -> None:
        path = storage.data_dir()
        try:
            os.startfile(path)  # noqa: S606 (Windows 専用アプリ)
        except (AttributeError, OSError):
            subprocess.Popen(["explorer", path])  # noqa: S607
