"""使い勝手まわりの小機能（MainWindow から切り出した Mixin）。

- ページ内リンク（目次のジャンプ・URL）を辿る／戻る・進む（Alt+←/→）
- 全画面表示（F11、Esc で戻る）
- キーボードショートカット一覧（F1）
- タブの右クリックメニュー（エクスプローラーで表示・パスをコピー・他を閉じる）
- 注釈・フォーム入力値を固定して保存（フラット化）
- スナップショット（範囲を画像としてコピー）の後始末

どれも「PDF を毎日開く人が、無いと地味に困る」もの。document / page_view 側の
機能を MainWindow の action に束ねるだけで、ロジックはなるべく持たない。
"""
from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QUrl
from PySide6.QtGui import (
    QAction, QColor, QDesktopServices, QKeySequence, QPainter, QPen, QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHeaderView,
    QLabel,
    QMessageBox,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
)


class DocTabBar(QTabBar):
    """文書タブのタブバー。タブ幅を [TAB_MIN, TAB_MAX] に収める。

    QSS の max-width は最初のレイアウトでは効かず（後の再スタイルで急に
    縮む）、min-width が無いとタブが増えたとき全部が「…」まで潰れる。
    sizeHint 側で決めておけば、収まらない分は素直にスクロール矢印になる。
    """

    TAB_MIN = 110
    TAB_MAX = 260

    def tabSizeHint(self, index: int) -> QSize:  # noqa: N802 (Qt 命名)
        s = super().tabSizeHint(index)
        s.setWidth(max(self.TAB_MIN, min(s.width(), self.TAB_MAX)))
        return s

    def minimumTabSizeHint(self, index: int) -> QSize:  # noqa: N802
        s = super().minimumTabSizeHint(index)
        s.setWidth(self.TAB_MIN)
        return s

    # タブが収まらないときに出る左右のスクロールボタン。Fusion + QSS では
    # 矢印の記号が描かれず（枠だけになる）、特にダークで存在が分からない。
    # Qt はレイアウトのたびに arrowType を設定し直すのでアイコン差し替えは
    # 効かず、描画イベントを横取りしてテーマの文字色でシェブロンを描く。
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._arrow_painters: list[_ScrollArrowPainter] = []
        for btn in self.findChildren(QToolButton):
            side = {"ScrollLeftButton": "left",
                    "ScrollRightButton": "right"}.get(btn.objectName())
            if side:
                self._arrow_painters.append(_ScrollArrowPainter(btn, side))

    def apply_theme(self, dark: bool) -> None:
        from . import theme

        c = theme._tokens(dark)
        for ap in self._arrow_painters:
            ap.set_colors(QColor(c["text"]), QColor(c["border_strong"]))


class _ScrollArrowPainter(QObject):
    """タブバーのスクロールボタンに ‹ › を描く（Paint イベントを置き換える）。"""

    def __init__(self, btn: QToolButton, side: str) -> None:
        super().__init__(btn)
        self._btn = btn
        self._side = side
        self._color = QColor("#161a21")
        self._disabled = QColor("#c9cfd8")
        btn.installEventFilter(self)

    def set_colors(self, color: QColor, disabled: QColor) -> None:
        self._color = color
        self._disabled = disabled
        self._btn.update()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt 命名)
        if obj is self._btn and event.type() == QEvent.Type.Paint:
            from PySide6.QtCore import QPointF
            from PySide6.QtWidgets import QStyle, QStyleOptionToolButton

            btn = self._btn
            opt = QStyleOptionToolButton()
            btn.initStyleOption(opt)
            # 矢印は自前で描くので、スタイル側の矢印描画は外して面と枠だけ描かせる
            opt.features = QStyleOptionToolButton.ToolButtonFeature.None_
            opt.arrowType = Qt.ArrowType.NoArrow
            opt.text = ""
            p = QPainter(btn)
            btn.style().drawComplexControl(
                QStyle.ComplexControl.CC_ToolButton, opt, p, btn)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            color = self._color if btn.isEnabled() else self._disabled
            p.setPen(QPen(color, 1.8, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            r = btn.rect()
            cx, cy = r.center().x() + 0.5, r.center().y() + 0.5
            if self._side == "left":
                pts = [(cx + 2.5, cy - 4.5), (cx - 2.5, cy), (cx + 2.5, cy + 4.5)]
            else:
                pts = [(cx - 2.5, cy - 4.5), (cx + 2.5, cy), (cx - 2.5, cy + 4.5)]
            p.drawPolyline([QPointF(x, y) for x, y in pts])
            p.end()
            return True
        return super().eventFilter(obj, event)


class _TabCloseButton(QToolButton):
    """文書タブの × 。Fusion 標準の close-button アイコンはダークテーマで
    背景に溶けて見えないので、文字の ✕ を theme.py の #tabClose で塗る。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("tabClose")
        self.setText("×")
        self.setAutoRaise(True)
        self.setFixedSize(QSize(16, 16))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("タブを閉じる（中ボタンクリックでも閉じます）")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)


class _TabBarMiddleClick(QObject):
    """タブバーの中ボタンクリックでそのタブを閉じる（ブラウザーと同じ）。"""

    def __init__(self, bar: QTabBar, close_cb) -> None:
        super().__init__(bar)
        self._bar = bar
        self._close = close_cb
        self._pressed = -1
        bar.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt 命名)
        if obj is self._bar:
            if (event.type() == QEvent.Type.MouseButtonPress
                    and event.button() == Qt.MouseButton.MiddleButton):
                self._pressed = self._bar.tabAt(event.position().toPoint())
                return True
            if (event.type() == QEvent.Type.MouseButtonRelease
                    and event.button() == Qt.MouseButton.MiddleButton):
                idx = self._bar.tabAt(event.position().toPoint())
                pressed, self._pressed = self._pressed, -1
                if idx >= 0 and idx == pressed:
                    self._close(idx)
                return True
        return super().eventFilter(obj, event)

# QShortcut や PageView.keyPressEvent が直接さばくキー（QAction ではないので
# findChildren では拾えない）。main_window._setup_shortcuts と揃えておく。
_VIEW_KEYS = [
    ("Ctrl+1", "実際のサイズ（100%）"),
    ("Ctrl+G", "ページ番号を入力して移動"),
    ("Ctrl+A", "すべて選択"),
    ("Ctrl+W", "タブを閉じる"),
    ("Ctrl+Tab / Ctrl+Shift+Tab", "次 / 前のタブ"),
    ("Ctrl+Home / Ctrl+End", "先頭 / 最終ページ"),
    ("← / →", "前 / 次のページ"),
    ("↑ / ↓", "スクロール"),
    ("Space / Shift+Space", "1 画面送り / 戻り"),
    ("Home / End", "先頭 / 最終ページ"),
    ("Delete", "選択中の注釈を削除"),
    ("Esc", "全画面を終了 / テキストの入力を確定"),
    ("Ctrl+Enter", "テキストの入力を確定"),
    ("Ctrl+ホイール", "拡大 / 縮小"),
    ("中ボタン", "オートスクロール開始 / 停止"),
    ("右ドラッグ ↑↓←→", "マウスジェスチャー（設定で割り当て）"),
    ("ダブル / トリプルクリック", "語 / 行を選択"),
]


def _key_names(act: QAction) -> list[str]:
    """action のショートカットを表示名で（同じものは 1 つに）。"""
    out: list[str] = []
    for k in act.shortcuts():
        if k.isEmpty():
            continue
        name = k.toString(QKeySequence.SequenceFormat.NativeText)
        if name not in out:
            out.append(name)
    return out


class ToolsMixin:
    """MainWindow に混ぜて使う。__init__ 内で _build_extra_actions() を呼ぶこと。"""

    NAV_HISTORY_MAX = 100

    # --- action の生成（メニュー / リボン組み立ての前に呼ぶ）--------------
    def _build_extra_actions(self) -> None:
        self.act_nav_back = QAction("前の位置へ戻る", self)
        self.act_nav_back.setShortcut(QKeySequence("Alt+Left"))
        self.act_nav_forward = QAction("次の位置へ進む", self)
        self.act_nav_forward.setShortcut(QKeySequence("Alt+Right"))
        self.act_fullscreen = QAction("全画面表示", self)
        self.act_fullscreen.setCheckable(True)
        self.act_fullscreen.setShortcut(QKeySequence("F11"))
        self.act_shortcuts = QAction("キーボードショートカット一覧…", self)
        self.act_shortcuts.setShortcut(QKeySequence("F1"))
        self.act_flatten = QAction("注釈・入力値を固定して保存…", self)
        self.act_copy_page_image = QAction("ページを画像としてコピー", self)
        self.act_show_in_explorer = QAction("エクスプローラーで表示", self)
        self.act_copy_path = QAction("ファイルのパスをコピー", self)
        self.act_close_others = QAction("他のタブをすべて閉じる", self)
        self.act_close_all = QAction("すべてのタブを閉じる", self)
        for act in (self.act_nav_back, self.act_nav_forward, self.act_fullscreen,
                    self.act_shortcuts):
            self.addAction(act)   # ショートカットをウィンドウ全体で効かせる

        self.act_nav_back.triggered.connect(lambda: self._nav_step(-1))
        self.act_nav_forward.triggered.connect(lambda: self._nav_step(1))
        self.act_fullscreen.toggled.connect(self._set_fullscreen)
        self.act_shortcuts.triggered.connect(self.show_shortcuts)
        self.act_flatten.triggered.connect(self.flatten_save)
        self.act_copy_page_image.triggered.connect(
            lambda: self.page_view.copy_page_image(self.page_view.index))
        self.act_show_in_explorer.triggered.connect(
            lambda: self.show_in_explorer(self.doc.path))
        self.act_copy_path.triggered.connect(
            lambda: self._copy_path(self.doc.path))
        self.act_close_others.triggered.connect(
            lambda: self.close_other_tabs(self.tabs.currentIndex()))
        self.act_close_all.triggered.connect(self.close_all_tabs)

        # Esc は全画面のときだけ拾う（テキスト注釈の入力中の Esc を奪わない）
        self._esc_fullscreen = QShortcut(QKeySequence("Escape"), self)
        self._esc_fullscreen.setEnabled(False)
        self._esc_fullscreen.activated.connect(
            lambda: self.act_fullscreen.setChecked(False))

        self._nav_back: list = []      # [(DocTab, page)]
        self._nav_forward: list = []
        self._nav_suspend = False
        self._update_nav_actions()

        self.tabs.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self._tab_context_menu)
        self._tab_middle_click = _TabBarMiddleClick(self.tabs.tabBar(), self._close_tab)

    def _install_tab_close(self, index: int) -> None:
        """index のタブに自前の × を付ける（open_path から呼ぶ）。"""
        bar = self.tabs.tabBar()
        btn = _TabCloseButton(bar)
        # タブは並べ替えられるので、押された時点でボタンの位置から番号を引く
        btn.clicked.connect(lambda _c=False, b=btn: self._close_tab_of_button(b))
        bar.setTabButton(index, QTabBar.ButtonPosition.RightSide, btn)

    def _close_tab_of_button(self, btn) -> None:
        bar = self.tabs.tabBar()
        for i in range(bar.count()):
            if bar.tabButton(i, QTabBar.ButtonPosition.RightSide) is btn:
                self._close_tab(i)
                return

    # --- 新しいタブへの接続（_new_tab から呼ぶ）----------------------------
    def _connect_tools_signals(self, pv) -> None:
        pv.link_activated.connect(self._on_link_activated)
        pv.link_hovered.connect(self._on_link_hovered)
        pv.snapshot_copied.connect(self._on_snapshot_copied)

    # --- リンク ---------------------------------------------------------
    def _on_link_activated(self, link: dict) -> None:
        kind = link.get("kind")
        if kind == "goto":
            self.goto_with_history(int(link.get("page", 0)))
            return
        if kind == "uri":
            uri = link.get("uri") or ""
            url = QUrl.fromUserInput(uri)
            scheme = url.scheme().lower()
            if scheme in ("http", "https", "mailto"):
                QDesktopServices.openUrl(url)
                self.statusBar().showMessage(f"開きます: {uri}", 3000)
            else:
                # file: や javascript: など、勝手に実行すると危ないものは出さない
                self.statusBar().showMessage(
                    f"このリンクは開けません（{uri[:60]}）", 5000)
            return
        if kind == "named":
            name = (link.get("uri") or "").lower()
            target = {"firstpage": 0, "lastpage": self.doc.page_count - 1,
                      "nextpage": self.page_view.index + 1,
                      "prevpage": self.page_view.index - 1}.get(name)
            if target is not None:
                self.goto_with_history(target)
            else:
                self.statusBar().showMessage(
                    f"このリンク先には対応していません（{name}）", 4000)
            return
        self.statusBar().showMessage("別のファイルへのリンクは開けません", 4000)

    def _on_link_hovered(self, label: str) -> None:
        if label:
            self.statusBar().showMessage(label, 0)
        else:
            self.statusBar().clearMessage()

    # --- 移動履歴（戻る / 進む）------------------------------------------
    def goto_with_history(self, index: int) -> None:
        """今の位置を履歴に積んでからページへ移動する（リンク・目次用）。"""
        tab = self._active_tab()
        if tab is None:
            return
        cur = tab.page_view.index
        if cur == index:
            return
        self._push_history(tab, cur)
        self._nav_forward.clear()
        self._goto(index)
        self._update_nav_actions()

    def _push_history(self, tab, page: int) -> None:
        if self._nav_back and self._nav_back[-1] == (tab, page):
            return
        self._nav_back.append((tab, page))
        del self._nav_back[:-self.NAV_HISTORY_MAX]

    def _nav_step(self, step: int) -> None:
        src, dst = ((self._nav_back, self._nav_forward) if step < 0
                    else (self._nav_forward, self._nav_back))
        while src:
            tab, page = src.pop()
            if self.tabs.indexOf(tab) < 0 or not tab.doc.is_open:
                continue   # 閉じたタブの履歴は飛ばす
            cur_tab = self._active_tab()
            if cur_tab is not None:
                dst.append((cur_tab, cur_tab.page_view.index))
            if tab is not cur_tab:
                self.tabs.setCurrentWidget(tab)
            self._goto(page)
            break
        self._update_nav_actions()

    def _update_nav_actions(self) -> None:
        self.act_nav_back.setEnabled(bool(self._nav_back))
        self.act_nav_forward.setEnabled(bool(self._nav_forward))

    def _forget_tab_history(self, tab) -> None:
        self._nav_back = [h for h in self._nav_back if h[0] is not tab]
        self._nav_forward = [h for h in self._nav_forward if h[0] is not tab]
        self._update_nav_actions()

    # --- 全画面 -----------------------------------------------------------
    def _set_fullscreen(self, on: bool) -> None:
        if on:
            self._fs_saved = {
                "docks": [(d, not d.isHidden()) for d in
                          (self.page_dock, self.toc_dock, self.annot_dock)],
                "ribbon_collapsed": self.ribbon.is_collapsed()
                if hasattr(self, "ribbon") else False,
                "maximized": self.isMaximized(),
            }
            for d, _vis in self._fs_saved["docks"]:
                d.hide()
            if hasattr(self, "ribbon"):
                self.ribbon.hide()
            self.statusBar().hide()
            self.showFullScreen()
            self._esc_fullscreen.setEnabled(True)
            self.page_view.setFocus()
        else:
            saved = getattr(self, "_fs_saved", None) or {}
            self._esc_fullscreen.setEnabled(False)
            if saved.get("maximized"):
                self.showMaximized()
            else:
                self.showNormal()
            self.statusBar().show()
            if hasattr(self, "ribbon"):
                self.ribbon.show()
            for d, vis in saved.get("docks", []):
                d.setVisible(vis)
        self.act_fullscreen.setText("全画面を終了" if on else "全画面表示")

    # --- ショートカット一覧 -------------------------------------------------
    def shortcut_rows(self) -> list[tuple[str, str]]:
        """(キー, 操作名) の一覧。QAction のショートカットを集めて重複を除く。"""
        rows: list[tuple[str, str]] = []
        seen: set[str] = set()
        # リボンの短縮名では意味が取れないものだけ、一覧用の名前を別に持つ
        names = {
            getattr(self, "act_search_next", None): "次の検索結果",
            getattr(self, "act_search_prev", None): "前の検索結果",
            getattr(self, "act_shortcuts", None): "キーボードショートカット一覧",
            getattr(self, "act_find", None): "検索（検索窓を開く）",
        }
        for act in self.findChildren(QAction):
            if act in names:
                keys = _key_names(act)
                if keys and " / ".join(keys) not in seen:
                    seen.add(" / ".join(keys))
                    rows.append((" / ".join(keys), names[act]))
                continue
            keys = _key_names(act)
            # リボン用の短い名前（iconText）を優先。「◀ 前へ」「＋ 拡大」の
            # ような飾りや末尾の「…」を一覧に持ち込まない。
            text = (act.iconText() or act.text()).replace("&", "").strip()
            text = text.lstrip("◀▶＋－⟲⟳ ").rstrip("…").strip()
            if not keys or not text or text.endswith("▾"):
                continue
            key = " / ".join(keys)
            if key in seen:
                continue
            seen.add(key)
            rows.append((key, text))
        rows.sort(key=lambda r: (len(r[0]), r[0]))
        rows.extend(_VIEW_KEYS)
        return rows

    def show_shortcuts(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("キーボードショートカット")
        dlg.resize(520, 560)
        lay = QVBoxLayout(dlg)
        rows = self.shortcut_rows()
        table = QTableWidget(len(rows), 2, dlg)
        table.setHorizontalHeaderLabels(["キー", "操作"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        for r, (key, text) in enumerate(rows):
            table.setItem(r, 0, QTableWidgetItem(key))
            table.setItem(r, 1, QTableWidgetItem(text))
        lay.addWidget(table)
        lay.addWidget(QLabel("マウスジェスチャーの割り当ては 設定 > 全般 から変えられます。"))
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dlg)
        btns.button(QDialogButtonBox.StandardButton.Close).setText("閉じる")
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        lay.addWidget(btns)
        self._shortcut_dialog = dlg
        dlg.exec()

    # --- タブの右クリック ---------------------------------------------------
    def _tab_context_menu(self, pos) -> None:
        from PySide6.QtWidgets import QMenu

        bar = self.tabs.tabBar()
        idx = bar.tabAt(pos)
        if idx < 0:
            return
        tab = self.tabs.widget(idx)
        path = getattr(getattr(tab, "doc", None), "path", None)
        menu = QMenu(self)
        a_explorer = menu.addAction("エクスプローラーで表示")
        a_copy = menu.addAction("ファイルのパスをコピー")
        a_explorer.setEnabled(bool(path))
        a_copy.setEnabled(bool(path))
        menu.addSeparator()
        a_close = menu.addAction("このタブを閉じる")
        a_others = menu.addAction("他のタブをすべて閉じる")
        a_others.setEnabled(self.tabs.count() > 1)
        a_all = menu.addAction("すべてのタブを閉じる")
        chosen = menu.exec(bar.mapToGlobal(pos))
        if chosen is a_explorer:
            self.show_in_explorer(path)
        elif chosen is a_copy:
            self._copy_path(path)
        elif chosen is a_close:
            self._close_tab(idx)
        elif chosen is a_others:
            self.close_other_tabs(idx)
        elif chosen is a_all:
            self.close_all_tabs()

    def show_in_explorer(self, path: str | None) -> None:
        if not path or not os.path.exists(path):
            self.statusBar().showMessage("ファイルが見つかりません", 3000)
            return
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])  # noqa: S607
        except OSError as exc:
            self.statusBar().showMessage(f"エクスプローラーを開けませんでした: {exc}", 5000)

    def _copy_path(self, path: str | None) -> None:
        if not path:
            return
        QApplication.clipboard().setText(os.path.normpath(path))
        self.statusBar().showMessage("パスをコピーしました", 2000)

    def close_other_tabs(self, keep: int) -> None:
        """keep 以外のタブを閉じる。未保存確認でキャンセルされたらそこで止める。"""
        tab = self.tabs.widget(keep)
        i = 0
        while i < self.tabs.count():
            if self.tabs.widget(i) is tab:
                i += 1
                continue
            before = self.tabs.count()
            self._close_tab(i)
            if self.tabs.count() == before:   # キャンセルされた
                i += 1

    def close_all_tabs(self) -> None:
        while self.tabs.count() > 0:
            before = self.tabs.count()
            self._close_tab(0)
            if self.tabs.count() == before:
                return

    # --- 固定して保存（フラット化）------------------------------------------
    def flatten_save(self) -> None:
        if not self.doc.is_open:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("注釈・入力値を固定して保存")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            "選んだものをページの絵の一部にして、別名で保存します。\n"
            "相手に渡しても動かされたり書き換えられたりしません（元に戻せない\n"
            "ので、今開いている文書はそのまま残し、別ファイルに書き出します）。"))
        cb_annots = QCheckBox("注釈（ハイライト・図形・テキスト・スタンプなど）")
        cb_annots.setChecked(True)
        cb_widgets = QCheckBox("フォームの入力値（入力欄を文字に固定する）")
        cb_widgets.setChecked(self.doc.is_form())
        cb_widgets.setEnabled(self.doc.is_form())
        lay.addWidget(cb_annots)
        lay.addWidget(cb_widgets)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                | QDialogButtonBox.StandardButton.Cancel, dlg)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("保存先を選ぶ…")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        self._flatten_dialog = dlg
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if not (cb_annots.isChecked() or cb_widgets.isChecked()):
            return
        base = os.path.splitext(self.doc.path or "document")[0]
        out, _ = QFileDialog.getSaveFileName(
            self, "固定して保存", base + "_固定.pdf", "PDF ファイル (*.pdf)")
        if not out:
            return
        try:
            self.doc.flatten_to(out, annots=cb_annots.isChecked(),
                                widgets=cb_widgets.isChecked())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"固定して保存できませんでした:\n{exc}")
            return
        self.statusBar().showMessage(f"固定して保存しました: {out}", 5000)
        ret = QMessageBox.question(
            self, "完了",
            f"固定して保存しました:\n{out}\n\n保存したファイルを開きますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.Yes:
            self.open_path(out)

    # --- スナップショット ----------------------------------------------------
    def _on_snapshot_copied(self, w: int, h: int) -> None:
        self.statusBar().showMessage(
            f"画像としてコピーしました（{w}×{h} px）。Word やメールに貼り付けられます", 4000)
        # 1 回撮ったら選択ツールへ戻す（次のクリックで意図せず撮らないように）
        if getattr(self, "act_tool_none", None) is not None and not self.act_tool_none.isChecked():
            self.act_tool_none.trigger()
