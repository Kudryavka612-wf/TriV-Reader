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
    "startup/resume_mode": "ask",      # 前回見ていたページ（RESUME_CHOICES）
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
    # 色はハイライトとそれ以外で分ける。以前は 1 色（黄）を共有していたため
    # テキストや図形まで黄色で入った。旧キー annot/color は読まない。
    "annot/ink_color": "#000000",       # 文字・図形・ペン・線
    "annot/highlight_color": "#ffdb2e",  # ハイライト
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
    # --- フォーム ---
    "form/autocomplete": True,         # 以前に入れた内容を候補に出す
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


# 「前回見ていたページ」の扱い。以前は自動で飛ぶだけ（オン/オフ）だったが、
# 毎回勝手に戻られると困る、という声で「尋ねる」を既定にした。
RESUME_CHOICES = [
    ("ask", "開くたびにたずねる"),
    ("always", "自動で続きから表示する"),
    ("never", "常に先頭から表示する"),
]


def resume_mode(settings) -> str:
    """前回見ていたページの扱い（ask/always/never）。旧設定のオフは never。"""
    if settings.contains("startup/resume_mode"):
        mode = value(settings, "startup/resume_mode")
        return mode if mode in dict(RESUME_CHOICES) else "ask"
    legacy = settings.value("startup/resume_page", None)
    if legacy is not None and str(legacy).lower() in ("false", "0"):
        return "never"
    return "ask"


def value(settings, key: str):
    """既定値の型で設定を読む（INI 保存でも bool/int が壊れないように）。"""
    default = DEFAULTS[key]
    try:
        return settings.value(key, default, type(default))
    except (TypeError, ValueError):
        return default


#: もう使っていない設定キー（初期値に戻すときに一緒に消す）
LEGACY_KEYS = ("annot/color", "startup/resume_page")


def reset_to_defaults(settings) -> None:
    """DEFAULTS にあるキーだけ消す（ウィンドウ位置や履歴は残す）。"""
    for key in (*DEFAULTS, *LEGACY_KEYS):
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
        self.tabs.addTab(self._build_form(), "フォーム")
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
        self.cmb_resume = QComboBox()
        for key, label in RESUME_CHOICES:
            self.cmb_resume.addItem(label, key)
        self.cmb_resume.setToolTip(
            "ファイルごとに最後に見ていたページを憶えています。\n"
            "「たずねる」では先頭のまま開き、ページの上に［続きから表示］を出します")
        self.cmb_resume.setMaximumWidth(240)
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
        form.addRow("前回見ていたページ", self.cmb_resume)
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
        self.btn_color.clicked.connect(
            lambda: self._pick_color(self.btn_color, "文字・図形の既定色"))
        self.btn_hl_color = QPushButton()
        self.btn_hl_color.setFixedWidth(120)
        self.btn_hl_color.clicked.connect(
            lambda: self._pick_color(self.btn_hl_color, "ハイライトの既定色"))
        self._colors = {
            self.btn_color: QColor(str(DEFAULTS["annot/ink_color"])),
            self.btn_hl_color: QColor(str(DEFAULTS["annot/highlight_color"])),
        }
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
        form.addRow("文字・図形の色", self._left(self.btn_color))
        form.addRow("ハイライトの色", self._left(self.btn_hl_color))
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
        form.addRow("", QLabel(
            "※ 自動保存は開いているファイル自身を書き換えます\n"
            "　（元に戻せるよう、最初の1回は必ず .bak を残します）"))
        return w

    def _build_form(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.cb_autocomplete = QCheckBox("以前に入力した内容を候補に出す")
        self.cb_autocomplete.setToolTip(
            "氏名・住所・電話番号など、前に入れた内容を入力中に候補として出します")
        form.addRow("入力候補", self.cb_autocomplete)
        form.addRow("", QLabel(
            "※ マイナンバー・口座番号・パスワード・暗証番号などの欄と、\n"
            "　12 桁・16 桁の番号は覚えません"))
        self.lbl_history = QLabel()
        btn = QPushButton("入力候補の履歴を消去")
        btn.clicked.connect(self._clear_form_history)
        form.addRow("", self._left(btn, self.lbl_history))
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
        idx = self.cmb_resume.findData(resume_mode(s))
        self.cmb_resume.setCurrentIndex(max(0, idx))
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
        self._set_color(self.btn_color, QColor(value(s, "annot/ink_color")))
        self._set_color(self.btn_hl_color, QColor(value(s, "annot/highlight_color")))
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

        self.cb_autocomplete.setChecked(value(s, "form/autocomplete"))
        from . import form_history
        self.lbl_history.setText(f"（{form_history.history().count()} 件）")

    def _store(self) -> None:
        s = self.settings
        s.setValue("startup/restore_session", self.cb_restore.isChecked())
        s.setValue("startup/resume_mode", self.cmb_resume.currentData())
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

        annot_keys = ("annot/author", "annot/ink_color", "annot/highlight_color",
                      "annot/width", "annot/opacity", "annot/text_size")
        before = tuple(value(s, k) for k in annot_keys)
        s.setValue("annot/author", self.ed_author.text().strip())
        s.setValue("annot/ink_color", self._colors[self.btn_color].name())
        s.setValue("annot/highlight_color", self._colors[self.btn_hl_color].name())
        s.setValue("annot/width", self.sp_width.value())
        s.setValue("annot/opacity", self.sp_opacity.value())
        s.setValue("annot/text_size", self.sp_text.value())
        # 注釈の既定を変えたときだけ、作業中のスタイルを既定に合わせ直す
        self.annot_changed = before != tuple(value(s, k) for k in annot_keys)

        s.setValue("confirm_overwrite", self.cb_confirm_overwrite.isChecked())
        s.setValue("save/backup", self.cb_backup.isChecked())
        s.setValue("save/autosave_min", self.sp_autosave.value())

        s.setValue("startup/check_update", self.cb_check_update.isChecked())
        s.setValue("update_url", self.ed_update_url.text().strip())
        if self.cb_telemetry.isEnabled():
            from . import telemetry
            telemetry.set_consent(s, self.cb_telemetry.isChecked())

        s.setValue("ocr/lang", self.cmb_ocr.currentData())
        s.setValue("form/autocomplete", self.cb_autocomplete.isChecked())

    def apply(self) -> None:
        self._store()
        self.window.apply_preferences(annot_changed=self.annot_changed)

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
    def _set_color(self, btn: QPushButton, col: QColor) -> None:
        if not col.isValid():
            return
        self._colors[btn] = col
        # 黒など暗い色でも色名が読めるよう、文字色を地の明るさで切り替える
        fg = "#ffffff" if col.lightness() < 128 else "#000000"
        btn.setStyleSheet(
            f"background:{col.name()}; color:{fg}; border:1px solid #888;"
            " min-height:22px;")
        btn.setText(col.name())

    def _pick_color(self, btn: QPushButton, title: str) -> None:
        col = QColorDialog.getColor(self._colors[btn], self, title)
        self._set_color(btn, col)

    def _clear_recent(self) -> None:
        self.settings.setValue("recent", [])
        self.window._rebuild_recent_menu()
        QMessageBox.information(self, "履歴", "最近使ったファイルの履歴を消去しました。")

    def _clear_form_history(self) -> None:
        from . import form_history
        form_history.history().clear()
        self.lbl_history.setText("（0 件）")
        QMessageBox.information(self, "入力候補", "フォームの入力候補の履歴を消去しました。")

    def _check_update_now(self) -> None:
        # 確認に要るのは更新元だけ。_store() で全部の項目を確定すると、そのあとの
        # 「キャンセル」で取り消せなくなる
        self.settings.setValue("update_url", self.ed_update_url.text().strip())
        self.window.check_for_update()

    def _open_data_dir(self) -> None:
        path = storage.data_dir()
        try:
            os.startfile(path)  # noqa: S606 (Windows 専用アプリ)
        except (AttributeError, OSError):
            subprocess.Popen(["explorer", path])  # noqa: S607
