"""メインウィンドウ。ツールバー・サムネイル・ページビューを束ねる。"""
from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
)
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTabWidget,
)

from . import ocr, page_view, theme
from .annot_list import AnnotListDock
from .bookmarks import TocDock, TocEditDialog
from .document import PdfDocument
from .organizer import PageOrganizer
from .page_view import PageView
from .thumbnail_list import ThumbnailList
from .welcome import WelcomeWidget

ZOOM_STEP = 1.25


def _parse_ranges(text: str, page_count: int) -> list[int]:
    """"1-3,5,8" のような 1 始まりの指定を 0 始まりのインデックス列にする。

    順序は入力どおり保持する（並べ替え抽出に使えるよう）。
    """
    indices: list[int] = []
    for token in text.replace(" ", "").split(","):
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            start, end = int(a), int(b)
            step = 1 if end >= start else -1
            for p in range(start, end + step, step):
                indices.append(p - 1)
        else:
            indices.append(int(token) - 1)
    if not indices:
        raise ValueError("ページが指定されていません")
    for i in indices:
        if i < 0 or i >= page_count:
            raise ValueError(f"範囲外のページ番号です（1〜{page_count}）")
    return indices


def _find_acrobat() -> str | None:
    """Adobe Acrobat / Acrobat Reader の実行ファイルを探す（無ければ None）。

    レジストリの App Paths（インストーラが登録する正規の場所）を優先し、
    見つからなければ既定のインストール先を直接あたる。
    """
    import winreg

    for exe in ("Acrobat.exe", "AcroRd32.exe"):
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(
                    root,
                    rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}",
                )
                path, _type = winreg.QueryValueEx(key, None)
                winreg.CloseKey(key)
                path = os.path.expandvars(str(path)).strip('"')
                if path and os.path.exists(path):
                    return path
            except OSError:
                continue

    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates = [
        rf"{pf}\Adobe\Acrobat DC\Acrobat\Acrobat.exe",
        rf"{pf86}\Adobe\Acrobat DC\Acrobat\Acrobat.exe",
        rf"{pf}\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe",
        rf"{pf86}\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


class DocTab(QStackedWidget):
    """1 つの PDF を表すタブ。自前の文書・ビューワー・整理画面を持つ。"""

    def __init__(self) -> None:
        super().__init__()
        self.doc = PdfDocument()
        self.page_view = PageView()
        self.organizer = PageOrganizer()
        self.addWidget(self.page_view)   # index 0
        self.addWidget(self.organizer)   # index 1


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("TriV-Reader")
        self.resize(1180, 820)
        self.setAcceptDrops(True)
        from . import storage
        self.settings = storage.make_settings()  # 通常=レジストリ / ポータブル=INI

        # タブが無いとき用の空状態（属性アクセスを安全にする）
        self._empty_doc = PdfDocument()
        self._spare_view = PageView()
        self._spare_organizer = PageOrganizer()

        self.welcome = WelcomeWidget()
        self.welcome.open_requested.connect(self.open_file)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        # ウェルカム ⇔ タブ群 を切り替える中央スタック
        self.center = QStackedWidget()
        self.center.addWidget(self.welcome)  # 0
        self.center.addWidget(self.tabs)     # 1
        self.center.setCurrentWidget(self.welcome)
        self.setCentralWidget(self.center)

        self.thumbnails = ThumbnailList()
        dock = QDockWidget("ページ", self)
        dock.setWidget(self.thumbnails)
        dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.page_dock = dock  # 表示メニューから再表示できるよう参照を保持

        # しおり（目次）ドック：ページドックとタブで重ねる
        self.toc_dock = TocDock(self)
        self.toc_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.toc_dock)
        self.tabifyDockWidget(dock, self.toc_dock)

        # 注釈一覧ドック：同じ左側にタブで重ねる
        self.annot_dock = AnnotListDock(self)
        self.annot_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.annot_dock)
        self.tabifyDockWidget(self.toc_dock, self.annot_dock)
        dock.raise_()

        # ドックの方針: 基本はウィンドウ内。×で閉じられる。外窓（フローティング）は
        # ドラッグで外へ出したときだけ。再表示時は必ずウィンドウ内に戻す。
        feat = (QDockWidget.DockWidgetFeature.DockWidgetClosable
                | QDockWidget.DockWidgetFeature.DockWidgetMovable
                | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        for d in (dock, self.toc_dock, self.annot_dock):
            d.setFeatures(feat)
            d.setFloating(False)  # 起動時は必ずウィンドウ内
            # 非表示→再表示（表示メニュー等）のとき、外窓のままなら中へ戻す
            d.visibilityChanged.connect(
                lambda vis, dd=d: (vis and dd.isFloating() and dd.setFloating(False)))

        self.page_label = QLabel("  ページ: - / -  ")
        self.statusBar().addPermanentWidget(self.page_label)

        self._build_toolbar()
        self._build_annotation_toolbar()
        self._build_search_toolbar()
        self._build_menubar()
        self._tighten_chrome()  # ツールバー/メニューの余白を詰める
        self._connect()
        self._polish_actions()
        self._apply_lite_mode()
        self._update_actions()
        self._restore_settings()
        self._force_quit = False
        self._tray_notified = False
        self._setup_tray()
        self._build_ribbon()  # Foxit 風リボンを最上部に（従来のバーは隠す）

    # --- アクティブタブへのアクセス（プロパティ） ---------------------
    def _active_tab(self) -> "DocTab | None":
        w = self.tabs.currentWidget()
        return w if isinstance(w, DocTab) else None

    @property
    def doc(self) -> PdfDocument:
        t = self._active_tab()
        return t.doc if t else self._empty_doc

    @property
    def page_view(self) -> PageView:
        t = self._active_tab()
        return t.page_view if t else self._spare_view

    @property
    def organizer(self) -> PageOrganizer:
        t = self._active_tab()
        return t.organizer if t else self._spare_organizer

    # --- UI 構築 --------------------------------------------------------
    def _build_toolbar(self) -> None:
        tb = self.addToolBar("メイン")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.main_toolbar = tb

        self.act_open = QAction("開く", self)
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        tb.addAction(self.act_open)
        tb.addSeparator()

        self.act_prev = QAction("◀ 前へ", self)
        self.act_prev.setShortcut(Qt.Key.Key_PageUp)
        self.act_next = QAction("次へ ▶", self)
        self.act_next.setShortcut(Qt.Key.Key_PageDown)
        tb.addAction(self.act_prev)
        tb.addAction(self.act_next)
        tb.addSeparator()

        self.act_zoom_in = QAction("＋ 拡大", self)
        self.act_zoom_in.setShortcut(QKeySequence.StandardKey.ZoomIn)
        self.act_zoom_out = QAction("－ 縮小", self)
        self.act_zoom_out.setShortcut(QKeySequence.StandardKey.ZoomOut)
        self.act_fit = QAction("幅に合わせる", self)
        self.act_fit_height = QAction("縦に合わせる", self)
        tb.addAction(self.act_zoom_out)
        tb.addAction(self.act_zoom_in)
        tb.addAction(self.act_fit)
        tb.addAction(self.act_fit_height)
        # ズームスライダー＋プリセット
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(25, 400)   # 25%〜400%
        self.zoom_slider.setValue(100)
        self.zoom_slider.setFixedWidth(120)
        tb.addWidget(self.zoom_slider)
        self.zoom_combo = QComboBox()
        self.zoom_combo.addItems(["50%", "75%", "100%", "125%", "150%", "200%",
                                  "幅に合わせる", "縦に合わせる"])
        self.zoom_combo.setCurrentText("100%")
        tb.addWidget(self.zoom_combo)
        tb.addSeparator()

        self.act_facing = QAction("見開き", self)
        self.act_facing.setCheckable(True)
        tb.addAction(self.act_facing)
        self.act_print = QAction("印刷", self)
        self.act_print.setShortcut(QKeySequence.StandardKey.Print)
        tb.addAction(self.act_print)
        self.act_open_acrobat = QAction("Acrobatで開く", self)
        tb.addAction(self.act_open_acrobat)
        self.act_set_default_pdf = QAction("既定のPDFアプリに設定", self)
        tb.addAction(self.act_set_default_pdf)
        tb.addSeparator()

        self.act_rotate_left = QAction("⟲ 左回転", self)
        self.act_rotate_left.setShortcut("Ctrl+L")
        self.act_rotate_right = QAction("⟳ 右回転", self)
        self.act_rotate_right.setShortcut("Ctrl+R")
        tb.addAction(self.act_rotate_left)
        tb.addAction(self.act_rotate_right)
        tb.addSeparator()

        self.act_save = QAction("上書き保存", self)
        self.act_save.setShortcut(QKeySequence.StandardKey.Save)
        self.act_save_as = QAction("名前を付けて保存", self)
        self.act_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
        tb.addAction(self.act_save)
        tb.addAction(self.act_save_as)
        tb.addSeparator()

        self.act_organize = QAction("ページ整理", self)
        self.act_organize.setCheckable(True)
        tb.addAction(self.act_organize)
        self.act_form = QAction("フォーム入力", self)
        self.act_form.setCheckable(True)
        self.act_form.setToolTip("入力欄付きPDFの各欄に文字を入力できるようにする")
        tb.addAction(self.act_form)
        tb.addSeparator()

        self.act_delete = QAction("ページ削除", self)
        self.act_merge = QAction("PDFを統合", self)
        self.act_extract = QAction("抽出", self)
        self.act_split = QAction("分割", self)
        tb.addAction(self.act_delete)
        tb.addAction(self.act_merge)
        tb.addAction(self.act_extract)
        tb.addAction(self.act_split)
        tb.addSeparator()

        self.act_to_images = QAction("PDF→画像", self)
        self.act_from_images = QAction("画像→PDF", self)
        self.act_add_images = QAction("画像をページ追加", self)
        tb.addAction(self.act_to_images)
        tb.addAction(self.act_from_images)
        tb.addAction(self.act_add_images)
        tb.addSeparator()

        self.act_ocr = QAction("文字認識(OCR)", self)
        tb.addAction(self.act_ocr)

        # メニュー専用アクション（ツールバーには出さない）
        self.act_add_bookmark = QAction("現在ページをしおりに追加", self)
        self.act_edit_toc = QAction("目次を編集…", self)
        self.act_compress = QAction("最適化して保存…", self)
        self.act_export_pdfa = QAction("PDF/A で書き出し…", self)
        self.act_protect = QAction("パスワードで保護して保存…", self)
        self.act_unlock = QAction("パスワードを解除して保存…", self)
        self.act_watermark = QAction("透かしを追加…", self)
        self.act_header_footer = QAction("ヘッダー/フッターを追加…", self)
        self.act_blank_page = QAction("白紙ページを挿入", self)
        self.act_duplicate_page = QAction("現在ページを複製", self)
        self.act_autocrop = QAction("全ページの余白を自動トリミング", self)
        self.act_metadata = QAction("文書プロパティ(メタデータ)…", self)
        self.act_export_text = QAction("テキストに書き出し(.txt)…", self)
        self.act_export_html = QAction("HTMLに書き出し…", self)
        self.act_batch = QAction("バッチ処理（複数PDF）…", self)
        self.act_diff = QAction("2つのPDFを比較…", self)
        self.act_deskew = QAction("傾き補正（スキャン）", self)
        self.act_check_update = QAction("更新を確認…", self)
        self.act_about = QAction("バージョン情報", self)
        self.act_set_update_url = QAction("更新元URLを設定…", self)
        self.act_report_govform = QAction("未対応フォーム機能を報告用に保存…", self)
        self.act_report_govform.setEnabled(False)
        self.act_telemetry_toggle = QAction("匿名レポートを送信する", self)
        self.act_telemetry_toggle.setCheckable(True)
        self.act_dark = QAction("ダークモード", self)
        self.act_dark.setCheckable(True)
        self.act_compact = QAction("コンパクト表示（アイコンのみ）", self)
        self.act_compact.setCheckable(True)
        self.act_copy = QAction("コピー", self)
        self.act_copy.setShortcut(QKeySequence.StandardKey.Copy)
        self.act_copy.setToolTip("選択したテキスト、または選択した注釈をコピー (Ctrl+C)")
        self.act_copy.setEnabled(False)
        self.act_paste = QAction("注釈を貼り付け", self)
        self.act_paste.setShortcut(QKeySequence.StandardKey.Paste)
        self.act_paste.setToolTip("コピーした注釈をこのページに貼り付け (Ctrl+V)")
        self._annot_clip = None  # コピーした注釈の内容（dict）
        self.addAction(self.act_copy)  # ショートカットを有効化

    def _build_annotation_toolbar(self) -> None:
        """注釈ツール用の2段目ツールバー。"""
        tb = self.addToolBar("注釈")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.annot_toolbar = tb

        self.tool_group = QActionGroup(self)
        self.tool_group.setExclusive(True)

        def add_tool(text: str, tool: str, checked: bool = False) -> QAction:
            act = QAction(text, self)
            act.setCheckable(True)
            act.setChecked(checked)
            act.setData(tool)
            self.tool_group.addAction(act)
            tb.addAction(act)
            return act

        self.act_tool_none = add_tool("選択", page_view.TOOL_NONE, checked=True)
        self.act_tool_highlight = add_tool("ハイライト", page_view.TOOL_HIGHLIGHT)
        self.act_tool_pen = add_tool("ペン", page_view.TOOL_PEN)
        self.act_tool_rect = add_tool("四角", page_view.TOOL_RECT)
        self.act_tool_underline = add_tool("下線", page_view.TOOL_UNDERLINE)
        self.act_tool_strike = add_tool("取消線", page_view.TOOL_STRIKEOUT)
        self.act_tool_line = add_tool("直線", page_view.TOOL_LINE)
        self.act_tool_arrow = add_tool("矢印", page_view.TOOL_ARROW)
        self.act_tool_circle = add_tool("円", page_view.TOOL_CIRCLE)
        self.act_tool_text = add_tool("テキスト", page_view.TOOL_TEXT)
        self.act_tool_note = add_tool("付箋", page_view.TOOL_NOTE)
        self.act_tool_redact = add_tool("墨消し", page_view.TOOL_REDACT)
        self.act_tool_crop = add_tool("トリミング", page_view.TOOL_CROP)
        self.act_tool_erase = add_tool("消しゴム", page_view.TOOL_ERASE)
        tb.addSeparator()

        # 色ボタン（現在色を表示）
        self.act_color = QAction("色", self)
        tb.addAction(self.act_color)
        self._annot_color = QColor(255, 219, 46)
        self._refresh_color_icon()

        # 線の太さ
        tb.addWidget(QLabel(" 太さ "))
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 20)
        self.width_spin.setValue(2)
        self.width_spin.setToolTip("線の太さ（ペン・四角・円・直線・矢印）")
        tb.addWidget(self.width_spin)
        tb.addSeparator()

        # 線種（実線/破線/点線）
        tb.addWidget(QLabel(" 線種 "))
        self.line_style_combo = QComboBox()
        for key, label in (("solid", "実線"), ("dashed", "破線"), ("dotted", "点線")):
            self.line_style_combo.addItem(label, key)
        self.line_style_combo.setToolTip("線種（四角・円・直線・矢印の枠線）")
        tb.addWidget(self.line_style_combo)

        # 矢印の始点/終点スタイル（直線・矢印ツール用）
        line_end_items = [
            ("none", "なし"), ("open_arrow", "矢印(開き)"), ("closed_arrow", "矢印(塗り)"),
            ("r_open_arrow", "矢印(開き・逆)"), ("r_closed_arrow", "矢印(塗り・逆)"),
            ("square", "四角"), ("circle", "丸"), ("diamond", "ひし形"),
            ("butt", "バット"), ("slash", "スラッシュ"),
        ]
        tb.addWidget(QLabel(" 始点 "))
        self.line_start_combo = QComboBox()
        for key, label in line_end_items:
            self.line_start_combo.addItem(label, key)
        self.line_start_combo.setToolTip("直線・矢印の始点の形状")
        tb.addWidget(self.line_start_combo)

        tb.addWidget(QLabel(" 終点 "))
        self.line_end_combo = QComboBox()
        for key, label in line_end_items:
            self.line_end_combo.addItem(label, key)
        self.line_end_combo.setToolTip("直線・矢印の終点の形状")
        tb.addWidget(self.line_end_combo)
        tb.addSeparator()

        # 塗りつぶし（四角/円用）
        self.act_fill_enabled = QAction("塗り", self)
        self.act_fill_enabled.setCheckable(True)
        tb.addAction(self.act_fill_enabled)
        self.act_fill_color = QAction("塗り色", self)
        tb.addAction(self.act_fill_color)
        self._fill_color = QColor(255, 255, 255)
        self._refresh_fill_icon()

        # 不透明度
        tb.addWidget(QLabel(" 不透明度 "))
        self.opacity_spin = QSpinBox()
        self.opacity_spin.setRange(10, 100)
        self.opacity_spin.setValue(100)
        self.opacity_spin.setSuffix("%")
        self.opacity_spin.setToolTip(
            "不透明度。新しく描く注釈に適用されるほか、選択中の注釈にも即反映されます\n"
            "（選択ツールで注釈をクリック → この値を変更）")
        tb.addWidget(self.opacity_spin)
        tb.addSeparator()

        # 文字サイズ（テキスト注釈用）
        tb.addWidget(QLabel(" 文字サイズ "))
        self.text_size_spin = QSpinBox()
        self.text_size_spin.setRange(6, 96)
        self.text_size_spin.setValue(14)
        self.text_size_spin.setSuffix("pt")
        self.text_size_spin.setToolTip(
            "テキスト注釈の文字サイズ（入力中や、テキスト注釈を選択中も変更可）")
        tb.addWidget(self.text_size_spin)
        tb.addSeparator()

        # フォント（テキスト注釈用）。リボンへ直接配置するためここでは作るだけ
        # （ツールバー経由で移設すると QWidgetAction の所有問題があるため）。
        self.text_font_combo = QComboBox()
        for key, label in (("helv", "ゴシック(標準)"), ("tiro", "明朝(Times)"),
                           ("cour", "等幅(Courier)")):
            self.text_font_combo.addItem(label, key)
        self.text_font_combo.setToolTip(
            "テキスト注釈のフォント（英数字に適用。日本語は標準フォントで描画）")

        # 入力コントロールが押しやすいよう最小幅を確保（リボン上で潰れない）
        self.opacity_spin.setMinimumWidth(58)
        self.text_size_spin.setMinimumWidth(56)
        self.width_spin.setMinimumWidth(46)
        self.text_font_combo.setMinimumWidth(104)

        # スタンプ（承認・済・日付など。選ぶとクリックした位置に置く）
        from PySide6.QtWidgets import QMenu, QToolButton
        R, G, B = (0.85, 0.12, 0.12), (0.10, 0.55, 0.15), (0.12, 0.30, 0.78)
        self._stamp_presets = [
            ("承認", R), ("確認", R), ("済", G), ("重要", R),
            ("㊙", R), ("✓", G), ("日付", B),
        ]
        self.stamp_btn = QToolButton()
        self.stamp_btn.setText("スタンプ ▾")
        self.stamp_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.stamp_btn.setToolTip("スタンプを選んで、ページ上のクリックした位置に配置")
        stamp_menu = QMenu(self.stamp_btn)
        for label, rgb in self._stamp_presets:
            act = stamp_menu.addAction(label)
            act.triggered.connect(
                lambda checked=False, t=label, c=rgb: self._place_stamp(t, c))
        self.stamp_btn.setMenu(stamp_menu)
        tb.addWidget(self.stamp_btn)
        tb.addSeparator()

        self.act_apply_redact = QAction("墨消しを適用", self)
        tb.addAction(self.act_apply_redact)
        self.act_clear_annots = QAction("このページの注釈を全消去", self)
        tb.addAction(self.act_clear_annots)

        # 注釈の元に戻す / やり直し
        self.act_undo = QAction("元に戻す", self)
        self.act_undo.setShortcut(QKeySequence.StandardKey.Undo)   # Ctrl+Z
        self.act_undo.setToolTip("注釈操作を元に戻す (Ctrl+Z)")
        tb.addAction(self.act_undo)
        self.act_redo = QAction("やり直し", self)
        # Ctrl+Y と Ctrl+Shift+Z の両方で効くようにする
        self.act_redo.setShortcuts([QKeySequence.StandardKey.Redo,
                                    QKeySequence("Ctrl+Y")])
        self.act_redo.setToolTip("注釈操作をやり直す (Ctrl+Y)")
        tb.addAction(self.act_redo)

    def _build_search_toolbar(self) -> None:
        """検索ポップアップ（Ctrl+F / 検索ボタンで表示される小窓）。"""
        from PySide6.QtWidgets import QDialog, QHBoxLayout, QToolButton

        self.act_search_prev = QAction("◀ 前", self)
        self.act_search_next = QAction("次 ▶", self)
        self.act_highlight_all = QAction("全てハイライト", self)

        pop = QDialog(self)
        pop.setWindowTitle("検索")
        pop.setWindowFlags(Qt.WindowType.Tool
                           | Qt.WindowType.WindowCloseButtonHint)
        row = QHBoxLayout(pop)
        row.setContentsMargins(8, 6, 8, 6)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("テキストを検索… (Enter)")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumWidth(220)
        row.addWidget(self.search_edit)
        for act in (self.act_search_prev, self.act_search_next):
            b = QToolButton()
            b.setDefaultAction(act)
            b.setAutoRaise(True)
            row.addWidget(b)
        self.search_count_label = QLabel("  0 件  ")
        row.addWidget(self.search_count_label)
        b = QToolButton()
        b.setDefaultAction(self.act_highlight_all)
        b.setAutoRaise(True)
        row.addWidget(b)
        self.search_popup = pop

        # ページ番号
        self.act_page_numbers = QAction("ページ番号を追加…", self)

        # Ctrl+F で検索ポップアップを表示
        self.act_find = QAction("検索", self)
        self.act_find.setShortcut(QKeySequence.StandardKey.Find)
        self.addAction(self.act_find)

    def _build_menubar(self) -> None:
        """全機能をメニューに整理（ツールバーの混雑緩和）。"""
        mb = self.menuBar()

        m_file = mb.addMenu("ファイル(&F)")
        m_file.addAction(self.act_open)
        self.recent_menu = QMenu("最近開いたファイル", self)
        m_file.addMenu(self.recent_menu)
        m_file.addSeparator()
        m_file.addAction(self.act_save)
        m_file.addAction(self.act_save_as)
        m_file.addSeparator()
        m_file.addAction(self.act_print)
        m_file.addAction(self.act_open_acrobat)
        m_file.addAction(self.act_set_default_pdf)
        m_file.addSeparator()
        m_file.addAction(self.act_compress)
        m_file.addAction(self.act_export_pdfa)
        m_file.addSeparator()
        m_file.addAction(self.act_protect)
        m_file.addAction(self.act_unlock)

        m_view = mb.addMenu("表示(&V)")
        m_view.addAction(self.act_dark)
        m_view.addAction(self.act_compact)
        m_view.addSeparator()
        # 左のページ一覧ドックの表示/非表示（消しても再表示できるように）
        self.act_toggle_pagedock = self.page_dock.toggleViewAction()
        self.act_toggle_pagedock.setText("ページ一覧を表示")
        m_view.addAction(self.act_toggle_pagedock)
        # しおり（目次）ドックの表示/非表示も表示メニューから
        act_toggle_toc = self.toc_dock.toggleViewAction()
        act_toggle_toc.setText("しおり一覧を表示")
        m_view.addAction(act_toggle_toc)
        # 注釈一覧ドックの表示/非表示
        act_toggle_annots = self.annot_dock.toggleViewAction()
        act_toggle_annots.setText("注釈一覧を表示")
        m_view.addAction(act_toggle_annots)
        m_view.addSeparator()
        # 注釈バーの表示/非表示（折りたたみ）
        self.act_toggle_annotbar = self.annot_toolbar.toggleViewAction()
        self.act_toggle_annotbar.setText("注釈バーを表示")
        m_view.addAction(self.act_toggle_annotbar)
        m_view.addSeparator()
        m_view.addAction(self.act_copy)
        m_view.addSeparator()
        m_view.addAction(self.act_prev)
        m_view.addAction(self.act_next)
        m_view.addSeparator()
        m_view.addAction(self.act_zoom_in)
        m_view.addAction(self.act_zoom_out)
        m_view.addAction(self.act_fit)
        m_view.addAction(self.act_fit_height)
        m_view.addSeparator()
        m_view.addAction(self.act_rotate_left)
        m_view.addAction(self.act_rotate_right)
        m_view.addSeparator()
        self._build_default_zoom_menu(m_view)

        m_pages = mb.addMenu("ページ(&P)")
        m_pages.addAction(self.act_organize)
        m_pages.addAction(self.act_form)
        m_pages.addSeparator()
        m_pages.addAction(self.act_delete)
        m_pages.addAction(self.act_merge)
        m_pages.addAction(self.act_extract)
        m_pages.addAction(self.act_split)
        m_pages.addSeparator()
        m_pages.addAction(self.act_blank_page)
        m_pages.addAction(self.act_duplicate_page)
        m_pages.addAction(self.act_autocrop)
        m_pages.addSeparator()
        m_pages.addAction(self.act_page_numbers)
        m_pages.addAction(self.act_header_footer)
        m_pages.addAction(self.act_watermark)

        m_annot = mb.addMenu("注釈(&A)")
        for act in self.tool_group.actions():
            m_annot.addAction(act)
        m_annot.addSeparator()
        m_annot.addAction(self.act_color)
        m_annot.addAction(self.act_apply_redact)
        m_annot.addAction(self.act_clear_annots)

        m_book = mb.addMenu("しおり(&B)")
        m_book.addAction(self.act_add_bookmark)
        m_book.addAction(self.act_edit_toc)
        m_book.addSeparator()
        m_book.addAction(self.toc_dock.toggleViewAction())

        m_conv = mb.addMenu("変換(&C)")
        m_conv.addAction(self.act_to_images)
        m_conv.addAction(self.act_from_images)
        m_conv.addAction(self.act_add_images)
        m_conv.addSeparator()
        m_conv.addAction(self.act_ocr)
        m_conv.addAction(self.act_deskew)
        m_conv.addSeparator()
        m_conv.addAction(self.act_export_text)
        m_conv.addAction(self.act_export_html)
        m_conv.addAction(self.act_metadata)
        m_conv.addSeparator()
        m_conv.addAction(self.act_batch)
        m_conv.addAction(self.act_diff)

        m_help = mb.addMenu("ヘルプ(&H)")
        m_help.addAction(self.act_check_update)
        m_help.addAction(self.act_set_update_url)
        m_help.addSeparator()
        m_help.addAction(self.act_report_govform)
        m_help.addAction(self.act_telemetry_toggle)
        m_help.addSeparator()
        m_help.addAction(self.act_about)

    # 開いた時の表示倍率のプリセット（設定値の文字列 → 表示ラベル）
    _DEFAULT_ZOOM_PRESETS = [
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

    def _build_default_zoom_menu(self, m_view: QMenu) -> None:
        """PDFを開いた時の既定の表示倍率を選ぶサブメニュー（表示メニュー内）。"""
        sub = m_view.addMenu("開いた時の表示倍率")
        self.default_zoom_group = QActionGroup(self)
        self.default_zoom_group.setExclusive(True)
        self.default_zoom_actions = {}
        for key, label in self._DEFAULT_ZOOM_PRESETS:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setData(key)
            self.default_zoom_group.addAction(act)
            sub.addAction(act)
            self.default_zoom_actions[key] = act
        self.default_zoom_group.triggered.connect(self._on_default_zoom_changed)

    def _on_default_zoom_changed(self, action) -> None:
        self.settings.setValue("default_zoom_mode", action.data())

    def _apply_default_zoom(self, remember_zoom: float | None = None) -> None:
        """新しく開いた文書に、設定済みの既定の表示倍率を適用する。

        remember_zoom は「前回の倍率を維持」モード用に、呼び出し側が
        set_document() などを呼ぶ *前* に読み取っておいた値を渡す。
        ドキュメントを開く過程で発生する内部的な fit_width() の呼び出し
        （新しいタブの初期化など）が settings の last_zoom を上書きして
        しまうため、ここで改めて読み直すと直前の値を取り逃す。
        """
        mode = self.settings.value("default_zoom_mode", "fit", str)
        if mode == "remember":
            zoom = remember_zoom if remember_zoom is not None else \
                self.settings.value("last_zoom", 1.0, float)
            self.page_view.set_zoom(zoom)
        elif mode == "fit_height":
            self.page_view.fit_height()
        elif mode.isdigit():
            self.page_view.set_zoom(int(mode) / 100.0)
        else:
            self.page_view.fit_width()

    def _build_ribbon(self) -> None:
        """Foxit 風のリボン（タブ切替式ツールバー）を最上部に構築する。

        既存の QAction / ウィジェットをそのまま流用し、従来のメニューバー・
        ツールバーは隠す。ボタンは action に束ねるので状態は自動同期する。
        """
        from PySide6.QtGui import QIntValidator
        from PySide6.QtWidgets import QToolButton

        from .ribbon import RIBBON_QSS, Ribbon

        # リボン用の短縮ラベル（長い action 名は iconText で短く表示。見切れ防止）
        short = {
            self.act_save: "保存", self.act_save_as: "別名保存",
            self.act_open_acrobat: "Acrobat", self.act_set_default_pdf: "既定に設定",
            self.act_copy: "コピー", self.act_clear_annots: "注釈を全消去",
            self.act_page_numbers: "ページ番号", self.act_toggle_pagedock: "ページ一覧",
            self.act_prev: "前へ", self.act_next: "次へ",
            self.act_zoom_in: "拡大", self.act_zoom_out: "縮小",
            self.act_rotate_left: "左回転", self.act_rotate_right: "右回転",
            self.act_search_prev: "前", self.act_search_next: "次",
        }
        for act, text in short.items():
            act.setIconText(text)

        r = Ribbon(self)
        self.ribbon = r

        # ── ホーム ──────────────────────────────────────────────
        p = r.add_page("ホーム")
        g = r.add_group(p, "ファイル")
        g.add_action(self.act_open, big=True)
        g.add_stack([self.act_save, self.act_save_as, self.act_print,
                     self.act_open_acrobat, self.act_set_default_pdf])
        recent_btn = QToolButton()
        recent_btn.setText("最近 ▾")
        recent_btn.setAutoRaise(True)
        recent_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        recent_btn.setMenu(self.recent_menu)
        g.add_widget(recent_btn)
        g = r.add_group(p, "ページ移動")
        g.add_stack([self.act_prev, self.act_next])
        # 現在ページの直接入力（例: 8 + Enter で 8 ページへ）
        self.page_edit = QLineEdit()
        self.page_edit.setFixedWidth(46)
        self.page_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_edit.setValidator(QIntValidator(1, 999999, self.page_edit))
        self.page_edit.setToolTip("ページ番号を入力して Enter でジャンプ")
        self.page_edit.returnPressed.connect(self._jump_to_page_edit)
        self.page_total_label = QLabel(" / -")
        g.add_widget(self.page_edit)
        g.add_widget(self.page_total_label)
        g = r.add_group(p, "表示倍率")
        g.add_stack([self.act_zoom_in, self.act_zoom_out,
                     self.act_fit, self.act_fit_height])
        g.add_widget(self.zoom_slider)
        g.add_widget(self.zoom_combo)
        # PDF を開いた時にどの倍率で表示するかを固定する設定（プルダウン）
        default_zoom_btn = QToolButton()
        default_zoom_btn.setText("開き方 ▾")
        default_zoom_btn.setAutoRaise(True)
        default_zoom_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        default_zoom_btn.setToolTip(
            "PDFを開いた時の表示倍率を固定します（幅/縦に合わせる・前回維持・固定%）")
        dz_menu = QMenu(default_zoom_btn)
        for act in self.default_zoom_group.actions():
            dz_menu.addAction(act)
        default_zoom_btn.setMenu(dz_menu)
        self.default_zoom_btn = default_zoom_btn
        g.add_widget(default_zoom_btn)
        g = r.add_group(p, "回転")
        g.add_stack([self.act_rotate_left, self.act_rotate_right])
        g = r.add_group(p, "検索")
        g.add_action(self.act_find, big=True)  # 押すと検索ポップアップが出る
        r.end_page(p)

        # ── 注釈 ────────────────────────────────────────────────
        p = r.add_page("注釈")
        g = r.add_group(p, "ツール")
        g.add_stack(list(self.tool_group.actions()))
        g = r.add_group(p, "書式")
        g.add_action(self.act_color, big=True)
        g.add_label(" 太さ ")
        g.add_widget(self.width_spin)
        g.add_label(" 線種 ")
        g.add_widget(self.line_style_combo)
        g = r.add_group(p, "矢印")
        g.add_label(" 始点 ")
        g.add_widget(self.line_start_combo)
        g.add_label(" 終点 ")
        g.add_widget(self.line_end_combo)
        g = r.add_group(p, "塗り・不透明度")
        g.add_action(self.act_fill_enabled, big=True)
        g.add_action(self.act_fill_color, big=True)
        g.add_label(" 不透明度 ")
        g.add_widget(self.opacity_spin)
        g = r.add_group(p, "テキスト")
        g.add_label(" サイズ ")
        g.add_widget(self.text_size_spin)
        g.add_label(" フォント ")
        g.add_widget(self.text_font_combo)
        g = r.add_group(p, "スタンプ")
        g.add_widget(self.stamp_btn)
        g = r.add_group(p, "編集")
        g.add_stack([self.act_undo, self.act_redo])
        g.add_stack([self.act_copy, self.act_paste])
        g.add_stack([self.act_apply_redact, self.act_clear_annots])
        r.end_page(p)

        # ── ページ ──────────────────────────────────────────────
        p = r.add_page("ページ")
        g = r.add_group(p, "モード")
        g.add_action(self.act_organize, big=True)
        g.add_action(self.act_form, big=True)
        g = r.add_group(p, "ページ操作")
        g.add_stack([self.act_delete, self.act_merge, self.act_extract,
                     self.act_split, self.act_blank_page, self.act_duplicate_page])
        g = r.add_group(p, "調整・装飾")
        g.add_stack([self.act_autocrop, self.act_page_numbers,
                     self.act_header_footer, self.act_watermark])
        r.end_page(p)

        # ── 変換 ────────────────────────────────────────────────
        p = r.add_page("変換")
        g = r.add_group(p, "画像")
        g.add_stack([self.act_to_images, self.act_from_images, self.act_add_images])
        g = r.add_group(p, "文字認識")
        g.add_stack([self.act_ocr, self.act_deskew])
        g = r.add_group(p, "出力")
        g.add_stack([self.act_export_text, self.act_export_html, self.act_metadata])
        g = r.add_group(p, "一括")
        g.add_stack([self.act_batch, self.act_diff])
        r.end_page(p)

        # ── 保護 ────────────────────────────────────────────────
        p = r.add_page("保護")
        g = r.add_group(p, "セキュリティ")
        g.add_stack([self.act_protect, self.act_unlock])
        g = r.add_group(p, "最適化")
        g.add_stack([self.act_compress, self.act_export_pdfa])
        r.end_page(p)

        # ── 表示 ────────────────────────────────────────────────
        p = r.add_page("表示")
        g = r.add_group(p, "テーマ")
        g.add_stack([self.act_dark, self.act_facing])
        g = r.add_group(p, "パネル")
        toc_toggle = self.toc_dock.toggleViewAction()
        toc_toggle.setIconText("しおり一覧")
        toc_toggle.setIcon(self._glyph_icon("📑"))
        annot_toggle = self.annot_dock.toggleViewAction()
        annot_toggle.setIconText("注釈一覧")
        annot_toggle.setIcon(self._glyph_icon("🗒"))
        g.add_stack([self.act_toggle_pagedock, toc_toggle, annot_toggle])
        g = r.add_group(p, "しおり")
        g.add_stack([self.act_add_bookmark, self.act_edit_toc])
        g = r.add_group(p, "ヘルプ")
        g.add_stack([self.act_check_update, self.act_set_update_url,
                     self.act_report_govform, self.act_about])
        r.end_page(p)

        # リボンを最上部に据え、従来のメニューバー/ツールバーは隠す
        self.setMenuWidget(r)
        r.setStyleSheet(RIBBON_QSS)
        # 太さ・線種・始点/終点・不透明・ズーム等のコントロールは、元々ツールバーへ
        # addWidget で置いてからリボンへ移設している。addWidget が内部で作る
        # QWidgetAction はウィジェットを「所有」し続けるため、この後 tb.hide() すると
        # リボンへ移したはずのウィジェットまで一緒に隠され、操作できなくなる
        # （＝リボン折りたたみに関係なく、これらの入力欄が使えない不具合）。
        # ツールバー側の QWidgetAction を外し、リボン内で確実に表示させてから隠す。
        from PySide6.QtWidgets import QWidgetAction
        for tb in (self.main_toolbar, self.annot_toolbar):
            for act in list(tb.actions()):
                if isinstance(act, QWidgetAction):
                    wdg = act.defaultWidget()
                    if wdg is not None and r.isAncestorOf(wdg):
                        tb.removeAction(act)
                        wdg.show()
            tb.hide()
        # 折りたたみ状態を復元
        if self.settings.value("ribbon_collapsed", False, bool):
            r.set_collapsed(True)

    def _jump_to_page_edit(self) -> None:
        """リボンのページ番号欄からジャンプ（入力 + Enter）。"""
        try:
            n = int(self.page_edit.text())
        except ValueError:
            return
        if self.doc.is_open:
            self._goto(n - 1)  # set_page 側で範囲内に丸められる
        self.page_view.setFocus()

    def _apply_lite_mode(self) -> None:
        """ライト版では OCR・傾き補正（numpy/Pillow 依存）の機能を隠す。"""
        from . import storage

        self.is_lite = storage.is_lite()
        if self.is_lite:
            self.act_ocr.setVisible(False)
            self.act_deskew.setVisible(False)
            self.setWindowTitle("TriV-Reader (Lite)")

    def _polish_actions(self) -> None:
        """各アクションにツールチップ/ステータスヒントを付ける（ショートカット併記）。"""
        tips = {
            self.act_open: "PDF ファイルを開きます",
            self.act_save: "上書き保存します",
            self.act_save_as: "名前を付けて保存します",
            self.act_print: "印刷します",
            self.act_prev: "前のページへ",
            self.act_next: "次のページへ",
            self.act_zoom_in: "拡大",
            self.act_zoom_out: "縮小",
            self.act_fit: "ページ幅に合わせる",
            self.act_fit_height: "ページ全体が縦に収まる倍率にする",
            self.act_facing: "2ページずつ並べて表示（見開き）",
            self.act_rotate_left: "左に90°回転",
            self.act_rotate_right: "右に90°回転",
            self.act_organize: "ページの並べ替え・削除・抽出を行う整理画面",
            self.act_find: "文書内のテキストを検索",
            self.act_copy: "選択したテキストをコピー",
            self.act_dark: "ダークモードの切り替え",
            self.act_color: "注釈の色を選択",
            self.act_fill_enabled: "四角/円を塗りつぶす（オフ＝枠線のみ）",
            self.act_fill_color: "塗りつぶしの色を選択",
            self.act_open_acrobat: "このPDFを Adobe Acrobat で開く（表示がおかしい時などの予備手段）",
            self.act_set_default_pdf: "TriV-Reader をPDFの候補アプリに登録し、Windowsの既定アプリ設定を開く",
            self.act_apply_redact: "登録した墨消しを適用して内容を完全削除",
            self.act_ocr: "画像/スキャンPDFを文字認識して検索可能にする",
            self.act_deskew: "スキャンの傾きを自動補正",
        }
        for act, tip in tips.items():
            seq = act.shortcut()
            sc = "" if seq.isEmpty() else seq.toString()
            act.setToolTip(f"{tip}（{sc}）" if sc else tip)
            act.setStatusTip(tip)
        # ツール群のヒント
        tool_tips = {
            page_view.TOOL_NONE: "選択：テキスト選択・注釈の選択/移動",
            page_view.TOOL_HIGHLIGHT: "文字をなぞってハイライト",
            page_view.TOOL_PEN: "フリーハンドで描画",
            page_view.TOOL_RECT: "四角を描く",
            page_view.TOOL_UNDERLINE: "文字に下線",
            page_view.TOOL_STRIKEOUT: "文字に取り消し線",
            page_view.TOOL_LINE: "直線を引く",
            page_view.TOOL_ARROW: "矢印を引く",
            page_view.TOOL_CIRCLE: "円/楕円を描く",
            page_view.TOOL_TEXT: "テキストボックスを追加",
            page_view.TOOL_NOTE: "付箋メモを追加",
            page_view.TOOL_REDACT: "墨消し範囲を指定（適用で完全削除）",
            page_view.TOOL_CROP: "トリミング範囲を指定",
            page_view.TOOL_ERASE: "注釈をクリックして削除",
        }
        for act in self.tool_group.actions():
            tip = tool_tips.get(act.data())
            if tip:
                act.setToolTip(tip)
                act.setStatusTip(tip)
        self._assign_icons()

    # --- アイコン / コンパクト表示 ------------------------------------
    def _glyph_icon(self, glyph: str) -> QIcon:
        """記号/絵文字を描いた小さなアイコンを生成（コンパクト表示用）。"""
        size = 36
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setPen(self.palette().color(self.foregroundRole()))
        f = QFont()
        f.setPointSize(16)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
        p.end()
        return QIcon(pm)

    def _assign_icons(self) -> None:
        glyphs = {
            self.act_open: "📂", self.act_save: "💾", self.act_save_as: "📝",
            self.act_print: "🖨", self.act_prev: "◀", self.act_next: "▶",
            self.act_zoom_in: "➕", self.act_zoom_out: "➖", self.act_fit: "↔",
            self.act_fit_height: "↕",
            self.act_facing: "📖", self.act_rotate_left: "↺", self.act_rotate_right: "↻",
            self.act_organize: "🗂", self.act_delete: "🗑", self.act_merge: "🔗",
            self.act_extract: "📤", self.act_split: "✂", self.act_to_images: "🖼",
            self.act_from_images: "📥", self.act_add_images: "🏞", self.act_ocr: "🔡",
            self.act_deskew: "📐", self.act_apply_redact: "✅",
            self.act_clear_annots: "🧹", self.act_highlight_all: "🖍",
            self.act_search_prev: "◀", self.act_search_next: "▶",
            # リボン化にあたり残りのアクションにもアイコンを付与
            self.act_form: "🖊", self.act_copy: "📋",
            self.act_blank_page: "🗒", self.act_duplicate_page: "📑",
            self.act_autocrop: "⬚", self.act_page_numbers: "🔢",
            self.act_header_footer: "📃", self.act_watermark: "💧",
            self.act_export_text: "📄", self.act_export_html: "🌐",
            self.act_metadata: "🏷", self.act_batch: "📚", self.act_diff: "🔀",
            self.act_protect: "🔒", self.act_unlock: "🔓",
            self.act_compress: "🗜", self.act_export_pdfa: "🅰",
            self.act_dark: "🌙", self.act_add_bookmark: "🔖",
            self.act_find: "🔍",
            self.act_edit_toc: "📇", self.act_check_update: "🔄",
            self.act_set_update_url: "⚙", self.act_about: "ℹ",
            self.act_report_govform: "📮",
            self.act_toggle_pagedock: "▤",
            self.act_fill_enabled: "🪣", self.act_fill_color: "🎨",
            self.act_open_acrobat: "🅐", self.act_set_default_pdf: "⭐",
        }
        for act, g in glyphs.items():
            act.setIcon(self._glyph_icon(g))
        tool_glyphs = {
            page_view.TOOL_NONE: "🖱", page_view.TOOL_HIGHLIGHT: "🖍",
            page_view.TOOL_PEN: "✏", page_view.TOOL_RECT: "▭",
            page_view.TOOL_UNDERLINE: "U", page_view.TOOL_STRIKEOUT: "S",
            page_view.TOOL_LINE: "／", page_view.TOOL_ARROW: "↗",
            page_view.TOOL_CIRCLE: "◯", page_view.TOOL_TEXT: "T",
            page_view.TOOL_NOTE: "📌", page_view.TOOL_REDACT: "▉",
            page_view.TOOL_CROP: "◳", page_view.TOOL_ERASE: "⌫",
        }
        for act in self.tool_group.actions():
            g = tool_glyphs.get(act.data())
            if g:
                act.setIcon(self._glyph_icon(g))

    # 通常時のツールバー余白（既定でも詰めて縦の無駄を減らす）
    _TOOLBAR_QSS = (
        "QToolBar { padding: 0px; spacing: 2px; margin: 0px; border: 0px; }"
        "QToolButton { padding: 3px 7px; margin: 0px; border-radius: 4px; }"
    )

    def _tighten_chrome(self) -> None:
        """ツールバー・メニューバーの余白を詰める（既定状態で適用）。"""
        for tb in (self.main_toolbar, self.annot_toolbar):
            tb.setContentsMargins(0, 0, 0, 0)
            tb.setStyleSheet(self._TOOLBAR_QSS)
        self.menuBar().setStyleSheet(
            "QMenuBar { padding: 0px; } QMenuBar::item { padding: 3px 9px; }"
        )

    def _set_compact(self, on: bool) -> None:
        """ツールバーをアイコンのみ（省スペース）/ 文字ラベル に切り替える。

        コンパクト時はボタン・ツールバーの余白をさらに詰めて縦幅を小さくする。
        """
        style = (Qt.ToolButtonStyle.ToolButtonIconOnly if on
                 else Qt.ToolButtonStyle.ToolButtonTextOnly)
        # コンパクト時のみ、各ツールバー限定で余白を最小化（全体QSSより優先）
        compact_qss = (
            "QToolBar { padding: 0px; spacing: 1px; }"
            "QToolButton { padding: 2px; margin: 0px; border-radius: 4px; }"
        )
        for tb in (self.main_toolbar, self.annot_toolbar):
            tb.setToolButtonStyle(style)
            tb.setIconSize(QSize(18, 18) if on else QSize(16, 16))
            tb.setContentsMargins(0, 0, 0, 0)
            # コンパクト解除時は既定の詰めQSSに戻す（空文字だと余白が広がる）
            tb.setStyleSheet(compact_qss if on else self._TOOLBAR_QSS)

    def _refresh_color_icon(self) -> None:
        """色ボタンに現在の色のアイコンを表示する。"""
        from PySide6.QtGui import QPixmap

        pm = QPixmap(18, 18)
        pm.fill(self._annot_color)
        self.act_color.setIcon(pm)

    def _refresh_fill_icon(self) -> None:
        """塗り色ボタンに現在の塗り色のアイコンを表示する。"""
        from PySide6.QtGui import QPixmap

        pm = QPixmap(18, 18)
        pm.fill(self._fill_color)
        self.act_fill_color.setIcon(pm)

    def _connect(self) -> None:
        self.act_open.triggered.connect(self.open_file)
        self.act_prev.triggered.connect(lambda: self._goto(self.page_view.index - 1))
        self.act_next.triggered.connect(lambda: self._goto(self.page_view.index + 1))
        self.act_zoom_in.triggered.connect(
            lambda: self.page_view.set_zoom(self.page_view.zoom * ZOOM_STEP)
        )
        self.act_zoom_out.triggered.connect(
            lambda: self.page_view.set_zoom(self.page_view.zoom / ZOOM_STEP)
        )
        self.act_fit.triggered.connect(self._fit)
        self.act_fit_height.triggered.connect(self._fit_height)
        self.act_rotate_left.triggered.connect(lambda: self._rotate(-90))
        self.act_rotate_right.triggered.connect(lambda: self._rotate(90))
        self.act_save.triggered.connect(self.save)
        self.act_save_as.triggered.connect(self.save_as)
        self.act_delete.triggered.connect(lambda: self._delete_page(self.page_view.index))
        self.act_merge.triggered.connect(self.merge_pdf)
        self.act_extract.triggered.connect(self.extract_pages)
        self.act_split.triggered.connect(self.split_pdf)
        self.act_organize.toggled.connect(self._toggle_organize)
        self.act_form.toggled.connect(self._toggle_form)
        self.act_to_images.triggered.connect(self.export_images_dialog)
        self.act_from_images.triggered.connect(self.images_to_pdf_dialog)
        self.act_add_images.triggered.connect(self.add_images_as_pages)
        self.act_ocr.triggered.connect(self.run_ocr)
        # 注釈ツール
        self.tool_group.triggered.connect(self._on_tool_changed)
        self.act_color.triggered.connect(self._pick_color)
        self.width_spin.valueChanged.connect(lambda v: self.page_view.set_pen_width(v))
        self.line_style_combo.currentIndexChanged.connect(
            lambda: self.page_view.set_line_style(self.line_style_combo.currentData())
        )
        self.line_start_combo.currentIndexChanged.connect(
            lambda: self.page_view.set_line_start(self.line_start_combo.currentData())
        )
        self.line_end_combo.currentIndexChanged.connect(
            lambda: self.page_view.set_line_end(self.line_end_combo.currentData())
        )
        self.act_fill_enabled.toggled.connect(
            lambda on: self.page_view.set_fill_enabled(on)
        )
        self.act_fill_color.triggered.connect(self._pick_fill_color)
        self.opacity_spin.valueChanged.connect(
            lambda v: self.page_view.set_opacity(v / 100.0)
        )
        self.text_size_spin.valueChanged.connect(
            lambda v: self.page_view.set_text_size(v)
        )
        self.text_font_combo.currentIndexChanged.connect(
            lambda: self.page_view.set_text_font(self.text_font_combo.currentData())
        )
        self.act_clear_annots.triggered.connect(self._clear_annots)
        self.act_undo.triggered.connect(self._undo_annot)
        self.act_redo.triggered.connect(self._redo_annot)
        # しおり / 最適化 / PDF-A
        self.act_add_bookmark.triggered.connect(self._add_bookmark)
        self.act_edit_toc.triggered.connect(self._edit_toc)
        self.act_compress.triggered.connect(self.compress_save)
        self.act_export_pdfa.triggered.connect(self.export_pdfa)
        self.toc_dock.page_requested.connect(self._goto)
        self.annot_dock.annot_selected.connect(self._on_annot_list_selected)
        self.annot_dock.annot_delete_requested.connect(self._on_annot_list_delete)
        # 検索 / ページ番号
        self.search_edit.returnPressed.connect(self._do_search)
        self.act_search_next.triggered.connect(lambda: self._search_step(1))
        self.act_search_prev.triggered.connect(lambda: self._search_step(-1))
        self.act_find.triggered.connect(self._focus_search)
        self.act_highlight_all.triggered.connect(self._highlight_all_hits)
        self.act_page_numbers.triggered.connect(self.add_page_numbers_dialog)
        # パスワード / テーマ
        self.act_protect.triggered.connect(self.protect_save)
        self.act_unlock.triggered.connect(self.unlock_save)
        self.act_dark.toggled.connect(self._toggle_dark)
        self.act_compact.toggled.connect(self._set_compact)
        self.act_copy.triggered.connect(self._copy)
        self.act_paste.triggered.connect(self._paste_annot)
        self.act_paste.setEnabled(False)
        # 墨消し適用・ページ操作・透かし・メタ・抽出
        self.act_apply_redact.triggered.connect(self._apply_redactions)
        self.act_blank_page.triggered.connect(self._insert_blank_page)
        self.act_duplicate_page.triggered.connect(self._duplicate_page)
        self.act_autocrop.triggered.connect(self._auto_crop_all)
        self.act_watermark.triggered.connect(self.add_watermark_dialog)
        self.act_header_footer.triggered.connect(self.add_header_footer_dialog)
        self.act_metadata.triggered.connect(self.edit_metadata_dialog)
        self.act_export_text.triggered.connect(lambda: self._export_text(False))
        self.act_export_html.triggered.connect(lambda: self._export_text(True))
        # ズーム / 印刷
        self.zoom_slider.sliderMoved.connect(
            lambda v: self.page_view.set_zoom(v / 100.0)
        )
        self.zoom_combo.activated.connect(self._on_zoom_preset)
        self.act_print.triggered.connect(self.print_document)
        self.act_open_acrobat.triggered.connect(self.open_in_acrobat)
        self.act_set_default_pdf.triggered.connect(self.set_as_default_pdf_app)
        self.act_facing.toggled.connect(self._toggle_facing)
        self.act_batch.triggered.connect(self.batch_dialog)
        self.act_diff.triggered.connect(self.diff_dialog)
        self.act_deskew.triggered.connect(self.deskew_document)
        self.act_check_update.triggered.connect(self.check_for_update)
        self.act_set_update_url.triggered.connect(self.set_update_url)
        self.act_about.triggered.connect(self.show_about)
        self.act_report_govform.triggered.connect(self.report_govform_unsupported)
        self.act_telemetry_toggle.toggled.connect(self._toggle_telemetry)
        # 共有ドック（アクティブタブに対して動作）
        self.thumbnails.page_selected.connect(self._goto)
        self.thumbnails.pages_reordered.connect(self._reorder)
        self.thumbnails.delete_requested.connect(self._delete_page)
        # 注: page_view / organizer のシグナルはタブ毎に _new_tab で接続する
        self._setup_shortcuts()

    def _setup_shortcuts(self) -> None:
        """一般的な PDF リーダーのキーボードショートカット。

        （PageView 側: ←→=ページ送り, ↑↓=スクロール, Home/End=先頭/最終,
          Space / Shift+Space=1画面送り/戻り）
        """
        from PySide6.QtGui import QShortcut

        # F3 / Shift+F3: 次/前の検索結果
        self.act_search_next.setShortcut(QKeySequence(Qt.Key.Key_F3))
        self.act_search_prev.setShortcut(QKeySequence("Shift+F3"))
        self.addAction(self.act_search_next)
        self.addAction(self.act_search_prev)
        # Ctrl+0: 幅に合わせる
        self.act_fit.setShortcut(QKeySequence("Ctrl+0"))
        self.addAction(self.act_fit)
        # Ctrl+1: 実際のサイズ (100%)
        QShortcut(QKeySequence("Ctrl+1"), self,
                  lambda: self.page_view.set_zoom(1.0))
        # Ctrl+2: 縦に合わせる
        self.act_fit_height.setShortcut(QKeySequence("Ctrl+2"))
        self.addAction(self.act_fit_height)
        # Ctrl+Home / Ctrl+End: 先頭 / 最終ページ
        QShortcut(QKeySequence("Ctrl+Home"), self,
                  lambda: self.page_view.set_page(0))
        QShortcut(QKeySequence("Ctrl+End"), self,
                  lambda: self.doc.is_open and self.page_view.set_page(
                      self.doc.page_count - 1))
        # Ctrl+G: ページ番号入力欄へ
        QShortcut(QKeySequence("Ctrl+G"), self, self._focus_page_edit)
        # Ctrl+W: 現在のタブを閉じる
        QShortcut(QKeySequence("Ctrl+W"), self,
                  lambda: self.tabs.count() and self._close_tab(
                      self.tabs.currentIndex()))
        # Ctrl+Tab / Ctrl+Shift+Tab: タブ切替
        QShortcut(QKeySequence("Ctrl+Tab"), self, lambda: self._cycle_tab(1))
        QShortcut(QKeySequence("Ctrl+Shift+Tab"), self,
                  lambda: self._cycle_tab(-1))

    def _focus_page_edit(self) -> None:
        if not hasattr(self, "page_edit"):
            return
        if self.ribbon.is_collapsed():
            self.ribbon._expand_if_collapsed(self.ribbon.currentIndex())
        self.ribbon.setCurrentIndex(0)  # ホームタブ
        self.page_edit.setFocus()
        self.page_edit.selectAll()

    def _cycle_tab(self, step: int) -> None:
        n = self.tabs.count()
        if n > 1:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % n)

    def _new_tab(self) -> "DocTab":
        """新しいドキュメントタブを作り、ビューワー/整理画面のシグナルを接続する。"""
        tab = DocTab()
        pv, org = tab.page_view, tab.organizer
        pv.page_changed.connect(self._on_page_changed)
        pv.annotation_changed.connect(self._on_annotation_changed)
        pv.selection_changed.connect(self._on_selection_changed)
        pv.text_selection_changed.connect(self._on_text_selection_changed)
        pv.zoom_changed.connect(self._on_zoom_changed)
        pv.form_changed.connect(self._on_form_changed)
        org.reordered.connect(self._reorder_from_organizer)
        org.delete_requested.connect(self._delete_pages)
        org.extract_requested.connect(self._extract_indices)
        org.rotate_requested.connect(self._rotate_pages)
        org.split_requested.connect(self.split_pdf)
        org.export_images_requested.connect(self._export_indices_images)
        org.page_activated.connect(self._jump_from_organizer)
        org.insert_requested.connect(self._insert_from_organizer)
        org.properties_requested.connect(lambda: self.act_metadata.trigger())
        org.security_requested.connect(lambda: self.act_protect.trigger())
        dark = self.act_dark.isChecked()
        pv.set_canvas_color(theme.canvas_color(dark))
        org.apply_theme(dark)
        return tab

    # --- 操作 -----------------------------------------------------------
    def open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "PDF を開く", self._last_dir(), "PDF ファイル (*.pdf)"
        )
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> None:
        # 既に同じファイルを開いていれば、そのタブへ切り替える
        for i in range(self.tabs.count()):
            t = self.tabs.widget(i)
            if isinstance(t, DocTab) and t.doc.path and os.path.abspath(t.doc.path) == os.path.abspath(path):
                self.tabs.setCurrentIndex(i)
                return

        # 「前回の倍率を維持」モード用に、新規タブ初期化の副作用で上書きされる
        # 前に読み取っておく
        remembered_zoom = self.settings.value("last_zoom", 1.0, float)

        tab = self._new_tab()
        doc = tab.doc
        try:
            doc.open(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"開けませんでした:\n{exc}")
            tab.deleteLater()
            return
        # 暗号化PDF: 使い捨て複製でパスワードを確定してから本体を開く
        if doc.needs_password:
            doc.close()
            password = None
            while True:
                pw, ok = QInputDialog.getText(
                    self, "パスワード", f"「{os.path.basename(path)}」のパスワード:",
                    QLineEdit.EchoMode.Password,
                )
                if not ok:
                    tab.deleteLater()
                    return
                if PdfDocument.check_password(path, pw):
                    password = pw
                    break
                QMessageBox.warning(self, "認証失敗", "パスワードが違います")
            doc.open(path, password=password)

        tab.page_view.set_document(doc)
        idx = self.tabs.addTab(tab, os.path.basename(path))
        self.tabs.setTabToolTip(idx, path)
        self.center.setCurrentWidget(self.tabs)
        self.tabs.setCurrentIndex(idx)  # _on_tab_changed が読み込み・更新を行う
        QApplication.processEvents()
        self._apply_default_zoom(remembered_zoom)
        self._add_recent(path)
        # 入力欄付き（フォーム）PDF は Adobe と同様、開いた時点で入力可能にする
        if doc.is_form():
            tab.page_view.set_form_mode(True)
            self.act_form.blockSignals(True)
            self.act_form.setChecked(True)
            self.act_form.blockSignals(False)
            self.statusBar().showMessage(
                "入力欄のある PDF です。欄をクリックしてそのまま入力できます"
                "（リボン「ページ > フォーム入力」でオフ）", 6000)
            self._check_unsupported_govform(doc)

    def activate_and_open(self, path: str) -> None:
        """別インスタンスから渡されたパスを開き、ウィンドウを前面化する。"""
        if path:
            self.open_path(path)
        # 最小化されていれば戻し、前面へ
        self.setWindowState(
            (self.windowState() & ~Qt.WindowState.WindowMinimized)
            | Qt.WindowState.WindowActive
        )
        self.show()
        self.raise_()
        self.activateWindow()

    # --- システムトレイ常駐 --------------------------------------------
    def _setup_tray(self) -> None:
        """タスクトレイに常駐させる（閉じても裏で起動を保ち、次回を高速化）。"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        icon = self.windowIcon()
        if icon.isNull():
            icon = QApplication.windowIcon()
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("TriV-Reader")
        menu = QMenu()
        act_show = menu.addAction("ウィンドウを表示")
        act_show.triggered.connect(self._show_from_tray)
        menu.addSeparator()
        self.act_tray_resident = menu.addAction("閉じてもトレイに常駐")
        self.act_tray_resident.setCheckable(True)
        self.act_tray_resident.setChecked(
            self.settings.value("tray/resident", True, type=bool)
        )
        self.act_tray_resident.toggled.connect(
            lambda on: self.settings.setValue("tray/resident", on)
        )
        menu.addSeparator()
        act_quit = menu.addAction("終了")
        act_quit.triggered.connect(self._quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _tray_resident_enabled(self) -> bool:
        return (
            getattr(self, "tray", None) is not None
            and self.act_tray_resident.isChecked()
        )

    def _on_tray_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.setWindowState(
            (self.windowState() & ~Qt.WindowState.WindowMinimized)
            | Qt.WindowState.WindowActive
        )
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit_app(self) -> None:
        """トレイメニューの「終了」：本当にアプリを終了する。"""
        self._force_quit = True
        self.close()

    def _on_tab_changed(self, _index: int) -> None:
        """アクティブタブが変わったら共有UI（サムネ/目次/状態）を同期する。"""
        # 直前タブのスクロール位置を保存（タブ切替で先頭に戻らないように）
        prev = getattr(self, "_active_doctab", None)
        if isinstance(prev, DocTab):
            try:
                prev._saved_scroll = prev.page_view.scroll_value()
            except Exception:  # noqa: BLE001
                pass

        tab = self._active_tab()
        self._active_doctab = tab
        if tab is None:
            self.center.setCurrentWidget(self.welcome)
            self._update_actions()
            self._update_status()
            self._update_title()
            return
        self.thumbnails.load(tab.doc)
        self.toc_dock.load(tab.doc.get_toc())
        self._refresh_annot_list()
        self.thumbnails.select_page(tab.page_view.index)
        # このタブで前回見ていたスクロール位置を復元（描画確定後に）
        saved = getattr(tab, "_saved_scroll", 0)
        if saved:
            QTimer.singleShot(0, lambda t=tab, v=saved: t.page_view.set_scroll_value(v))
        # 整理トグルをこのタブの状態に同期
        self.act_organize.blockSignals(True)
        self.act_organize.setChecked(tab.currentWidget() is tab.organizer)
        self.act_organize.blockSignals(False)
        # フォーム入力トグルをこのタブの状態に同期
        self.act_form.blockSignals(True)
        self.act_form.setChecked(tab.page_view.form_mode())
        self.act_form.blockSignals(False)
        # 検索UIをこのタブに同期
        self.search_edit.clear()
        self._update_search_label()
        self._update_actions()
        self._update_status()
        self._update_title()

    def _close_tab(self, index: int) -> None:
        tab = self.tabs.widget(index)
        if not isinstance(tab, DocTab):
            return
        if tab.doc.is_open and tab.doc.modified:
            self.tabs.setCurrentIndex(index)
            ret = QMessageBox.question(
                self, "未保存の変更",
                f"「{self.tabs.tabText(index)}」に保存されていない変更があります。保存しますか？",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if ret == QMessageBox.StandardButton.Save:
                self.save()
                if tab.doc.modified:  # 保存がキャンセルされた等
                    return
            elif ret == QMessageBox.StandardButton.Cancel:
                return
        tab.doc.close()
        if getattr(self, "_active_doctab", None) is tab:
            self._active_doctab = None
        self.tabs.removeTab(index)
        tab.deleteLater()
        if self.tabs.count() == 0:
            # 最後のタブを閉じたら左のサムネ/しおり表示も消す（表示が残らないように）
            self.thumbnails.load(self._empty_doc)
            self.toc_dock.load([])
            self.center.setCurrentWidget(self.welcome)
            self._update_actions()
            self._update_title()

    def _goto(self, index: int) -> None:
        """サムネイルクリック等での明示的なページ移動（スクロールする）。"""
        self.page_view.set_page(index)
        self.thumbnails.select_page(self.page_view.index)
        self._update_status()

    def _on_page_changed(self, index: int) -> None:
        """スクロールで表示ページが変わったときの追従（再スクロールはしない）。"""
        self.thumbnails.select_page(index)
        self._update_status()

    def _fit(self) -> None:
        self.page_view.fit_width()
        self._update_status()

    def _fit_height(self) -> None:
        self.page_view.fit_height()
        self._update_status()

    def _rotate(self, delta: int) -> None:
        if not self.doc.is_open:
            return
        index = self.page_view.index
        self.doc.rotate_page(index, delta)
        self.page_view.refresh_page(index)
        self.thumbnails.refresh_page(self.doc, index)
        self._update_title()

    # --- 注釈 ----------------------------------------------------------
    def _on_tool_changed(self, action) -> None:
        self.page_view.set_tool(action.data())
        tool = action.data()
        # 直線/矢印ツールを選んだ時は終点スタイルを分かりやすい既定値にする
        # （ユーザーがコンボボックスで手動変更した後は、その値を尊重する）
        if tool == page_view.TOOL_ARROW:
            self._set_combo_data(self.line_end_combo, "closed_arrow")
        elif tool == page_view.TOOL_LINE:
            self._set_combo_data(self.line_end_combo, "none")
        if tool == page_view.TOOL_NONE:
            self.statusBar().clearMessage()
        else:
            self.statusBar().showMessage(
                f"注釈ツール: {action.text()}（ページ上でドラッグ/クリック）", 0
            )

    @staticmethod
    def _set_combo_data(combo: QComboBox, data) -> None:
        i = combo.findData(data)
        if i >= 0:
            combo.setCurrentIndex(i)

    def _pick_color(self) -> None:
        col = QColorDialog.getColor(self._annot_color, self, "注釈の色")
        if not col.isValid():
            return
        self._annot_color = col
        self.page_view.set_color(col)
        self._refresh_color_icon()
        # 注釈を選択中なら、その色を変更する
        if self.page_view.has_selection():
            self.page_view.recolor_selection(col)

    def _pick_fill_color(self) -> None:
        col = QColorDialog.getColor(self._fill_color, self, "塗りつぶしの色")
        if not col.isValid():
            return
        self._fill_color = col
        self.page_view.set_fill_color(col)
        self._refresh_fill_icon()

    def _on_selection_changed(self, has: bool) -> None:
        self._refresh_copy_enabled()
        ref = self.page_view.current_annot_ref()
        if ref is not None:
            self.annot_dock.select_xref(ref[0], ref[1])
        if has:
            self._sync_textstyle_controls()
            self.statusBar().showMessage(
                "注釈を選択中：ドラッグで移動 / ダブルクリックで本文編集 / "
                "太さ・線種・色・不透明度・塗り・文字サイズ・フォントで書式変更 / "
                "Ctrl+C コピー / Delete で削除", 0
            )
        else:
            self.statusBar().clearMessage()

    def _sync_textstyle_controls(self) -> None:
        """テキスト注釈を選択したら、文字サイズ・フォントの表示を注釈に合わせる。

        こうすることで、選択後にスピンを1回動かすだけで「今のサイズから」
        変更できる（意図しないサイズへ飛ばない）。
        """
        style = self.page_view.selection_textstyle()
        if not style or style.get("kind") != "FreeText":
            return
        self.text_size_spin.blockSignals(True)
        self.text_size_spin.setValue(int(round(style["fontsize"])))
        self.text_size_spin.blockSignals(False)
        self.page_view.text_size = max(6, int(round(style["fontsize"])))
        i = self.text_font_combo.findData(style.get("fontname", "helv"))
        if i >= 0:
            self.text_font_combo.blockSignals(True)
            self.text_font_combo.setCurrentIndex(i)
            self.text_font_combo.blockSignals(False)
            self.page_view.text_font = style.get("fontname", "helv")

    def _refresh_copy_enabled(self) -> None:
        pv = self.page_view
        self.act_copy.setEnabled(pv.has_selection() or pv.has_text_selection())

    @staticmethod
    def _focused_text_widget():
        """入力欄（テキスト系）にフォーカスがあればそれを返す（Ctrl+C/V の振り分け用）。"""
        from PySide6.QtWidgets import (QLineEdit, QPlainTextEdit, QTextEdit)
        fw = QApplication.focusWidget()
        return fw if isinstance(fw, (QLineEdit, QPlainTextEdit, QTextEdit)) else None

    def _copy(self) -> None:
        """入力欄フォーカス中はその選択文字を、注釈選択中はその注釈を、
        いずれでもなければ選択テキストをコピー。"""
        tw = self._focused_text_widget()
        if tw is not None:
            tw.copy()
            return
        desc = self.page_view.selected_annot_desc()
        if desc is not None:
            self._annot_clip = desc
            self.act_paste.setEnabled(True)
            self.statusBar().showMessage(
                "注釈をコピーしました（Ctrl+V で貼り付け）", 2500)
        else:
            self._copy_text()

    def _paste_annot(self) -> None:
        # 入力欄で編集中なら通常のテキスト貼り付けにする（注釈は貼らない）。
        tw = self._focused_text_widget()
        if tw is not None:
            tw.paste()
            return
        if self._annot_clip is not None and self.doc.is_open:
            if self.page_view.paste_annot(self._annot_clip):
                self.statusBar().showMessage("注釈を貼り付けました", 2000)

    def _clear_annots(self) -> None:
        if not self.doc.is_open:
            return
        index = self.page_view.index
        n = self.doc.clear_annots(index)
        if n:
            self.page_view.refresh_page(index)
            self._update_title()
            self.statusBar().showMessage(f"{n} 件の注釈を消去しました", 3000)
        else:
            self.statusBar().showMessage("このページに注釈はありません", 3000)

    def _place_stamp(self, label: str, rgb) -> None:
        """スタンプ配置モードに入る。日付は今日の日付を文字にする。"""
        from datetime import date
        if label == "日付":
            text = date.today().strftime("%Y/%m/%d")
        else:
            text = label
        color = QColor(int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255))
        self.page_view.set_stamp(text, color)
        self.statusBar().showMessage(
            f"スタンプ「{text}」：ページ上のクリックした位置に配置します", 0)

    def _undo_annot(self) -> None:
        if self.doc.is_open:
            self.page_view.undo_annot()
            self._update_undo_actions()

    def _redo_annot(self) -> None:
        if self.doc.is_open:
            self.page_view.redo_annot()
            self._update_undo_actions()

    def _update_undo_actions(self) -> None:
        pv = self.page_view
        self.act_undo.setEnabled(pv.can_undo_annot())
        self.act_redo.setEnabled(pv.can_redo_annot())

    def _on_annotation_changed(self, index: int) -> None:
        self._update_undo_actions()
        self._refresh_annot_list()
        self._update_title()
        self.thumbnails.refresh_page(self.doc, index)

    def _refresh_annot_list(self) -> None:
        try:
            self.annot_dock.load(self.doc.list_annots() if self.doc.is_open else [])
        except Exception:  # noqa: BLE001
            pass

    def _on_annot_list_selected(self, page: int, xref: int) -> None:
        """一覧クリック：そのページへ移動し、注釈を選択する。"""
        if not self.doc.is_open:
            return
        self._goto(page)
        self.page_view.set_tool(page_view.TOOL_NONE)  # 選択できるツールに
        self.page_view.set_selection(page, xref)

    def _on_annot_list_delete(self, page: int, xref: int) -> None:
        if not self.doc.is_open:
            return
        self.doc.delete_annot_xref(page, xref)
        self.page_view.refresh_page(page)
        self._on_annotation_changed(page)

    # --- 墨消し / ページ操作 / 透かし / メタ / 抽出 -------------------
    def _apply_redactions(self) -> None:
        if not self.doc.is_open:
            return
        ret = QMessageBox.question(
            self, "墨消しの適用",
            "登録済みの墨消し範囲の文字・画像を完全に削除します。元に戻せません。続行しますか？",
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.page_view.apply_redactions()
        self.thumbnails.load(self.doc)
        self._update_title()
        self.statusBar().showMessage("墨消しを適用しました", 3000)

    def _insert_blank_page(self) -> None:
        if not self.doc.is_open:
            return
        at = self.page_view.index + 1
        self.doc.insert_blank_page(at)
        self._reload(at)
        self.statusBar().showMessage("白紙ページを挿入しました", 3000)

    def _duplicate_page(self) -> None:
        if not self.doc.is_open:
            return
        idx = self.page_view.index
        self.doc.duplicate_page(idx)
        self._reload(idx + 1)
        self.statusBar().showMessage("ページを複製しました", 3000)

    def _auto_crop_all(self) -> None:
        if not self.doc.is_open:
            return
        n = 0
        for i in range(self.doc.page_count):
            rect = self.doc.auto_crop_rect(i)
            if rect:
                self.doc.set_crop(i, rect)
                n += 1
        self._reload(self.page_view.index)
        self.statusBar().showMessage(f"{n} ページの余白をトリミングしました", 3000)

    def add_watermark_dialog(self) -> None:
        if not self.doc.is_open:
            return
        text, ok = QInputDialog.getText(self, "透かし", "透かし文字:", text="社外秘")
        if not ok or not text.strip():
            return
        self.doc.add_watermark(text.strip())
        self._reload(self.page_view.index)
        self.statusBar().showMessage("透かしを追加しました", 3000)

    def add_header_footer_dialog(self) -> None:
        if not self.doc.is_open:
            return
        pos, ok = QInputDialog.getItem(
            self, "ヘッダー/フッター", "位置:", ["フッター(下)", "ヘッダー(上)"], 0, False
        )
        if not ok:
            return
        hint = "左・中央・右をカンマ区切りで入力。{date} {filename} {page} {total} 使用可"
        text, ok = QInputDialog.getText(
            self, "内容", hint, text="{filename}, , {page}/{total}"
        )
        if not ok:
            return
        cells = [c.strip() for c in (text.split(",") + ["", "", ""])[:3]]
        self.doc.add_header_footer(cells[0], cells[1], cells[2],
                                   top=(pos == "ヘッダー(上)"))
        self._reload(self.page_view.index)
        self.statusBar().showMessage("ヘッダー/フッターを追加しました", 3000)

    def edit_metadata_dialog(self) -> None:
        if not self.doc.is_open:
            return
        meta = self.doc.get_metadata()
        title, ok = QInputDialog.getText(self, "文書プロパティ", "タイトル:",
                                         text=meta.get("title", "") or "")
        if not ok:
            return
        author, ok = QInputDialog.getText(self, "文書プロパティ", "著者:",
                                          text=meta.get("author", "") or "")
        if not ok:
            return
        keywords, ok = QInputDialog.getText(self, "文書プロパティ", "キーワード:",
                                            text=meta.get("keywords", "") or "")
        if not ok:
            return
        meta.update(title=title, author=author, keywords=keywords)
        self.doc.set_metadata(meta)
        self._update_title()
        self.statusBar().showMessage("メタデータを更新しました", 3000)

    def _export_text(self, as_html: bool) -> None:
        if not self.doc.is_open:
            return
        base = os.path.splitext(self.doc.path or "document")[0]
        if as_html:
            out, _ = QFileDialog.getSaveFileName(
                self, "HTMLに書き出し", base + ".html", "HTML (*.html)")
        else:
            out, _ = QFileDialog.getSaveFileName(
                self, "テキストに書き出し", base + ".txt", "テキスト (*.txt)")
        if not out:
            return
        try:
            if as_html:
                self.doc.export_html(out)
            else:
                self.doc.export_text(out)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"書き出せませんでした:\n{exc}")
            return
        self.statusBar().showMessage(f"書き出しました: {out}", 4000)

    # --- しおり / 目次 -------------------------------------------------
    def _add_bookmark(self) -> None:
        if not self.doc.is_open:
            return
        page1 = self.page_view.index + 1
        title, ok = QInputDialog.getText(
            self, "しおりを追加", "タイトル:", text=f"ページ {page1}"
        )
        if not ok or not title.strip():
            return
        self.doc.add_bookmark(title.strip(), page1)
        self.toc_dock.load(self.doc.get_toc())
        self._update_title()
        self.statusBar().showMessage("しおりを追加しました", 3000)

    def _edit_toc(self) -> None:
        if not self.doc.is_open:
            return
        dlg = TocEditDialog(self.doc.get_toc(), self.doc.page_count, self)
        if dlg.exec() == TocEditDialog.DialogCode.Accepted:
            self.doc.set_toc(dlg.result_toc())
            self.toc_dock.load(self.doc.get_toc())
            self._update_title()
            self.statusBar().showMessage("目次を更新しました", 3000)

    # --- 検索 ----------------------------------------------------------
    def _focus_search(self) -> None:
        """検索ポップアップを表示してフォーカスする（Ctrl+F / 検索ボタン）。"""
        pop = self.search_popup
        if not pop.isVisible():
            # メインウィンドウ右上（リボンの下あたり）に出す
            geo = self.geometry()
            ribbon_h = self.ribbon.height() if hasattr(self, "ribbon") else 96
            pop.adjustSize()
            pop.move(geo.right() - pop.width() - 48, geo.top() + ribbon_h + 48)
            pop.show()
        pop.raise_()
        pop.activateWindow()
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def _do_search(self) -> None:
        if not self.doc.is_open:
            return
        query = self.search_edit.text().strip()
        if not query:
            self.page_view.clear_search()
            self.search_count_label.setText("  0 件  ")
            return
        hits = self.doc.search(query)
        self.page_view.set_search_results(hits)
        self._update_search_label()
        if not hits:
            self.statusBar().showMessage(f"「{query}」は見つかりませんでした", 3000)

    def _search_step(self, step: int) -> None:
        if self.page_view.search_count() == 0:
            return
        self.page_view.goto_hit(self.page_view.search_current() + step)
        self.thumbnails.select_page(self.page_view.index)
        self._update_search_label()

    def _update_search_label(self) -> None:
        n = self.page_view.search_count()
        if n == 0:
            self.search_count_label.setText("  0 件  ")
        else:
            self.search_count_label.setText(
                f"  {self.page_view.search_current() + 1} / {n} 件  "
            )

    def _highlight_all_hits(self) -> None:
        if not self.doc.is_open:
            return
        hits = self.page_view._search_hits
        if not hits:
            self.statusBar().showMessage("先に検索してください", 3000)
            return
        c = self._annot_color
        n = self.doc.add_search_highlights(
            hits, (c.redF(), c.greenF(), c.blueF())
        )
        # 影響ページを再描画
        for idx in {h[0] for h in hits}:
            self.page_view.refresh_page(idx)
            self.thumbnails.refresh_page(self.doc, idx)
        self._update_title()
        self.statusBar().showMessage(f"{n} 件をハイライトしました", 3000)

    # --- パスワード保護 / 解除 ----------------------------------------
    def protect_save(self) -> None:
        if not self.doc.is_open:
            return
        pw, ok = QInputDialog.getText(
            self, "パスワード保護", "設定するパスワード:", QLineEdit.EchoMode.Password
        )
        if not ok or not pw:
            return
        pw2, ok = QInputDialog.getText(
            self, "パスワード確認", "もう一度入力:", QLineEdit.EchoMode.Password
        )
        if not ok:
            return
        if pw != pw2:
            QMessageBox.warning(self, "不一致", "パスワードが一致しません")
            return
        base = os.path.splitext(self.doc.path or "document")[0]
        out, _ = QFileDialog.getSaveFileName(
            self, "保護して保存", base + "_protected.pdf", "PDF ファイル (*.pdf)"
        )
        if not out:
            return
        try:
            self.doc.save_encrypted(out, pw)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"保護できませんでした:\n{exc}")
            return
        QMessageBox.information(self, "完了", f"AES-256 で保護して保存しました:\n{out}")

    def unlock_save(self) -> None:
        if not self.doc.is_open:
            return
        if not self.doc.is_encrypted:
            QMessageBox.information(self, "情報", "このPDFは暗号化されていません")
            return
        base = os.path.splitext(self.doc.path or "document")[0]
        out, _ = QFileDialog.getSaveFileName(
            self, "解除して保存", base + "_unlocked.pdf", "PDF ファイル (*.pdf)"
        )
        if not out:
            return
        try:
            self.doc.save_decrypted(out)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"解除できませんでした:\n{exc}")
            return
        QMessageBox.information(self, "完了", f"暗号化を解除して保存しました:\n{out}")

    # --- テキストコピー ------------------------------------------------
    def _copy_text(self) -> None:
        if self.page_view.copy_selection():
            self.statusBar().showMessage("選択テキストをコピーしました", 2000)

    def _on_text_selection_changed(self, has: bool) -> None:
        self._refresh_copy_enabled()
        if has:
            self.statusBar().showMessage(
                "テキスト選択中：Ctrl+C または右クリック→コピー", 0
            )
        else:
            self.statusBar().clearMessage()

    # --- ズーム / 印刷 -------------------------------------------------
    def _on_zoom_preset(self, _idx: int) -> None:
        text = self.zoom_combo.currentText()
        if text == "幅に合わせる":
            self.page_view.fit_width()
        elif text == "縦に合わせる":
            self.page_view.fit_height()
        else:
            try:
                self.page_view.set_zoom(int(text.rstrip("%")) / 100.0)
            except ValueError:
                pass

    def _on_zoom_changed(self, zoom: float) -> None:
        pct = int(round(zoom * 100))
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(max(self.zoom_slider.minimum(),
                                      min(pct, self.zoom_slider.maximum())))
        self.zoom_slider.blockSignals(False)
        self._update_status()
        # 「前回の倍率を維持」モード用に、実際の倍率を都度記憶しておく
        self.settings.setValue("last_zoom", zoom)

    def _toggle_facing(self, on: bool) -> None:
        self.page_view.set_facing(on)
        self.page_view.fit_width()

    def deskew_document(self) -> None:
        if not self.doc.is_open:
            return
        ret = QMessageBox.question(
            self, "傾き補正",
            "スキャン（画像）ページの傾きを補正します。\n"
            "対象ページは画像として作り直されます（テキストのあるページは変更しません）。続行しますか？",
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        dlg = QProgressDialog("傾き補正中…", "キャンセル", 0, self.doc.page_count, self)
        dlg.setWindowTitle("傾き補正")
        dlg.setMinimumDuration(0)

        def progress(done, total):
            dlg.setMaximum(total)
            dlg.setValue(done)
            QApplication.processEvents()
            return not dlg.wasCanceled()

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            n = self.doc.deskew_all(progress=progress)
        except Exception as exc:  # noqa: BLE001
            dlg.close()
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "エラー", f"傾き補正に失敗:\n{exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        dlg.close()
        if n < 0:
            self.statusBar().showMessage("傾き補正をキャンセルしました", 3000)
            return
        self._reload(self.page_view.index)
        self.thumbnails.load(self.doc)
        self._update_title()
        self.statusBar().showMessage(f"{n} ページの傾きを補正しました", 4000)

    # --- バッチ処理 / 比較 ---------------------------------------------
    def batch_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "処理する PDF を選択（複数可）", self._last_dir(), "PDF (*.pdf)"
        )
        if not paths:
            return
        op, ok = QInputDialog.getItem(
            self, "バッチ処理", "操作:",
            ["最適化(圧縮)", "各ページ画像化(PNG 150dpi)", "OCR(日本語+英語)"], 0, False
        )
        if not ok:
            return
        out_dir = QFileDialog.getExistingDirectory(self, "出力先フォルダ")
        if not out_dir:
            return
        tessdata = None
        if op.startswith("OCR"):
            try:
                tessdata = ocr.ensure_languages(["jpn", "eng"])
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "エラー", f"OCR準備に失敗:\n{exc}")
                return

        dlg = QProgressDialog("バッチ処理中…", "キャンセル", 0, len(paths), self)
        dlg.setWindowTitle("バッチ処理")
        dlg.setMinimumDuration(0)
        done, errors = 0, []
        for n, p in enumerate(paths):
            dlg.setValue(n)
            dlg.setLabelText(f"{os.path.basename(p)} ({n + 1}/{len(paths)})")
            QApplication.processEvents()
            if dlg.wasCanceled():
                break
            stem = os.path.splitext(os.path.basename(p))[0]
            try:
                d = PdfDocument()
                d.open(p)
                if d.needs_password:
                    d.close()
                    errors.append(f"{stem}: パスワード保護のためスキップ")
                    continue
                if op.startswith("最適化"):
                    d.compress_to(os.path.join(out_dir, f"{stem}_optimized.pdf"))
                elif op.startswith("各ページ"):
                    d.export_page_images(list(range(d.page_count)), out_dir,
                                         "png", 150, stem)
                else:  # OCR
                    d.ocr_to(os.path.join(out_dir, f"{stem}_ocr.pdf"),
                             "jpn+eng", tessdata, dpi=300)
                d.close()
                done += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{stem}: {exc}")
        dlg.setValue(len(paths))
        msg = f"{done} 件を処理しました。\n出力先: {out_dir}"
        if errors:
            msg += "\n\n[スキップ/失敗]\n" + "\n".join(errors[:10])
        QMessageBox.information(self, "バッチ処理 完了", msg)

    def diff_dialog(self) -> None:
        from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QPlainTextEdit,
                                       QVBoxLayout)
        import difflib
        a, _ = QFileDialog.getOpenFileName(self, "比較するPDF①", self._last_dir(), "PDF (*.pdf)")
        if not a:
            return
        b, _ = QFileDialog.getOpenFileName(self, "比較するPDF②", self._last_dir(), "PDF (*.pdf)")
        if not b:
            return

        def text_of(path):
            d = PdfDocument(); d.open(path)
            if d.needs_password:
                d.close()
                raise RuntimeError("パスワード保護されています")
            t = d.full_text(); d.close()
            return t

        try:
            ta, tb = text_of(a), text_of(b)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"比較できませんでした:\n{exc}")
            return
        diff = list(difflib.unified_diff(
            ta.splitlines(), tb.splitlines(),
            fromfile=os.path.basename(a), tofile=os.path.basename(b), lineterm=""
        ))
        dlg = QDialog(self)
        dlg.setWindowTitle("PDF テキスト比較")
        dlg.resize(720, 560)
        lay = QVBoxLayout(dlg)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setStyleSheet("font-family: Consolas, monospace;")
        view.setPlainText("\n".join(diff) if diff else "差分はありません（テキストは一致）。")
        lay.addWidget(view)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(dlg.reject)
        box.accepted.connect(dlg.accept)
        lay.addWidget(box)
        dlg.exec()

    def print_document(self) -> None:
        """Adobe 風の印刷ダイアログ（プレビュー付き）で印刷する。"""
        if not self.doc.is_open:
            return
        from PySide6.QtPrintSupport import QPrinterInfo

        from .print_dialog import (
            SIZE_ACTUAL, SIZE_CUSTOM, SIZE_FIT, SIZE_SHRINK, PrintDialog,
        )

        dlg = PrintDialog(self.doc, self.page_view.index, self.settings, self)
        if dlg.exec() != PrintDialog.DialogCode.Accepted:
            return
        opt = dlg.options()

        # プリンタを構成
        info = QPrinterInfo.printerInfo(opt["printer"])
        printer = (QPrinter(info, QPrinter.PrinterMode.HighResolution)
                   if not info.isNull() else QPrinter(QPrinter.PrinterMode.HighResolution))
        printer.setCopyCount(opt["copies"])
        if opt["grayscale"]:
            printer.setColorMode(QPrinter.ColorMode.GrayScale)
        if opt["orientation"] == "landscape":
            from PySide6.QtGui import QPageLayout
            printer.setPageOrientation(QPageLayout.Orientation.Landscape)

        # 対象ページ
        total = self.doc.page_count
        if opt["page_mode"] == "current":
            pages = [self.page_view.index]
        elif opt["page_mode"] == "range":
            try:
                pages = _parse_ranges(opt["range_text"], total)
            except ValueError as exc:
                QMessageBox.information(self, "入力エラー", str(exc))
                return
        else:
            pages = list(range(total))
        pages = [i for i in pages if 0 <= i < total]
        if not pages:
            return

        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainter

        res = printer.resolution() or 300           # プリンタの dpi
        render_dpi = min(res, 300)                   # 描画は 300dpi で頭打ち（高速・高画質）
        zoom = render_dpi / 72.0
        size_mode = opt["size_mode"]
        custom_scale = opt["custom_scale"]
        auto_rotate = opt["orientation"] == "auto"

        painter = QPainter(printer)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        page_rect = printer.pageRect(QPrinter.Unit.DevicePixel)  # 印刷可能領域(px)
        progress = QProgressDialog("印刷を準備中…", "キャンセル", 0, len(pages), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(400)
        try:
            for n, i in enumerate(pages):
                progress.setValue(n)
                if progress.wasCanceled():
                    painter.end()
                    self.statusBar().showMessage("印刷をキャンセルしました", 3000)
                    return
                if n > 0:
                    printer.newPage()
                # ページの実寸(pt) → 紙面ピクセル数（1pt = 1/72inch）
                w_pt, h_pt = self.doc.page_pixel_size(i, 1.0)
                nat_w = w_pt / 72.0 * res
                nat_h = h_pt / 72.0 * res
                if nat_w <= 0 or nat_h <= 0:
                    continue
                # 向き=自動: 内容と用紙の縦横が食い違うときは内容を 90° 回して印刷
                rotate = auto_rotate and (
                    (nat_w > nat_h) != (page_rect.width() > page_rect.height()))
                eff_w, eff_h = (nat_h, nat_w) if rotate else (nat_w, nat_h)
                fit = min(page_rect.width() / eff_w, page_rect.height() / eff_h)
                if size_mode == SIZE_ACTUAL:
                    s = 1.0
                elif size_mode == SIZE_FIT:
                    s = fit
                elif size_mode == SIZE_SHRINK:
                    s = min(1.0, fit)
                else:  # SIZE_CUSTOM
                    s = custom_scale
                tgt_w = eff_w * s
                tgt_h = eff_h * s
                x = page_rect.left() + (page_rect.width() - tgt_w) / 2
                y = page_rect.top() + (page_rect.height() - tgt_h) / 2
                pix = self.doc.render_page(i, zoom=zoom)
                src = QRectF(0, 0, pix.width(), pix.height())
                if rotate:
                    painter.save()
                    painter.translate(x + tgt_w / 2, y + tgt_h / 2)
                    painter.rotate(90)
                    painter.drawPixmap(
                        QRectF(-tgt_h / 2, -tgt_w / 2, tgt_h, tgt_w), pix, src)
                    painter.restore()
                else:
                    painter.drawPixmap(QRectF(x, y, tgt_w, tgt_h), pix, src)
            progress.setValue(len(pages))
        finally:
            if painter.isActive():
                painter.end()
        self.statusBar().showMessage("印刷ジョブを送信しました", 3000)

    # --- 外部アプリで開く（Acrobat） -----------------------------------
    def open_in_acrobat(self) -> None:
        """表示中の PDF を Adobe Acrobat（無ければ既定アプリ選択）で開く。

        このアプリはファイルをメモリに読み込んで表示している（ロックしない）
        ため、同じファイルを他のアプリで同時に開ける。
        """
        if not self.doc.is_open:
            return
        path = self.doc.path
        if not path or not os.path.exists(path):
            QMessageBox.information(
                self, "Acrobatで開く",
                "このドキュメントはまだファイルとして保存されていません。\n"
                "先に「名前を付けて保存」してから開いてください。",
            )
            return
        # 未保存の変更は Acrobat 側には見えない（ディスク上の最終保存状態が開く）
        if self.doc.modified:
            ret = QMessageBox.question(
                self, "Acrobatで開く",
                "保存していない変更があります。\n"
                "保存してから開きますか？（「いいえ」の場合、最後に保存した"
                "状態が Acrobat で開きます）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
            )
            if ret == QMessageBox.StandardButton.Cancel:
                return
            if ret == QMessageBox.StandardButton.Yes:
                self.save()
                if self.doc.modified:  # 保存がキャンセル/失敗した場合は中止
                    return

        import subprocess
        exe = _find_acrobat()
        if exe:
            try:
                subprocess.Popen([exe, path], close_fds=True)
                self.statusBar().showMessage("Adobe Acrobat で開きました", 3000)
                return
            except OSError as exc:
                QMessageBox.warning(
                    self, "Acrobatで開く", f"Acrobat の起動に失敗しました:\n{exc}"
                )
                return
        # Acrobat が見つからない → Windows の「プログラムから開く」で代替
        ret = QMessageBox.question(
            self, "Acrobatで開く",
            "Adobe Acrobat が見つかりませんでした。\n"
            "Windows の「プログラムから開く」ダイアログで開くアプリを選びますか？",
        )
        if ret == QMessageBox.StandardButton.Yes:
            subprocess.Popen(
                ["rundll32.exe", "shell32.dll,OpenAs_RunDLL", path], close_fds=True
            )

    # --- 未対応フォーム機能の検知・報告 --------------------------------
    def _check_unsupported_govform(self, doc) -> None:
        """開いたフォームに未対応の入力補助があれば、そっと知らせる。

        官公庁フォームは共有 JS ライブラリで作られており、稀にこのアプリが
        まだ対応していない補助関数を使う。その場合は「選べない/丸がつかない」
        欄が出るため、ユーザーが気づけるよう通知し、報告用に書き出せるように
        アクションを有効化する。
        """
        self._unsupported_helpers = []
        try:
            names = doc.govform_unsupported_helpers()
        except Exception:  # noqa: BLE001
            names = []
        self._unsupported_helpers = names
        self.act_report_govform.setEnabled(bool(names))
        if names:
            self.statusBar().showMessage(
                f"このフォームには未対応の入力補助が {len(names)} 件あります。"
                "［ヘルプ > 未対応フォーム機能を報告用に保存］で書き出せます。", 8000)
            self._maybe_send_govform_telemetry(doc)

    def _telemetry_url(self) -> str:
        """テレメトリ送信先（version.py の定数を設定で上書き可）。空=送信しない。"""
        from .version import TELEMETRY_URL
        return self.settings.value("telemetry_url", "", str) or TELEMETRY_URL

    def _maybe_send_govform_telemetry(self, doc) -> None:
        """同意済みなら、未対応フォームの匿名レポートを自動送信する。

        送信先URL未設定なら何もしない（同意も尋ねない）。未決定なら初回だけ
        同意を尋ね、既定は「送らない」。送るのは関数名＋フォーム定義＋版＋指紋
        だけで、利用者の入力値・氏名・ファイル名は含めない。
        """
        from . import telemetry
        url = self._telemetry_url()
        if not url:
            return  # 配布者が受け口を設定していない → 送信機能そのものが無効
        state = telemetry.consent_state(self.settings)
        if state == "":  # 未決定 → 初回だけ同意を尋ねる
            state = "yes" if self._ask_telemetry_consent() else "no"
        if state != "yes":
            return
        try:
            payload = doc.govform_telemetry_payload(app_version=self._app_version())
        except Exception:  # noqa: BLE001
            payload = None
        if not payload:
            return
        fp = payload.get("form_fingerprint", "")
        if fp and telemetry.already_sent(self.settings, fp):
            return  # 同じフォームは一度だけ
        telemetry.send_async(url, payload)
        if fp:
            telemetry.mark_sent(self.settings, fp)
        self.statusBar().showMessage("未対応フォームの匿名レポートを送信しました", 4000)

    def _ask_telemetry_consent(self) -> bool:
        """初回の同意確認。既定（Escや×）は「送らない」。"""
        from . import telemetry
        box = QMessageBox(self)
        box.setWindowTitle("匿名レポートの送信")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            "未対応の入力補助を含むフォームを開きました。\n"
            "改善のため、匿名レポートを開発元へ送信してもよろしいですか？")
        box.setInformativeText(
            "送信する内容: 未対応の関数名 / フォーム側の定義 / アプリ・ライブラリ版 /\n"
            "　フォームの指紋（同一判定用）\n"
            "送信しない内容: あなたが入力した値・氏名・住所等の個人情報・ファイル名\n\n"
            "この選択は［ヘルプ > 匿名レポートを送信する］でいつでも変更できます。")
        yes = box.addButton("送信する", QMessageBox.ButtonRole.YesRole)
        no = box.addButton("送信しない", QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(no)
        box.exec()
        agreed = box.clickedButton() is yes
        telemetry.set_consent(self.settings, agreed)
        self.act_telemetry_toggle.blockSignals(True)
        self.act_telemetry_toggle.setChecked(agreed)
        self.act_telemetry_toggle.blockSignals(False)
        return agreed

    def _app_version(self) -> str:
        from .version import APP_VERSION
        return APP_VERSION

    def _toggle_telemetry(self, on: bool) -> None:
        from . import telemetry
        telemetry.set_consent(self.settings, on)
        self.statusBar().showMessage(
            "匿名レポートの送信を{}にしました".format("オン" if on else "オフ"), 4000)

    def report_govform_unsupported(self) -> None:
        """未対応ヘルパーの診断テキストを保存する（報告用）。

        ネットワークへは送らない。書き出したファイルをユーザーが開発元へ
        送ることで対応追加できる、という導線。
        """
        if not self.doc.is_open:
            return
        names = getattr(self, "_unsupported_helpers", []) or []
        if not names:
            QMessageBox.information(
                self, "未対応フォーム機能の報告",
                "このフォームに未対応の入力補助は見つかりませんでした。")
            return
        base = "TriVReader_未対応フォーム機能.txt"
        if self.doc.path:
            base = os.path.splitext(os.path.basename(self.doc.path))[0] + "_未対応機能.txt"
        out, _ = QFileDialog.getSaveFileName(
            self, "報告用に保存", os.path.join(self._last_dir(), base),
            "テキスト (*.txt)")
        if not out:
            return
        try:
            from .version import APP_VERSION
            written = self.doc.write_govform_diagnostic(out, app_version=APP_VERSION)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "報告用に保存", f"書き出しに失敗しました:\n{exc}")
            return
        QMessageBox.information(
            self, "報告用に保存",
            f"未対応の入力補助 {len(written)} 件を書き出しました:\n{out}\n\n"
            "このファイルを開発元へお送りいただくと、対応追加を検討できます。\n"
            "（フォーム側の定義のみを含み、入力した値は含まれません）")

    def set_as_default_pdf_app(self) -> None:
        """TriV-Reader を .pdf の候補アプリとして登録し、既定アプリ設定を開く。

        Windows 10/11 は既定アプリの乗っ取り防止のため、レジストリ登録だけでは
        既定にできない（UserChoice はハッシュ保護されており OS のみが書ける）。
        ここでは「開くアプリの候補」への登録までを行い、実際に既定へ選ぶのは
        Windows の設定画面でユーザー自身に行ってもらう。
        """
        from . import associate

        exe = associate.current_exe()
        if not exe:
            QMessageBox.information(
                self, "既定のPDFアプリに設定",
                "ソース実行では利用できません。\n"
                "ビルド済み(インストール済み)の TriV-Reader から実行してください。",
            )
            return
        try:
            associate.register(exe)
        except OSError as exc:
            QMessageBox.warning(
                self, "既定のPDFアプリに設定",
                f"関連付けの登録に失敗しました:\n{exc}",
            )
            return
        QMessageBox.information(
            self, "既定のPDFアプリに設定",
            "TriV-Reader をPDFの候補アプリとして登録しました。\n\n"
            "続けて開く「既定のアプリ」設定で、.pdf の項目から\n"
            "「TriV-Reader」を選んでください。\n"
            "（Windowsの仕様上、既定への変更はユーザー操作が必要です）",
        )
        try:
            associate.open_default_apps_settings()
        except OSError as exc:
            QMessageBox.warning(
                self, "既定のPDFアプリに設定",
                f"設定画面を開けませんでした:\n{exc}\n\n"
                "手動で［設定 > アプリ > 既定のアプリ］を開いてください。",
            )

    # --- 設定の永続化 / 最近のファイル / D&D --------------------------
    def _last_dir(self) -> str:
        return self.settings.value("last_dir", "", str)

    def _add_recent(self, path: str) -> None:
        self.settings.setValue("last_dir", os.path.dirname(path))
        recent = self.settings.value("recent", [], list) or []
        recent = [p for p in recent if p != path]
        recent.insert(0, path)
        self.settings.setValue("recent", recent[:8])
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.clear()
        recent = self.settings.value("recent", [], list) or []
        for p in recent:
            act = self.recent_menu.addAction(os.path.basename(p))
            act.setData(p)
            act.triggered.connect(lambda _c=False, path=p: self.open_path(path))
        self.recent_menu.setEnabled(bool(recent))

    def _restore_settings(self) -> None:
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        dark = self.settings.value("dark", False, bool)
        if dark:
            self.act_dark.setChecked(True)  # toggled シグナルでテーマ適用
        # キャンバス背景色を現在テーマに合わせる（ライト起動時も適用）
        self.page_view.set_canvas_color(theme.canvas_color(dark))
        if self.settings.value("compact", False, bool):
            self.act_compact.setChecked(True)  # toggled で適用
        # ツールバーの表示/非表示（折りたたみ）を復元
        self.annot_toolbar.setVisible(self.settings.value("annot_bar", True, bool))
        # ドック（ページ一覧/しおり/注釈一覧）の表示状態を復元
        # （前回消していたら消したまま）
        self.page_dock.setVisible(self.settings.value("page_dock_visible", True, bool))
        self.toc_dock.setVisible(self.settings.value("toc_dock_visible", True, bool))
        self.annot_dock.setVisible(
            self.settings.value("annot_dock_visible", True, bool))
        # 前回どのパネルを手前（タブの前面）にしていたかを復元
        active = self.settings.value("left_dock_active", "page", str)
        d = {"page": self.page_dock, "toc": self.toc_dock,
             "annot": self.annot_dock}.get(active)
        if d is not None and not d.isHidden():
            d.raise_()
        # 開いた時の既定の表示倍率（未設定なら「幅に合わせる」）
        mode = self.settings.value("default_zoom_mode", "fit", str)
        act = self.default_zoom_actions.get(mode, self.default_zoom_actions["fit"])
        act.setChecked(True)
        # 匿名レポート送信トグル: 送信先URL未設定なら意味がないので隠す。
        # 設定済みなら現在の同意状態を反映する。
        from . import telemetry
        has_url = bool(self._telemetry_url())
        self.act_telemetry_toggle.setVisible(has_url)
        self.act_telemetry_toggle.blockSignals(True)
        self.act_telemetry_toggle.setChecked(
            telemetry.consent_state(self.settings) == "yes")
        self.act_telemetry_toggle.blockSignals(False)
        self._rebuild_recent_menu()

    def _save_settings(self) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("dark", self.act_dark.isChecked())
        self.settings.setValue("compact", self.act_compact.isChecked())
        self.settings.setValue("annot_bar", self.annot_toolbar.isVisible())
        # ドックの表示状態（タブ化で裏に隠れていても「表示中」として扱う）
        self.settings.setValue("page_dock_visible", not self.page_dock.isHidden())
        self.settings.setValue("toc_dock_visible", not self.toc_dock.isHidden())
        self.settings.setValue("annot_dock_visible", not self.annot_dock.isHidden())
        # 手前（タブの前面）に出ているパネルを記憶する。タブ化されたドックは
        # 前面の1つだけ isVisible() が True になる。
        active = ""
        for name, d in (("page", self.page_dock), ("toc", self.toc_dock),
                        ("annot", self.annot_dock)):
            if d.isVisible():
                active = name
                break
        if active:
            self.settings.setValue("left_dock_active", active)
        if hasattr(self, "ribbon"):
            self.settings.setValue("ribbon_collapsed", self.ribbon.is_collapsed())

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls() and any(
            u.toLocalFile().lower().endswith(".pdf") for u in event.mimeData().urls()
        ):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        for u in event.mimeData().urls():
            p = u.toLocalFile()
            if p.lower().endswith(".pdf"):
                self.open_path(p)
                break

    # --- 更新 / バージョン情報 ----------------------------------------
    def show_about(self) -> None:
        from . import updater
        from .version import APP_VERSION

        QMessageBox.information(
            self, "バージョン情報",
            f"TriV-Reader\nバージョン {APP_VERSION}\n"
            f"{'（凍結ビルド）' if updater.is_frozen() else '（ソース実行）'}",
        )

    def set_update_url(self) -> None:
        from . import updater

        cur = updater.manifest_url(self.settings)
        url, ok = QInputDialog.getText(
            self, "更新元URLを設定",
            "update.json の公開URL（GitHub Releases 等）:", text=cur
        )
        if ok:
            self.settings.setValue("update_url", url.strip())
            self.statusBar().showMessage("更新元URLを保存しました", 3000)

    def check_for_update(self) -> None:
        from . import updater

        if not updater.is_frozen():
            QMessageBox.information(
                self, "更新の確認",
                "ソース実行では自動更新は不要です。\n"
                "依存の更新は update.ps1（pip 更新＋再ビルド）をご利用ください。",
            )
            return
        url = updater.manifest_url(self.settings)
        if not url:
            QMessageBox.information(
                self, "更新元が未設定",
                "更新元URLが設定されていません。\n"
                "［ヘルプ > 更新元URLを設定］で update.json のURLを設定してください。",
            )
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            info = updater.check(url)
        except Exception as exc:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "更新の確認", f"確認できませんでした:\n{exc}")
            return
        QApplication.restoreOverrideCursor()
        if not info:
            QMessageBox.information(self, "更新の確認", "お使いのバージョンは最新です。")
            return

        notes = info.get("notes", "")
        ret = QMessageBox.question(
            self, "更新があります",
            f"新しいバージョン {info['version']} があります。\n\n{notes}\n\n"
            "今すぐ更新しますか？（ダウンロード後、アプリを再起動します）",
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        dlg = QProgressDialog("ダウンロード中…", "キャンセル", 0, 100, self)
        dlg.setWindowTitle("更新")
        dlg.setMinimumDuration(0)

        def progress(done, total):
            if total > 0:
                dlg.setValue(int(done * 100 / total))
            QApplication.processEvents()
            return not dlg.wasCanceled()

        try:
            zip_path = updater.download(info["url"], progress=progress)
        except Exception as exc:  # noqa: BLE001
            dlg.close()
            QMessageBox.warning(self, "更新", f"ダウンロードに失敗しました:\n{exc}")
            return
        dlg.close()
        # 改ざん検証（update.json に sha256 があれば照合）
        if not updater.verify(zip_path, info.get("sha256", "")):
            QMessageBox.critical(
                self, "更新",
                "ダウンロードしたファイルの検証(SHA256)に失敗しました。\n"
                "更新を中止します。",
            )
            return
        try:
            updater.apply_and_restart(zip_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "更新", f"更新の適用に失敗しました:\n{exc}")
            return
        QMessageBox.information(
            self, "更新", "アプリを終了して更新を適用します。\n自動的に再起動します。"
        )
        QApplication.instance().quit()

    # --- テーマ --------------------------------------------------------
    def _toggle_dark(self, on: bool) -> None:
        theme.apply_theme(QApplication.instance(), on)
        canvas = theme.canvas_color(on)
        # 全タブ＋予備ビューに反映
        self._spare_view.set_canvas_color(canvas)
        for i in range(self.tabs.count()):
            t = self.tabs.widget(i)
            if isinstance(t, DocTab):
                t.page_view.set_canvas_color(canvas)
                t.organizer.apply_theme(on)
        self._assign_icons()  # 記号アイコンの色をテーマに追従させる

    # --- ページ番号 ----------------------------------------------------
    def add_page_numbers_dialog(self) -> None:
        if not self.doc.is_open:
            return
        pos_map = {
            "下中央": "bottom-center", "下右": "bottom-right", "下左": "bottom-left",
            "上中央": "top-center", "上右": "top-right", "上左": "top-left",
        }
        pos_label, ok = QInputDialog.getItem(
            self, "ページ番号", "位置:", list(pos_map.keys()), 0, False
        )
        if not ok:
            return
        fmt, ok = QInputDialog.getItem(
            self, "ページ番号", "書式:",
            ["{n}", "{n} / {total}", "- {n} -", "Page {n}"], 1, True
        )
        if not ok or "{n}" not in fmt:
            if ok:
                QMessageBox.information(self, "書式エラー", "{n} を含めてください")
            return
        start, ok = QInputDialog.getInt(
            self, "ページ番号", "開始番号:", value=1, minValue=0
        )
        if not ok:
            return
        try:
            self.doc.add_page_numbers(pos_map[pos_label], fmt, start=start)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"追加できませんでした:\n{exc}")
            return
        self.page_view.reload(self.page_view.index)
        self.thumbnails.load(self.doc)
        self._update_title()
        self.statusBar().showMessage("ページ番号を追加しました", 3000)

    # --- 最適化 / PDF-A ------------------------------------------------
    def compress_save(self) -> None:
        if not self.doc.is_open:
            return
        before = os.path.getsize(self.doc.path) if self.doc.path and os.path.exists(self.doc.path) else 0
        base = os.path.splitext(self.doc.path or "document")[0]
        out, _ = QFileDialog.getSaveFileName(
            self, "最適化して保存", base + "_optimized.pdf", "PDF ファイル (*.pdf)"
        )
        if not out:
            return
        try:
            self.doc.compress_to(out)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"最適化できませんでした:\n{exc}")
            return
        after = os.path.getsize(out)
        msg = f"最適化して保存しました:\n{out}\n\nサイズ: {after:,} バイト"
        if before:
            msg += f"（元 {before:,} バイト / {100 * after // before}%）"
        QMessageBox.information(self, "完了", msg)

    def export_pdfa(self) -> None:
        if not self.doc.is_open:
            return
        base = os.path.splitext(self.doc.path or "document")[0]
        out, _ = QFileDialog.getSaveFileName(
            self, "PDF/A で書き出し", base + "_pdfa.pdf", "PDF ファイル (*.pdf)"
        )
        if not out:
            return
        try:
            self.doc.export_pdfa(out)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"PDF/A 化できませんでした:\n{exc}")
            return
        QMessageBox.information(
            self, "PDF/A 完了",
            f"PDF/A-2b として書き出しました:\n{out}\n\n"
            "※ sRGB の OutputIntent と PDF/A 識別情報を付与しています。"
            "厳密な準拠検証は専用バリデータの利用を推奨します。",
        )

    # --- ページ整理画面の切り替え -------------------------------------
    def _toggle_organize(self, on: bool) -> None:
        tab = self._active_tab()
        if tab is None:
            return
        if on:
            tab.organizer.load(tab.doc)
            tab.setCurrentWidget(tab.organizer)
        else:
            tab.setCurrentWidget(tab.page_view)

    # --- フォーム入力の切り替え ---------------------------------------
    def _toggle_form(self, on: bool) -> None:
        tab = self._active_tab()
        if tab is None:
            return
        if on and tab.currentWidget() is tab.organizer:
            # 整理画面ではなくビューワーに切り替えてから入力
            self.act_organize.setChecked(False)
        tab.page_view.set_form_mode(on)
        if on:
            self.statusBar().showMessage(
                "フォーム入力モード: 黄色い欄をクリックして入力できます", 4000)

    def _on_form_changed(self) -> None:
        """フォーム欄の値が変わったら未保存状態を反映。"""
        self._update_title()
        self._update_actions()

    def _jump_from_organizer(self, index: int) -> None:
        """整理画面でダブルクリックされたページをビューワーで開く。"""
        self.act_organize.setChecked(False)  # ビューワーへ戻す
        self._goto(index)

    # --- ページ編集（Step 3） ------------------------------------------
    def _reload(self, target: int) -> None:
        """ページ構成変更後に全ビュー（サイドバー・ビューワー・整理画面）を再構築。"""
        self.thumbnails.load(self.doc)
        self.page_view.reload(target)
        self.organizer.load(self.doc)
        self.thumbnails.select_page(self.page_view.index)
        self._update_status()
        self._update_title()

    def _delete_page(self, index: int) -> None:
        if not self.doc.is_open:
            return
        try:
            self.doc.delete_page(index)
        except ValueError as exc:
            QMessageBox.information(self, "削除できません", str(exc))
            return
        self._reload(min(index, self.doc.page_count - 1))

    def _delete_pages(self, indices: list) -> None:
        if not self.doc.is_open or not indices:
            return
        try:
            self.doc.delete_pages([int(i) for i in indices])
        except ValueError as exc:
            QMessageBox.information(self, "削除できません", str(exc))
            return
        self._reload(min(min(indices), self.doc.page_count - 1))

    def _rotate_pages(self, indices: list, delta: int) -> None:
        if not self.doc.is_open or not indices:
            return
        for i in indices:
            self.doc.rotate_page(int(i), delta)
        self._reload(self.page_view.index)

    def _reorder(self, new_order: list) -> None:
        if not self.doc.is_open:
            return
        # 並べ替え後に、元の表示ページがどこへ移ったかを追う
        current = self.page_view.index
        try:
            target = new_order.index(current)
        except ValueError:
            target = 0
        self.doc.reorder([int(x) for x in new_order])
        self._reload(target)

    def _reorder_from_organizer(self, new_order: list, new_selected: list) -> None:
        """整理画面での並べ替え。再読み込み後に選択を復元し、連続操作できるようにする。"""
        if not self.doc.is_open:
            return
        self.doc.reorder([int(x) for x in new_order])
        # ビューワー側の現在ページは新しい位置へ追随
        current = self.page_view.index
        try:
            target = new_order.index(current)
        except ValueError:
            target = 0
        self._reload(target)
        self.organizer.select_positions([int(p) for p in new_selected])

    def merge_pdf(self) -> None:
        if not self.doc.is_open:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "末尾に統合する PDF を選択（複数可）", self._last_dir(),
            "PDF ファイル (*.pdf)"
        )
        if not paths:
            return
        total = 0
        try:
            for path in paths:  # 選んだ順に末尾へ追加
                total += self.doc.insert_pdf(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"統合できませんでした:\n{exc}")
            if not total:
                return
        self._reload(self.doc.page_count - 1)
        self.statusBar().showMessage(
            f"{len(paths)} ファイル（{total} ページ）を末尾に統合しました", 4000)

    def _insert_from_organizer(self, kind: str, at: int) -> None:
        """ページ整理の「挿入」。kind='file'|'blank'、at=挿入位置。"""
        if not self.doc.is_open:
            return
        at = max(0, min(at, self.doc.page_count))
        if kind == "blank":
            self.doc.insert_blank_page(at)
            self._reload(at)
            self.statusBar().showMessage("白紙ページを挿入しました", 3000)
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "挿入する PDF を選択（複数可）", self._last_dir(),
            "PDF ファイル (*.pdf)"
        )
        if not paths:
            return
        pos = at
        total = 0
        try:
            for path in paths:
                n = self.doc.insert_pdf(path, at=pos)
                pos += n
                total += n
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"挿入できませんでした:\n{exc}")
            if not total:
                return
        self._reload(at)
        self.statusBar().showMessage(f"{total} ページを挿入しました", 3000)

    def extract_pages(self) -> None:
        if not self.doc.is_open:
            return
        text, ok = QInputDialog.getText(
            self,
            "ページ抽出",
            "抽出するページ（例: 1-3,5,8）:",
            text=f"{self.page_view.index + 1}",
        )
        if not ok or not text.strip():
            return
        try:
            indices = _parse_ranges(text, self.doc.page_count)
        except ValueError as exc:
            QMessageBox.information(self, "入力エラー", str(exc))
            return
        self._extract_indices(indices)

    def _extract_indices(self, indices: list) -> None:
        """指定インデックス（0始まり）のページを別ファイルへ抽出する。"""
        if not self.doc.is_open or not indices:
            return
        indices = [int(i) for i in indices]
        out, _ = QFileDialog.getSaveFileName(
            self, "抽出結果を保存", "extracted.pdf", "PDF ファイル (*.pdf)"
        )
        if not out:
            return
        try:
            self.doc.extract_to(indices, out)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"抽出できませんでした:\n{exc}")
            return
        self.statusBar().showMessage(
            f"{len(indices)} ページを抽出しました: {out}", 4000
        )

    def split_pdf(self) -> None:
        if not self.doc.is_open:
            return
        n, ok = QInputDialog.getInt(
            self, "PDF 分割", "何ページごとに分割しますか？", value=1, minValue=1
        )
        if not ok:
            return
        out_dir = QFileDialog.getExistingDirectory(self, "出力先フォルダを選択")
        if not out_dir:
            return
        stem = os.path.splitext(os.path.basename(self.doc.path or "split"))[0]
        try:
            outputs = self.doc.split_every(n, out_dir, stem)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"分割できませんでした:\n{exc}")
            return
        QMessageBox.information(
            self, "分割完了", f"{len(outputs)} 個のファイルを出力しました。\n{out_dir}"
        )

    # --- 画像変換（Step 4） --------------------------------------------
    def export_images_dialog(self) -> None:
        """PDF→画像。ページ範囲を指定して書き出す（ツールバーから）。"""
        if not self.doc.is_open:
            return
        text, ok = QInputDialog.getText(
            self,
            "画像に書き出し",
            "対象ページ（例: 1-3,5 / 空欄で全ページ）:",
            text=f"1-{self.doc.page_count}",
        )
        if not ok:
            return
        try:
            indices = (
                _parse_ranges(text, self.doc.page_count)
                if text.strip()
                else list(range(self.doc.page_count))
            )
        except ValueError as exc:
            QMessageBox.information(self, "入力エラー", str(exc))
            return
        self._export_indices_images(indices)

    def _export_indices_images(self, indices: list) -> None:
        """指定ページを画像として書き出す（整理画面の選択からも呼ばれる）。"""
        if not self.doc.is_open or not indices:
            return
        indices = [int(i) for i in indices]
        fmt, ok = QInputDialog.getItem(
            self, "形式", "画像形式:", ["PNG", "JPEG"], 0, False
        )
        if not ok:
            return
        dpi, ok = QInputDialog.getInt(
            self, "解像度", "DPI（大きいほど高精細）:", value=150, minValue=36, maxValue=600
        )
        if not ok:
            return
        out_dir = QFileDialog.getExistingDirectory(self, "出力先フォルダを選択")
        if not out_dir:
            return
        stem = os.path.splitext(os.path.basename(self.doc.path or "page"))[0]
        try:
            outputs = self.doc.export_page_images(indices, out_dir, fmt, dpi, stem)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"書き出せませんでした:\n{exc}")
            return
        QMessageBox.information(
            self, "完了", f"{len(outputs)} 枚の画像を出力しました。\n{out_dir}"
        )

    def images_to_pdf_dialog(self) -> None:
        """画像→PDF。複数画像から新しい PDF を作る（開いていなくても可）。"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "PDF にする画像を選択（複数可）", "",
            "画像 (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff)",
        )
        if not paths:
            return
        out, _ = QFileDialog.getSaveFileName(
            self, "保存先 PDF", "images.pdf", "PDF ファイル (*.pdf)"
        )
        if not out:
            return
        try:
            PdfDocument.images_to_pdf(paths, out)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"作成できませんでした:\n{exc}")
            return
        ret = QMessageBox.question(
            self, "完了", f"PDF を作成しました:\n{out}\n\n今すぐ開きますか？"
        )
        if ret == QMessageBox.StandardButton.Yes:
            self.open_path(out)  # 新しいタブで開く

    def add_images_as_pages(self) -> None:
        """画像を現在の PDF にページとして取り込む。"""
        if not self.doc.is_open:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "ページとして追加する画像を選択（複数可）", "",
            "画像 (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff)",
        )
        if not paths:
            return
        try:
            added = self.doc.insert_images_as_pages(paths)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"追加できませんでした:\n{exc}")
            return
        self._reload(self.doc.page_count - 1)
        self.statusBar().showMessage(f"{added} ページ（画像）を追加しました", 3000)

    # --- OCR（Step 5） --------------------------------------------------
    def run_ocr(self) -> None:
        """全ページを OCR し、検索可能 PDF を別名で書き出す。"""
        if not self.doc.is_open:
            return
        labels = list(ocr.LANGUAGE_CHOICES.keys())
        label, ok = QInputDialog.getItem(
            self, "文字認識(OCR)", "認識する言語:", labels, 0, False
        )
        if not ok:
            return
        language = ocr.LANGUAGE_CHOICES[label]

        # 必要な言語データを用意（不足分はダウンロード）
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            tessdata = ocr.ensure_languages(language.split("+"))
        except Exception as exc:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(
                self, "OCR 準備エラー",
                f"言語データを用意できませんでした:\n{exc}\n\n"
                "ネットワーク接続をご確認ください。",
            )
            return
        finally:
            QApplication.restoreOverrideCursor()

        base = os.path.splitext(self.doc.path or "document")[0]
        out, _ = QFileDialog.getSaveFileName(
            self, "検索可能 PDF の保存先", base + "_ocr.pdf", "PDF ファイル (*.pdf)"
        )
        if not out:
            return

        dlg = QProgressDialog("OCR 処理中…", "キャンセル", 0, self.doc.page_count, self)
        dlg.setWindowTitle("文字認識(OCR)")
        dlg.setMinimumDuration(0)
        dlg.setValue(0)

        def progress(done: int, total: int) -> bool:
            dlg.setMaximum(total)
            dlg.setValue(done)
            dlg.setLabelText(f"OCR 処理中… {done}/{total} ページ")
            QApplication.processEvents()
            return not dlg.wasCanceled()

        try:
            done = self.doc.ocr_to(out, language, tessdata, dpi=300, progress=progress)
        except Exception as exc:  # noqa: BLE001
            dlg.close()
            QMessageBox.critical(self, "OCR エラー", f"OCR に失敗しました:\n{exc}")
            return
        dlg.close()
        if not done:
            self.statusBar().showMessage("OCR をキャンセルしました", 3000)
            return
        ret = QMessageBox.question(
            self, "OCR 完了",
            f"検索可能 PDF を作成しました:\n{out}\n\n今すぐ開きますか？",
        )
        if ret == QMessageBox.StandardButton.Yes:
            self.open_path(out)  # 新しいタブで開く

    def save(self) -> None:
        if not self.doc.is_open:
            return
        # 上書き保存の確認（「次回から表示しない」を選べる）
        if self.settings.value("confirm_overwrite", True, bool) and self.doc.path:
            from PySide6.QtWidgets import QCheckBox
            box = QMessageBox(self)
            box.setWindowTitle("上書き保存")
            box.setIcon(QMessageBox.Icon.Question)
            box.setText(f"元のファイルに上書き保存します:\n{self.doc.path}\n\nよろしいですか？")
            box.setStandardButtons(QMessageBox.StandardButton.Yes
                                   | QMessageBox.StandardButton.No)
            box.setDefaultButton(QMessageBox.StandardButton.Yes)
            cb = QCheckBox("次回から表示しない")
            box.setCheckBox(cb)
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
            if cb.isChecked():
                self.settings.setValue("confirm_overwrite", False)
        # Documents 配下などはフォルダー保護で上書きが弾かれることがあるため、
        # 失敗したら名前を付けて保存にフォールバックする。
        try:
            self.doc.save()
        except Exception as exc:  # noqa: BLE001
            ret = QMessageBox.question(
                self,
                "上書きできません",
                f"上書き保存に失敗しました:\n{exc}\n\n"
                "別の場所に名前を付けて保存しますか？",
            )
            if ret == QMessageBox.StandardButton.Yes:
                self.save_as()
            return
        self._update_title()
        self.statusBar().showMessage("保存しました", 3000)

    def save_as(self) -> None:
        if not self.doc.is_open:
            return
        suggested = self.doc.path or "untitled.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "名前を付けて保存", suggested, "PDF ファイル (*.pdf)"
        )
        if not path:
            return
        try:
            self.doc.save_as(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"保存できませんでした:\n{exc}")
            return
        self.doc.path = path
        tab = self._active_tab()
        if tab is not None:
            self.tabs.setTabToolTip(self.tabs.indexOf(tab), path)
        self._update_title()
        self.statusBar().showMessage(f"保存しました: {path}", 4000)

    # --- 状態更新 -------------------------------------------------------
    def _update_status(self) -> None:
        if self.doc.is_open:
            self.page_label.setText(
                f"  ページ: {self.page_view.index + 1} / {self.doc.page_count}"
                f"   ズーム: {self.page_view.zoom * 100:.0f}%  "
            )
        else:
            self.page_label.setText("  ページ: - / -  ")
        # リボンのページ番号欄も同期（リボン構築前に呼ばれることがあるためガード）
        if hasattr(self, "page_edit"):
            if self.doc.is_open:
                if not self.page_edit.hasFocus():  # 入力中は上書きしない
                    self.page_edit.setText(str(self.page_view.index + 1))
                self.page_total_label.setText(f" / {self.doc.page_count}")
                self.page_edit.setEnabled(True)
            else:
                self.page_edit.clear()
                self.page_total_label.setText(" / -")
                self.page_edit.setEnabled(False)

    def _update_title(self) -> None:
        tab = self._active_tab()
        if tab is None:
            self.setWindowTitle("TriV-Reader")
            return
        name = os.path.basename(tab.doc.path) if tab.doc.path else "(無題)"
        mark = "*" if tab.doc.modified else ""
        self.setWindowTitle(f"TriV-Reader — {mark}{name}")
        # タブ見出しにも未保存マークを反映
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            self.tabs.setTabText(idx, f"{mark}{name}")

    def _update_actions(self) -> None:
        has = self.doc.is_open
        for act in (
            self.act_prev,
            self.act_next,
            self.act_zoom_in,
            self.act_zoom_out,
            self.act_fit,
            self.act_fit_height,
            self.act_rotate_left,
            self.act_rotate_right,
            self.act_save,
            self.act_save_as,
            self.act_delete,
            self.act_merge,
            self.act_extract,
            self.act_split,
            self.act_organize,
            self.act_to_images,
            self.act_add_images,
            self.act_ocr,
            self.act_color,
            self.act_clear_annots,
            self.act_add_bookmark,
            self.act_edit_toc,
            self.act_compress,
            self.act_export_pdfa,
            self.act_page_numbers,
            self.act_find,
            self.act_search_next,
            self.act_search_prev,
            self.act_highlight_all,
            self.act_protect,
            self.act_unlock,
            self.act_apply_redact,
            self.act_blank_page,
            self.act_duplicate_page,
            self.act_autocrop,
            self.act_watermark,
            self.act_header_footer,
            self.act_metadata,
            self.act_export_text,
            self.act_export_html,
            self.act_print,
            self.act_open_acrobat,
            self.act_facing,
            self.act_deskew,
            *self.tool_group.actions(),
        ):
            act.setEnabled(has)
        self.width_spin.setEnabled(has)
        self.search_edit.setEnabled(has)
        # 画像→PDF は PDF を開いていなくても使える
        self.act_from_images.setEnabled(True)
        # フォーム入力は「入力欄付きPDF」を開いているときだけ有効
        is_form = has and self.doc.is_form()
        self.act_form.setEnabled(is_form)
        if not is_form and self.act_form.isChecked():
            self.act_form.blockSignals(True)
            self.act_form.setChecked(False)
            self.act_form.blockSignals(False)

    # --- 終了確認 -------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        # 通常の×でタブがあるときは「現在のタブ / 全て / キャンセル」を尋ねる。
        # （トレイの「終了」からの呼び出し _force_quit ではダイアログを出さない）
        if not self._force_quit and self.tabs.count() > 0:
            box = QMessageBox(self)
            box.setWindowTitle("ウィンドウを閉じる")
            box.setIcon(QMessageBox.Icon.Question)
            box.setText("ウィンドウを閉じます。どうしますか？")
            b_current = box.addButton("現在のタブを閉じる",
                                      QMessageBox.ButtonRole.AcceptRole)
            b_all = box.addButton("全てのタブを閉じる",
                                  QMessageBox.ButtonRole.DestructiveRole)
            b_cancel = box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(b_cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is None or clicked is b_cancel:
                event.ignore()
                return
            if clicked is b_current:
                idx = self.tabs.currentIndex()
                if idx >= 0:
                    self._close_tab(idx)  # 未保存確認は _close_tab 内で行う
                event.ignore()            # ウィンドウ自体は残す
                return
            # b_all → このまま全タブを閉じる処理へ進む

        # 全タブを閉じる（各タブの未保存は _close_tab が確認。中断されたら閉じない）
        while self.tabs.count() > 0:
            before = self.tabs.count()
            self._close_tab(0)
            if self.tabs.count() == before:  # 未保存確認でキャンセルされた
                self._force_quit = False
                event.ignore()
                return

        # トレイ常駐が有効で通常の×なら、終了せずトレイへ隠す（通知は出さない）
        if not self._force_quit and self._tray_resident_enabled():
            event.ignore()
            self.hide()
            return

        self._save_settings()
        if getattr(self, "tray", None) is not None:
            self.tray.hide()
        event.accept()
        QApplication.instance().quit()
