"""ページ整理用の大画面（グリッド表示）。

サムネイルを大きなグリッドで並べ、複数選択して削除・抽出・回転、
ドラッグで並べ替えができる。ビューワーとは画面を切り替えて使う。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .document import PdfDocument

ORIG_INDEX_ROLE = Qt.ItemDataRole.UserRole


class _PreviewPane(QWidget):
    """選択中ページの拡大プレビュー。ペインの大きさに合わせて等比で描画する。

    抽出・削除などの操作前に「どのページか」を確認しながら作業できるように、
    グリッドの右側に常駐させる（スプリッターで幅の調整・折りたたみが可能）。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._caption = ""
        self._dark = False
        self.setMinimumWidth(120)

    def set_page(self, pixmap: QPixmap, caption: str) -> None:
        self._pixmap = pixmap
        self._caption = caption
        self.update()

    def clear_page(self, message: str = "ページを選択するとここに表示されます") -> None:
        self._pixmap = None
        self._caption = message
        self.update()

    def apply_theme(self, dark: bool) -> None:
        self._dark = dark
        self.update()

    CAPTION_H = 24  # 上部のページ番号表示の高さ(px)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        p = QPainter(self)
        r = self.rect()
        p.fillRect(r, QColor("#242426") if self._dark else QColor("#fafafa"))
        p.setPen(QColor("#ddd") if self._dark else QColor("#333"))
        p.drawText(0, 0, r.width(), self.CAPTION_H,
                   Qt.AlignmentFlag.AlignCenter, self._caption)
        if self._pixmap is None or self._pixmap.isNull():
            p.end()
            return
        avail_w = max(1, r.width() - 12)
        avail_h = max(1, r.height() - self.CAPTION_H - 8)
        pm = self._pixmap
        # 高 DPI の pixmap は、width() がデバイス px（論理 px の dpr 倍）。論理サイズで測る
        lw = max(1.0, pm.deviceIndependentSize().width())
        lh = max(1.0, pm.deviceIndependentSize().height())
        scale = min(avail_w / lw, avail_h / lh, 1.0)
        w = max(1, int(lw * scale))
        h = max(1, int(lh * scale))
        x = (r.width() - w) // 2
        y = self.CAPTION_H + (avail_h - h) // 2
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(x, y, w, h, pm)
        p.setPen(QColor("#666") if self._dark else QColor("#b0b0b0"))
        p.drawRect(x, y, w - 1, h - 1)
        p.end()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        self.update()


class _Grid(QListWidget):
    """サムネイルのアイコングリッド。複数選択とボタンによる並べ替えに対応。

    ネイティブのドラッグ&ドロップは環境により動作しないため採用せず、
    「左へ/右へ」「先頭へ/末尾へ」ボタンで確実に並べ替える方式にしている。
    """

    reordered = Signal(list, list)  # (新しい順序, 移動後に選択すべき位置)
    """注: PageOrganizer 側にも同名・同型のシグナルがあり、そこへ転送される。"""

    def __init__(self, thumb: int, parent=None) -> None:
        super().__init__(parent)
        self._thumb = thumb
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setFlow(QListWidget.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(12)
        self.setIconSize(QSize(thumb, int(thumb * 1.4)))
        self.setGridSize(QSize(thumb + 30, int(thumb * 1.4) + 40))
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.apply_theme(False)

    def apply_theme(self, dark: bool) -> None:
        """選択ページ強調のスタイルをテーマに合わせる。"""
        if dark:
            self.setStyleSheet(
                """
                QListWidget { background: #242426; }
                QListWidget::item { border: 2px solid transparent; border-radius: 8px;
                    padding: 4px; color: #ddd; }
                QListWidget::item:selected { background: #234; border: 3px solid #3a8fe0;
                    color: #cfe6ff; }
                QListWidget::item:hover { background: #303338; }
                """
            )
        else:
            self.setStyleSheet(
                """
                QListWidget { background: #fafafa; }
                QListWidget::item { border: 2px solid transparent; border-radius: 8px;
                    padding: 4px; color: #333; }
                QListWidget::item:selected { background: #d6e4ff; border: 3px solid #1a73e8;
                    color: #0b3d91; }
                QListWidget::item:hover { background: #eef3ff; }
                """
            )

    def move_selected(self, step: int) -> None:
        """選択ページを step（-1=左 / +1=右）だけ移動する。"""
        rows = sorted(self.row(it) for it in self.selectedItems())
        if not rows:
            return
        if step < 0:
            if rows[0] == 0:
                return  # 先頭より前へは動かせない
            for r in rows:  # 左から処理
                it = self.takeItem(r)
                self.insertItem(r - 1, it)
                it.setSelected(True)
        else:
            if rows[-1] == self.count() - 1:
                return  # 末尾より後ろへは動かせない
            for r in reversed(rows):  # 右から処理
                it = self.takeItem(r)
                self.insertItem(r + 1, it)
                it.setSelected(True)
        self._emit_order()

    def move_to_edge(self, to_front: bool) -> None:
        """選択ページをまとめて先頭(または末尾)へ移動する。"""
        rows = sorted(self.row(it) for it in self.selectedItems())
        if not rows:
            return
        moved = [self.takeItem(r) for r in reversed(rows)]
        moved.reverse()
        base = 0 if to_front else self.count()
        self.clearSelection()
        for offset, it in enumerate(moved):
            self.insertItem(base + offset, it)
            it.setSelected(True)
        self._emit_order()

    def _emit_order(self) -> None:
        new_order = [
            self.item(r).data(ORIG_INDEX_ROLE) for r in range(self.count())
        ]
        # 移動後に選択し直すべき位置（再読み込みで選択が消えても復元するため）
        new_selected = sorted(self.row(it) for it in self.selectedItems())
        self.reordered.emit(new_order, new_selected)


class InsertDialog(QDialog):
    """挿入の内容（PDF/白紙）と位置（先頭/末尾/選択の前/後ろ）を選ぶダイアログ。"""

    POS_FRONT, POS_BACK, POS_BEFORE, POS_AFTER = range(4)

    def __init__(self, has_selection: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ページを挿入")
        form = QFormLayout(self)
        self.kind = QComboBox()
        self.kind.addItems(["PDF ファイルから…", "白紙ページ"])
        form.addRow("挿入する内容:", self.kind)
        self.pos = QComboBox()
        self.pos.addItems(["先頭", "末尾", "選択ページの前", "選択ページの後ろ"])
        if not has_selection:
            # 選択が無いときは前/後は選べない
            for i in (self.POS_BEFORE, self.POS_AFTER):
                self.pos.model().item(i).setEnabled(False)
        self.pos.setCurrentIndex(self.POS_BACK)
        form.addRow("挿入する場所:", self.pos)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


class PageOrganizer(QWidget):
    """ページ整理画面。各操作は選択ページに対して行う。"""

    reordered = Signal(list, list)    # (新しい順序, 移動後に選択すべき位置)
    delete_requested = Signal(list)   # 選択ページの削除
    extract_requested = Signal(list)  # 選択ページの抽出
    export_images_requested = Signal(list)  # 選択ページを画像に書き出し
    rotate_requested = Signal(list, int)  # 選択ページの回転(度)
    split_requested = Signal()        # 分割（リボン用に残置）
    page_activated = Signal(int)      # ダブルクリックでビューワーへ
    insert_requested = Signal(str, int)   # ("file"|"blank", 挿入位置)
    properties_requested = Signal()   # プロパティ（メタデータ）
    security_requested = Signal()     # セキュリティ（パスワード保護）

    THUMB = 190

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        # 操作ボタン（12 個）の列。そのまま置くと、この画面が（隠れていても）窓の
        # 最小幅を 1100px 超に押し上げ、PDF を開いただけで窓を狭められなくなる
        # （ノート PC の画面や、半分に並べる配置に収まらない）。狭いときは横に
        # スクロールさせる。
        bar_host = QWidget()
        bar = QHBoxLayout(bar_host)
        bar.setContentsMargins(0, 0, 0, 0)
        self._sel_label = QLabel("未選択")
        self._sel_label.setStyleSheet("font-weight:bold; color:#1a73e8;")
        bar.addWidget(self._sel_label)
        bar.addStretch(1)
        # メニュー構成: 挿入、抽出、削除、画像、先頭へ、左へ、右へ、末尾へ、
        #               左回転、右回転、プロパティ、セキュリティ
        self._btn_insert = QPushButton("挿入")
        self._btn_extract = QPushButton("抽出")
        self._btn_delete = QPushButton("削除")
        self._btn_images = QPushButton("画像")
        self._btn_front = QPushButton("⏮ 先頭へ")
        self._btn_move_l = QPushButton("◀ 左へ")
        self._btn_move_r = QPushButton("右へ ▶")
        self._btn_back = QPushButton("末尾へ ⏭")
        self._btn_rotate_l = QPushButton("⟲ 左回転")
        self._btn_rotate_r = QPushButton("⟳ 右回転")
        self._btn_props = QPushButton("プロパティ")
        self._btn_security = QPushButton("セキュリティ")
        for b in (
            self._btn_insert,
            self._btn_extract,
            self._btn_delete,
            self._btn_images,
            self._btn_front,
            self._btn_move_l,
            self._btn_move_r,
            self._btn_back,
            self._btn_rotate_l,
            self._btn_rotate_r,
            self._btn_props,
            self._btn_security,
        ):
            bar.addWidget(b)
        bar_scroll = self._bar_scroll = QScrollArea()
        bar_scroll.setWidget(bar_host)
        bar_scroll.setWidgetResizable(True)
        bar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        bar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        bar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        bar_scroll.setAutoFillBackground(False)
        bar_scroll.viewport().setAutoFillBackground(False)
        bar_host.setAutoFillBackground(False)
        # スクロールバーが出ても、ボタンが欠けない高さにする
        bar_scroll.setFixedHeight(bar_host.sizeHint().height()
                                  + bar_scroll.horizontalScrollBar().sizeHint().height() + 2)
        layout.addWidget(bar_scroll)
        hint = QLabel(
            "ページを選択して操作します（右クリックでもメニューが出ます）。"
            "Ctrl/Shift で複数選択できます。"
        )
        hint.setStyleSheet("color:#777;")
        hint.setWordWrap(True)   # 折り返さないと、この 1 行ぶんの幅が窓の最小幅になる
        layout.addWidget(hint)

        self.grid = _Grid(self.THUMB)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._on_context_menu)

        # グリッドの右に選択ページの拡大プレビュー（スプリッターで幅調整可）
        self.preview = _PreviewPane()
        self.preview.clear_page()
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(self.grid)
        self._splitter.addWidget(self.preview)
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([720, 300])
        # ペイン幅を変えたら、その幅に合った解像度で描画し直す（ぼやけ防止）
        self._splitter.splitterMoved.connect(
            lambda *_: self._update_preview(self.selected_indices()))
        layout.addWidget(self._splitter, 1)  # 余白はすべてグリッド/プレビューへ
        self._doc: PdfDocument | None = None

        # 配線
        self._btn_insert.clicked.connect(self._insert_dialog)
        self._btn_extract.clicked.connect(
            lambda: self._emit_selected(self.extract_requested)
        )
        self._btn_delete.clicked.connect(
            lambda: self._emit_selected(self.delete_requested)
        )
        self._btn_images.clicked.connect(
            lambda: self._emit_selected(self.export_images_requested)
        )
        self._btn_front.clicked.connect(lambda: self.grid.move_to_edge(True))
        self._btn_back.clicked.connect(lambda: self.grid.move_to_edge(False))
        self._btn_move_l.clicked.connect(lambda: self.grid.move_selected(-1))
        self._btn_move_r.clicked.connect(lambda: self.grid.move_selected(1))
        self._btn_rotate_l.clicked.connect(lambda: self._emit_rotate(-90))
        self._btn_rotate_r.clicked.connect(lambda: self._emit_rotate(90))
        self._btn_props.clicked.connect(self.properties_requested.emit)
        self._btn_security.clicked.connect(self.security_requested.emit)
        self.grid.reordered.connect(self.reordered.emit)
        self.grid.itemDoubleClicked.connect(self._on_double_click)
        self.grid.itemSelectionChanged.connect(self._update_selection_label)
        # カレント行だけが動いた場合（Ctrl+クリック等）もプレビューを追従させる
        self.grid.currentItemChanged.connect(
            lambda *_: self._update_preview(self.selected_indices()))

    # --- 挿入 / 右クリックメニュー --------------------------------------
    def _insert_position(self, pos_index: int) -> int:
        """InsertDialog の位置選択を実際の挿入インデックスへ変換する。"""
        sel = self.selected_indices()
        if pos_index == InsertDialog.POS_FRONT:
            return 0
        if pos_index == InsertDialog.POS_BEFORE and sel:
            return sel[0]
        if pos_index == InsertDialog.POS_AFTER and sel:
            return sel[-1] + 1
        return self.grid.count()  # 末尾

    def _insert_dialog(self) -> None:
        dlg = InsertDialog(bool(self.selected_indices()), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        kind = "file" if dlg.kind.currentIndex() == 0 else "blank"
        at = self._insert_position(dlg.pos.currentIndex())
        self.insert_requested.emit(kind, at)

    def _on_context_menu(self, pos) -> None:
        item = self.grid.itemAt(pos)
        if item is not None and not item.isSelected():
            self.grid.clearSelection()
            item.setSelected(True)
        has_sel = bool(self.selected_indices())
        menu = QMenu(self)
        act_insert = menu.addAction("挿入…")
        menu.addSeparator()
        act_extract = menu.addAction("抽出")
        act_delete = menu.addAction("削除")
        act_images = menu.addAction("画像に書き出し")
        menu.addSeparator()
        act_front = menu.addAction("先頭へ")
        act_left = menu.addAction("左へ")
        act_right = menu.addAction("右へ")
        act_back = menu.addAction("末尾へ")
        menu.addSeparator()
        act_rot_l = menu.addAction("左回転")
        act_rot_r = menu.addAction("右回転")
        menu.addSeparator()
        act_props = menu.addAction("プロパティ")
        act_sec = menu.addAction("セキュリティ")
        for a in (act_extract, act_delete, act_images, act_front, act_left,
                  act_right, act_back, act_rot_l, act_rot_r):
            a.setEnabled(has_sel)
        chosen = menu.exec(self.grid.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_insert:
            self._insert_dialog()
        elif chosen is act_extract:
            self._emit_selected(self.extract_requested)
        elif chosen is act_delete:
            self._emit_selected(self.delete_requested)
        elif chosen is act_images:
            self._emit_selected(self.export_images_requested)
        elif chosen is act_front:
            self.grid.move_to_edge(True)
        elif chosen is act_left:
            self.grid.move_selected(-1)
        elif chosen is act_right:
            self.grid.move_selected(1)
        elif chosen is act_back:
            self.grid.move_to_edge(False)
        elif chosen is act_rot_l:
            self._emit_rotate(-90)
        elif chosen is act_rot_r:
            self._emit_rotate(90)
        elif chosen is act_props:
            self.properties_requested.emit()
        elif chosen is act_sec:
            self.security_requested.emit()

    # --- 読み込み -------------------------------------------------------
    def load(self, doc: PdfDocument) -> None:
        self._doc = doc
        self.grid.clear()
        self.preview.clear_page()
        if not doc.is_open:
            return
        for i in range(doc.page_count):
            pixmap = doc.render_thumbnail(
                i, max_width=self.THUMB, dpr=self.devicePixelRatioF())
            item = QListWidgetItem(f"ページ {i + 1}")
            item.setIcon(pixmap)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setData(ORIG_INDEX_ROLE, i)
            self.grid.addItem(item)

    def apply_theme(self, dark: bool) -> None:
        self.grid.apply_theme(dark)
        self.preview.apply_theme(dark)

    def selected_indices(self) -> list[int]:
        return sorted(self.grid.row(it) for it in self.grid.selectedItems())

    def select_positions(self, positions: list[int]) -> None:
        """指定位置のページを選択する（並べ替え後の選択復元に使う）。"""
        self.grid.clearSelection()
        last = None
        for p in positions:
            if 0 <= p < self.grid.count():
                self.grid.item(p).setSelected(True)
                last = p
        if last is not None:
            self.grid.setCurrentRow(last)
            self.grid.scrollToItem(self.grid.item(last))
        self._update_selection_label()

    def _update_selection_label(self) -> None:
        sel = self.selected_indices()
        if not sel:
            self._sel_label.setText("未選択")
        else:
            pages = ", ".join(str(i + 1) for i in sel)
            self._sel_label.setText(f"{len(sel)} ページ選択中（{pages}）")
        self._update_preview(sel)

    def _update_preview(self, sel: list[int]) -> None:
        """選択中のページ（複数選択時はカレント行）をプレビューに描画する。"""
        if not sel or self._doc is None or not self._doc.is_open:
            self.preview.clear_page()
            return
        # Ctrl/Shift 選択の途中でも「最後に操作した行」を優先して見せる
        row = self.grid.currentRow()
        if row not in sel:
            row = sel[-1]
        item = self.grid.item(row)
        if item is None:
            self.preview.clear_page()
            return
        # 並べ替え直後でも正しいページを描画できるよう元インデックスで引く
        orig = item.data(ORIG_INDEX_ROLE)
        try:
            w_pt, h_pt = self._doc.page_pixel_size(orig, 1.0)
            avail_w = max(120, self.preview.width() - 12)
            avail_h = max(120, self.preview.height() - 32)
            zoom = min(avail_w / max(1, w_pt), avail_h / max(1, h_pt), 3.0)
            pixmap = self._doc.render_page(orig, zoom=zoom, dpr=self.devicePixelRatioF())
        except Exception:  # noqa: BLE001
            self.preview.clear_page("プレビューを表示できません")
            return
        n = len(sel)
        cap = f"ページ {row + 1} / {self.grid.count()}"
        if n > 1:
            cap += f"（{n} ページ選択中）"
        self.preview.set_page(pixmap, cap)

    # --- 内部 -----------------------------------------------------------
    def _emit_selected(self, signal: Signal) -> None:
        sel = self.selected_indices()
        if sel:
            signal.emit(sel)

    def _emit_rotate(self, delta: int) -> None:
        sel = self.selected_indices()
        if sel:
            self.rotate_requested.emit(sel, delta)

    def _on_double_click(self, item: QListWidgetItem) -> None:
        self.page_activated.emit(self.grid.row(item))
