"""ライト/ダークのUIテーマ（配色トークン＋アプリ全体のスタイルシート）。

色は下の _LIGHT / _DARK のトークンだけを直接いじれば全体に効く。個々の
ウィジェットに setStyleSheet を撒くとテーマ切替で取り残されるため、原則
ここに書く（objectName で引っかける）。

デザインの方針:
- 面（surface）とホバーの階調で情報の層を作り、枠線は極力細く薄く。
- ホバーはグレーではなくアクセント色を薄く敷く（今どきの見え方＋押せる感）。
- 角丸は 6/8/10px の 3 段階。小さい部品ほど小さい角丸。
- 選択中のタブは下線（インジケータ）で示す。塗り潰しは操作ボタンだけに使う。
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

# UI フォント。Latin/数字は Windows 11 の標準（Segoe UI Variable）、かなは
# Yu Gothic UI に落ちる（Qt は字形が無いファミリを飛ばして次を使う）。
UI_FONT_FAMILIES = [
    "Segoe UI Variable Text", "Yu Gothic UI", "Meiryo UI", "Segoe UI",
]

# テーマごとの色トークン
_LIGHT = {
    "bg": "#f4f5f7",          # ウィンドウ地。面より一段沈める
    "surface": "#ffffff",     # カード・入力欄・メニューなどの面
    "surface_alt": "#eef0f4",  # 交互行・押し込み・薄い面
    "border": "#e3e6ec",      # 通常の境界（極薄）
    "border_strong": "#c9cfd8",  # 入力欄など、形を見せたい境界
    "text": "#161a21",
    "muted": "#69707d",       # 補足・見出し・無効
    "accent": "#2563eb",
    "accent_hover": "#1d4ed8",
    "accent_text": "#ffffff",
    "accent_soft": "rgba(37,99,235,0.10)",   # ホバー
    "accent_soft2": "rgba(37,99,235,0.18)",  # 選択・チェック
    "canvas": "#54585f",      # ページの周り（Acrobat 風の中間グレー）
    "canvas2": "#464a51",     # 下に向けてわずかに沈むグラデーション
    "paper": "#ffffff",       # 未描画ページのプレースホルダ
    "shadow": "0,0,0",        # ページの影（rgb）
    "shadow_a": 0.28,
}
_DARK = {
    "bg": "#1b1d21",
    "surface": "#25282d",
    "surface_alt": "#2e3238",
    "border": "#343941",
    "border_strong": "#454b55",
    "text": "#e6e9ef",
    "muted": "#98a0ad",
    "accent": "#4d8dfd",
    "accent_hover": "#6ea3ff",
    "accent_text": "#0d1117",
    "accent_soft": "rgba(77,141,253,0.16)",
    "accent_soft2": "rgba(77,141,253,0.28)",
    "canvas": "#191b1f",
    "canvas2": "#141619",
    "paper": "#2a2d33",
    "shadow": "0,0,0",
    "shadow_a": 0.55,
}


def _tokens(dark: bool) -> dict:
    return _DARK if dark else _LIGHT


def _palette(c: dict) -> QPalette:
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(c["bg"]))
    p.setColor(QPalette.ColorRole.WindowText, QColor(c["text"]))
    p.setColor(QPalette.ColorRole.Base, QColor(c["surface"]))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(c["surface_alt"]))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(c["surface"]))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(c["text"]))
    p.setColor(QPalette.ColorRole.Text, QColor(c["text"]))
    p.setColor(QPalette.ColorRole.Button, QColor(c["surface"]))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(c["text"]))
    p.setColor(QPalette.ColorRole.Highlight, QColor(c["accent"]))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(c["accent_text"]))
    p.setColor(QPalette.ColorRole.Link, QColor(c["accent"]))
    p.setColor(QPalette.ColorRole.Mid, QColor(c["muted"]))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(c["muted"]))
    grp = QPalette.ColorGroup.Disabled
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText,
                 QPalette.ColorRole.WindowText):
        p.setColor(grp, role, QColor(c["muted"]))
    return p


def _qss(c: dict) -> str:
    return f"""
    /* ── 土台 ─────────────────────────────────────────── */
    QMainWindow, QDialog {{ background: {c['bg']}; }}
    QWidget {{ color: {c['text']}; }}
    QLabel:disabled {{ color: {c['muted']}; }}

    /* ── リボン ───────────────────────────────────────── */
    #ribbon {{ background: {c['bg']}; }}
    #ribbon > QTabBar {{ background: transparent; }}
    #ribbon > QTabBar::tab {{
        background: transparent; color: {c['muted']};
        padding: 6px 16px 5px 16px; margin: 2px 1px 0 1px;
        border: 0; border-bottom: 2px solid transparent; border-radius: 0; }}
    #ribbon > QTabBar::tab:hover {{ color: {c['text']}; background: {c['accent_soft']}; }}
    #ribbon > QTabBar::tab:selected {{
        color: {c['accent']}; border-bottom: 2px solid {c['accent']};
        font-weight: 600; }}
    #ribbonGroupTitle {{ color: {c['muted']}; font-size: 11px; }}
    #ribbonSep {{ color: {c['border']}; margin: 6px 3px; }}
    #ribbon QToolButton {{
        padding: 3px 5px; border-radius: 6px; border: 1px solid transparent; }}
    #ribbon QToolButton:hover {{
        background: {c['accent_soft']}; border-color: {c['accent_soft2']}; }}
    #ribbon QToolButton:pressed {{ background: {c['accent_soft2']}; }}
    #ribbon QToolButton:checked {{
        background: {c['accent']}; color: {c['accent_text']}; border-color: {c['accent']}; }}
    #ribbon QScrollArea {{ background: transparent; border: 0; }}
    #ribbon QScrollArea > QWidget > QWidget {{ background: transparent; }}
    #ribbon QScrollBar:horizontal {{ height: 8px; background: transparent; }}
    #ribbon QScrollBar::handle:horizontal {{
        background: {c['border_strong']}; border-radius: 4px; min-width: 24px; }}
    #ribbon QScrollBar::add-line:horizontal, #ribbon QScrollBar::sub-line:horizontal {{
        width: 0px; }}

    /* ── 文書タブ ─────────────────────────────────────── */
    #docTabs::pane {{ border: 0; background: transparent; }}
    #docTabs > QTabBar {{ background: transparent; }}
    #docTabs > QTabBar::tab {{
        background: {c['surface_alt']}; color: {c['muted']};
        border: 1px solid transparent; border-bottom: 0;
        border-top-left-radius: 8px; border-top-right-radius: 8px;
        padding: 6px 10px 6px 12px; margin: 3px 2px 0 0; }}
    #docTabs > QTabBar::tab:hover {{ background: {c['accent_soft']}; color: {c['text']}; }}
    #docTabs > QTabBar::tab:selected {{
        background: {c['surface']}; color: {c['text']};
        border-color: {c['border']}; }}
    /* × は Fusion 標準アイコンだとダークで見えないため、自前の QToolButton
       （objectName=tabClose、mw_tools._TabCloseButton）を各タブに置いている */
    #tabClose {{ color: {c['muted']}; background: transparent; border: 0;
        border-radius: 7px; padding: 0; margin: 0; font-size: 11px; }}
    #tabClose:hover {{ background: {c['accent_soft2']}; color: {c['text']}; }}
    #tabClose:pressed {{ background: {c['accent']}; color: {c['accent_text']}; }}
    /* タブが収まらないときの左右スクロール矢印。既定の描画は背景と同化して
       ダークで消えるので、面と枠を与えて矢印の色も明示する */
    #docTabs > QTabBar::scroller {{ width: 44px; }}
    #docTabs > QTabBar QToolButton {{
        background: {c['surface_alt']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 6px;
        margin: 4px 1px 1px 1px; }}
    #docTabs > QTabBar QToolButton:hover {{ background: {c['accent_soft']}; }}
    #docTabs > QTabBar QToolButton:disabled {{ color: {c['border_strong']}; }}

    /* ── メニュー ─────────────────────────────────────── */
    QMenuBar {{ background: {c['bg']}; color: {c['text']}; border: 0; }}
    QMenuBar::item {{ padding: 5px 10px; background: transparent; border-radius: 6px; }}
    QMenuBar::item:selected {{ background: {c['accent_soft']}; }}
    QMenu {{ background: {c['surface']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 10px; padding: 6px; }}
    QMenu::item {{ padding: 7px 28px 7px 26px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {c['accent_soft2']}; color: {c['text']}; }}
    QMenu::item:disabled {{ color: {c['muted']}; }}
    QMenu::separator {{ height: 1px; background: {c['border']}; margin: 5px 10px; }}
    QMenu::indicator {{ width: 16px; height: 16px; left: 6px; }}

    /* ── ツールバー / ツールボタン ────────────────────── */
    QToolBar {{ background: {c['bg']}; border: 0; spacing: 3px; padding: 3px; }}
    QToolBar::separator {{ background: {c['border']}; width: 1px; margin: 5px 6px; }}
    QToolButton {{ color: {c['text']}; background: transparent;
        border: 1px solid transparent; border-radius: 6px; padding: 5px 9px; }}
    QToolButton:hover {{ background: {c['accent_soft']}; }}
    QToolButton:pressed {{ background: {c['accent_soft2']}; }}
    QToolButton:checked {{ background: {c['accent']}; color: {c['accent_text']}; }}
    QToolButton:disabled {{ color: {c['muted']}; }}
    QToolButton::menu-indicator {{ image: none; }}

    /* ── ボタン ───────────────────────────────────────── */
    QPushButton {{ background: {c['surface']}; border: 1px solid {c['border_strong']};
        border-radius: 8px; padding: 6px 16px; color: {c['text']}; }}
    QPushButton:hover {{ background: {c['accent_soft']}; border-color: {c['accent']}; }}
    QPushButton:pressed {{ background: {c['accent_soft2']}; }}
    QPushButton:disabled {{ color: {c['muted']}; border-color: {c['border']};
        background: {c['surface_alt']}; }}
    QPushButton:default {{ border-color: {c['accent']}; }}
    QPushButton#primary {{ background: {c['accent']}; color: {c['accent_text']};
        border: 0; padding: 9px 22px; font-weight: 600; border-radius: 8px; }}
    QPushButton#primary:hover {{ background: {c['accent_hover']}; }}
    QPushButton#primary:pressed {{ background: {c['accent_hover']}; }}

    /* ── 入力 ─────────────────────────────────────────── */
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
        background: {c['surface']}; border: 1px solid {c['border_strong']};
        border-radius: 8px; padding: 5px 8px; color: {c['text']};
        selection-background-color: {c['accent']};
        selection-color: {c['accent_text']}; }}
    QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
        border-color: {c['muted']}; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
    QPlainTextEdit:focus, QTextEdit:focus {{ border: 2px solid {c['accent']}; padding: 4px 7px; }}
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
        background: {c['surface_alt']}; color: {c['muted']}; }}
    QComboBox::drop-down {{ border: 0; width: 20px; }}
    QSpinBox::up-button, QSpinBox::down-button,
    QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
        subcontrol-origin: border; width: 17px; border: 0;
        background: transparent; border-radius: 4px; margin: 2px 3px; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-position: top right; }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-position: bottom right; }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover,
    QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
        background: {c['accent_soft2']}; }}
    QComboBox QAbstractItemView {{ background: {c['surface']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 8px; padding: 4px;
        selection-background-color: {c['accent_soft2']}; selection-color: {c['text']}; }}

    /* ── チェック / ラジオ ────────────────────────────── */
    QCheckBox, QRadioButton {{ spacing: 7px; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px;
        border: 1px solid {c['border_strong']}; background: {c['surface']}; }}
    QCheckBox::indicator {{ border-radius: 4px; }}
    QRadioButton::indicator {{ border-radius: 9px; }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {c['accent']}; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background: {c['accent']}; border-color: {c['accent']}; }}
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
        background: {c['surface_alt']}; border-color: {c['border']}; }}

    /* ── 一覧 ─────────────────────────────────────────── */
    QListWidget, QTreeWidget, QListView, QTreeView {{
        background: {c['surface']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 10px; outline: 0; padding: 3px; }}
    QListWidget::item, QTreeWidget::item {{ padding: 4px; border-radius: 6px; }}
    QListWidget::item:hover, QTreeWidget::item:hover {{ background: {c['accent_soft']}; }}
    QListWidget::item:selected, QTreeWidget::item:selected {{
        background: {c['accent_soft2']}; color: {c['text']}; }}
    QHeaderView::section {{ background: {c['surface_alt']}; color: {c['muted']};
        border: 0; border-bottom: 1px solid {c['border']}; padding: 5px 8px; }}

    /* ── ドック / 区切り ──────────────────────────────── */
    QDockWidget {{ color: {c['text']}; titlebar-close-icon: none; }}
    QDockWidget::title {{ background: {c['bg']}; color: {c['muted']};
        padding: 8px 10px; border-bottom: 1px solid {c['border']}; }}
    QSplitter::handle {{ background: {c['border']}; }}
    QSplitter::handle:hover {{ background: {c['accent']}; }}
    QFrame[frameShape="4"], QFrame[frameShape="5"] {{ color: {c['border']}; }}

    /* ── スクロールバー（細めのオーバーレイ風）──────── */
    QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['border_strong']};
        border-radius: 5px; min-height: 40px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['muted']}; }}
    QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {c['border_strong']};
        border-radius: 5px; min-width: 40px; }}
    QScrollBar::handle:horizontal:hover {{ background: {c['muted']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    /* ── スライダ / 進捗 ──────────────────────────────── */
    QSlider::groove:horizontal {{ height: 4px; background: {c['border_strong']};
        border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: {c['accent']}; width: 14px; height: 14px;
        border-radius: 7px; margin: -6px 0; border: 2px solid {c['surface']}; }}
    QSlider::handle:horizontal:hover {{ background: {c['accent_hover']}; }}
    QProgressBar {{ background: {c['surface_alt']}; border: 0; border-radius: 6px;
        height: 10px; text-align: center; color: {c['text']}; }}
    QProgressBar::chunk {{ background: {c['accent']}; border-radius: 6px; }}

    /* ── その他 ───────────────────────────────────────── */
    QGroupBox {{ border: 1px solid {c['border']}; border-radius: 10px;
        margin-top: 10px; padding: 10px 8px 8px 8px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px;
        color: {c['muted']}; }}
    QStatusBar {{ background: {c['bg']}; border-top: 1px solid {c['border']};
        color: {c['muted']}; }}
    QStatusBar::item {{ border: 0; }}
    QStatusBar QLabel {{ color: {c['muted']}; }}
    QToolTip {{ background: {c['surface']}; color: {c['text']};
        border: 1px solid {c['border_strong']}; border-radius: 6px; padding: 5px 7px; }}
    QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 8px; }}
    QTabBar::tab {{ background: transparent; color: {c['muted']}; padding: 6px 14px;
        border-radius: 7px; margin: 2px; }}
    QTabBar::tab:hover {{ background: {c['accent_soft']}; color: {c['text']}; }}
    QTabBar::tab:selected {{ background: {c['accent_soft2']}; color: {c['text']}; }}

    /* ── ウェルカム画面 ───────────────────────────────── */
    #welcomeCard {{ background: {c['surface']}; border: 1px solid {c['border']};
        border-radius: 16px; }}
    #welcomeTitle {{ font-size: 26px; font-weight: 700; color: {c['text']}; }}
    #welcomeSub {{ color: {c['muted']}; font-size: 13px; }}
    #welcomeHint {{ color: {c['muted']}; font-size: 12px; }}
    #recentTile {{ background: {c['surface_alt']}; border: 1px solid transparent;
        border-radius: 10px; text-align: left; }}
    #recentTile:hover {{ background: {c['accent_soft']}; border-color: {c['accent']}; }}
    #recentName {{ font-weight: 600; color: {c['text']}; }}
    #recentDir {{ color: {c['muted']}; font-size: 11px; }}
    """


def canvas_color(dark: bool) -> str:
    """ページ表示エリアの背景色（テーマ別）。"""
    return _tokens(dark)["canvas"]


def canvas_gradient(dark: bool) -> tuple[str, str]:
    """ページ表示エリアの背景グラデーション（上, 下）。"""
    c = _tokens(dark)
    return c["canvas"], c["canvas2"]


def paper_color(dark: bool) -> str:
    """未描画ページ（プレースホルダ）の紙の色。"""
    return _tokens(dark)["paper"]


def page_shadow(dark: bool) -> tuple[str, float]:
    """ページの落ち影の (rgb, 最大の濃さ)。"""
    c = _tokens(dark)
    return c["shadow"], c["shadow_a"]


def accent_color(dark: bool) -> str:
    return _tokens(dark)["accent"]


def apply_font(app: QApplication) -> None:
    """UI フォントを整える（欧文と数字を Windows 11 標準の字形に寄せる）。"""
    f: QFont = app.font()
    f.setFamilies(UI_FONT_FAMILIES)
    f.setHintingPreference(QFont.HintingPreference.PreferDefaultHinting)
    app.setFont(f)


def apply_theme(app: QApplication, dark: bool, force: bool = False) -> None:
    """アプリ全体のテーマを切り替える。

    setStyleSheet はアプリ内の全ウィジェットを作り直しに近い勢いで
    再スタイルするため、起動時に 2 回走らせると体感で分かるほど遅い。
    同じテーマを当て直すだけなら何もしない（force=True で強制）。
    """
    if not force and getattr(app, "_trv_theme_dark", None) is dark:
        return
    c = _tokens(dark)
    app.setStyle("Fusion")
    apply_font(app)
    app.setPalette(_palette(c))
    app.setStyleSheet(_qss(c))
    app._trv_theme_dark = dark
