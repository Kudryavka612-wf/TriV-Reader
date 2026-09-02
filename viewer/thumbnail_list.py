"""サムネイル一覧（左ドック）ウィジェット。

クリックでページ移動、ドラッグで並べ替え、Delete/右クリックで削除。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem, QMenu

from .document import PdfDocument

ORIG_INDEX_ROLE = Qt.ItemDataRole.UserRole


class ThumbnailList(QListWidget):
    """各ページのサムネイルを縦に並べる。"""

    page_selected = Signal(int)          # クリックでページ移動
    pages_reordered = Signal(list)       # ドラッグ並べ替え後の新しい順序(旧index配列)
    delete_requested = Signal(int)       # ページ削除要求

    THUMB_WIDTH = 150

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setIconSize(QSize(self.THUMB_WIDTH, int(self.THUMB_WIDTH * 1.6)))
        self.setSpacing(6)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        # ドラッグ＆ドロップでの並べ替えを有効化
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

        self.currentRowChanged.connect(self._on_row_changed)
        self.customContextMenuRequested.connect(self._on_context_menu)

        self._doc: PdfDocument | None = None
        self._gen = 0            # ロード世代（古いロードのキャンセル用）
        self._pending: list[int] = []  # これから実描画する index のキュー

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        """空のときは白い箱にせず、何を待っている場所かを書いておく。"""
        super().paintEvent(event)
        if self.count():
            return
        p = QPainter(self.viewport())
        p.setPen(self.palette().color(QPalette.ColorRole.Mid))
        p.drawText(self.viewport().rect(),
                   Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                   "PDF を開くとページの一覧がここに並びます")

    # --- 遅延サムネイル描画 ---------------------------------------------
    # 全ページのサムネイルを一気に同期描画すると、PDF を開いた瞬間に
    # その完了までページ本体の表示がブロックされてしまう。そこでまず
    # 番号付きの空プレースホルダを即座に並べ、実画像は分割して非同期に
    # 埋めていく（ブラウザのサムネイルパネルと同様の挙動）。
    CHUNK = 4  # 1 回の描画で処理するページ数

    def load(self, doc: PdfDocument) -> None:
        self._doc = doc
        self._gen += 1           # 進行中の旧ロードを無効化
        self._pending = []
        self.clear()
        if not doc.is_open:
            return
        # プレースホルダ（薄いグレーの枠）を即時に並べる
        ph = QPixmap(self.THUMB_WIDTH, int(self.THUMB_WIDTH * 1.4))
        ph.fill(Qt.GlobalColor.transparent)
        placeholder = QIcon(ph)
        for i in range(doc.page_count):
            item = QListWidgetItem()
            item.setIcon(placeholder)
            item.setText(f"{i + 1}")
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setData(ORIG_INDEX_ROLE, i)  # 並べ替え検知用に元indexを保持
            self.addItem(item)
        if doc.page_count:
            self.setCurrentRow(0)
        # 実画像は次のイベントループ以降に少しずつ描画
        self._pending = list(range(doc.page_count))
        QTimer.singleShot(0, lambda g=self._gen: self._render_chunk(g))

    def _render_chunk(self, gen: int) -> None:
        if gen != self._gen or self._doc is None or not self._doc.is_open:
            return  # 別ファイルに切り替わった等 → このロードは破棄
        done = 0
        while self._pending and done < self.CHUNK:
            i = self._pending.pop(0)
            if 0 <= i < self.count():
                try:
                    pixmap = self._doc.render_thumbnail(i, max_width=self.THUMB_WIDTH)
                    self.item(i).setIcon(pixmap)
                except Exception:  # noqa: BLE001 (描画失敗は枠のまま継続)
                    pass
            done += 1
        if self._pending:
            QTimer.singleShot(0, lambda g=gen: self._render_chunk(g))

    def refresh_page(self, doc: PdfDocument, index: int) -> None:
        """1 ページ分のサムネイルを再描画（回転後の反映などに使う）。"""
        if 0 <= index < self.count() and doc.is_open:
            pixmap = doc.render_thumbnail(index, max_width=self.THUMB_WIDTH)
            self.item(index).setIcon(pixmap)

    def select_page(self, index: int) -> None:
        """外部からの選択（ページ送り等）を反映。シグナルは出さない。"""
        if 0 <= index < self.count() and index != self.currentRow():
            self.blockSignals(True)
            self.setCurrentRow(index)
            self.blockSignals(False)

    # --- 並べ替え -------------------------------------------------------
    def dropEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().dropEvent(event)
        # ドロップ後の並びから新しい順序（旧index配列）を組み立てて通知
        new_order = [
            self.item(row).data(ORIG_INDEX_ROLE) for row in range(self.count())
        ]
        if new_order != sorted(range(self.count())) or any(
            new_order[i] != i for i in range(len(new_order))
        ):
            self.pages_reordered.emit(new_order)

    # --- 削除メニュー ---------------------------------------------------
    def _on_context_menu(self, pos) -> None:
        item = self.itemAt(pos)
        if item is None:
            return
        row = self.row(item)
        menu = QMenu(self)
        act_delete = menu.addAction("このページを削除")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen == act_delete:
            self.delete_requested.emit(row)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.key() == Qt.Key.Key_Delete and self.currentRow() >= 0:
            self.delete_requested.emit(self.currentRow())
            return
        super().keyPressEvent(event)

    def _on_row_changed(self, row: int) -> None:
        if row >= 0:
            self.page_selected.emit(row)
