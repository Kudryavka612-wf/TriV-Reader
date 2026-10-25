"""サムネイル一覧（左ドック）ウィジェット。

クリックでページ移動、ドラッグで並べ替え、Delete/右クリックで削除。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
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
        self._rendered: set[int] = set()   # 実画像が入っている行
        self._placeholder: QIcon | None = None
        # スクロール中に毎回キューを組み直さない（止まってから 1 回）
        self._scan_timer = QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.setInterval(60)
        self._scan_timer.timeout.connect(self._queue_visible)
        self.verticalScrollBar().valueChanged.connect(
            lambda _v: self._scan_timer.start())

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
    CHUNK = 4        # 1 回の描画で処理するページ数
    MARGIN = 6       # 画面の上下、この行数ぶんまで先に描いておく
    MAX_RENDERED = 120  # 実画像を持たせておく上限（超えたら遠い行から捨てる）

    def load(self, doc: PdfDocument) -> None:
        self._doc = doc
        self._gen += 1           # 進行中の旧ロードを無効化
        self._pending = []
        self._rendered = set()
        self.clear()
        if not doc.is_open:
            return
        # プレースホルダ（薄いグレーの枠）を即時に並べる
        ph = QPixmap(self.THUMB_WIDTH, int(self.THUMB_WIDTH * 1.4))
        ph.fill(Qt.GlobalColor.transparent)
        self._placeholder = QIcon(ph)
        for i in range(doc.page_count):
            item = QListWidgetItem()
            item.setIcon(self._placeholder)
            item.setText(f"{i + 1}")
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setData(ORIG_INDEX_ROLE, i)  # 並べ替え検知用に元indexを保持
            self.addItem(item)
        if doc.page_count:
            self.setCurrentRow(0)
        # 実画像は次のイベントループ以降に、見えているぶんから描く
        QTimer.singleShot(0, self._queue_visible)

    # 全ページを一気に描くと、300 ページのスキャン PDF では開いた直後に
    # 数十秒ぶん CPU を使い、サムネイルの画像だけで数十 MB 抱えてしまう。
    # 見えている範囲（＋前後 MARGIN 行）だけを描き、離れた行の画像は捨てる。
    def _visible_rows(self) -> tuple[int, int]:
        if not self.count():
            return (0, -1)
        vp = self.viewport().rect()
        # 左端は spacing の隙間で「項目なし」になるので、横は真ん中で測る
        x = max(1, vp.width() // 2)
        pad = self.spacing() + 2
        top = self.indexAt(QPoint(x, pad)).row()
        bottom = self.indexAt(QPoint(x, max(pad, vp.height() - pad))).row()
        if top < 0:
            top = max(0, self.currentRow())
        if bottom < 0:
            # まだレイアウトされていない/最終行より下を指した場合。行の高さから
            # 見えている数を見積もる（ここで count()-1 にすると、全ページ描画に
            # 逆戻りしてしまう）。
            row_h = max(1, self.sizeHintForRow(0) + 2 * self.spacing())
            bottom = top + max(1, vp.height() // row_h)
        return (max(0, top - self.MARGIN),
                min(self.count() - 1, bottom + self.MARGIN))

    def _queue_visible(self) -> None:
        """見えている範囲のうち、まだ画像が無い行を描画キューに積む。"""
        if self._doc is None or not self._doc.is_open or not self.count():
            return
        lo, hi = self._visible_rows()
        self._pending = [i for i in range(lo, hi + 1) if i not in self._rendered]
        if self._pending:
            QTimer.singleShot(0, lambda g=self._gen: self._render_chunk(g))

    def _render_chunk(self, gen: int) -> None:
        if gen != self._gen or self._doc is None or not self._doc.is_open:
            return  # 別ファイルに切り替わった等 → このロードは破棄
        done = 0
        while self._pending and done < self.CHUNK:
            i = self._pending.pop(0)
            if 0 <= i < self.count():
                try:
                    pixmap = self._doc.render_thumbnail(
                        i, max_width=self.THUMB_WIDTH, dpr=self.devicePixelRatioF())
                    self.item(i).setIcon(pixmap)
                    self._rendered.add(i)
                except Exception:  # noqa: BLE001 (描画失敗は枠のまま継続)
                    pass
            done += 1
        if self._pending:
            QTimer.singleShot(0, lambda g=gen: self._render_chunk(g))
        else:
            self._trim_rendered()

    def _trim_rendered(self) -> None:
        """遠くの行の画像を捨てて、抱えるメモリを一定に保つ。"""
        if len(self._rendered) <= self.MAX_RENDERED or self._placeholder is None:
            return
        lo, hi = self._visible_rows()
        center = (lo + hi) // 2
        for i in sorted(self._rendered, key=lambda r: -abs(r - center)):
            if len(self._rendered) <= self.MAX_RENDERED:
                break
            if lo <= i <= hi:
                continue  # 見えている行は残す
            item = self.item(i)
            if item is not None:
                item.setIcon(self._placeholder)
            self._rendered.discard(i)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().showEvent(event)
        self._scan_timer.start()   # 隠れていた間に動いた分を描く

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        self._scan_timer.start()

    def refresh_page(self, doc: PdfDocument, index: int) -> None:
        """1 ページ分のサムネイルを再描画（回転後の反映などに使う）。"""
        if 0 <= index < self.count() and doc.is_open:
            pixmap = doc.render_thumbnail(
                index, max_width=self.THUMB_WIDTH, dpr=self.devicePixelRatioF())
            self.item(index).setIcon(pixmap)
            self._rendered.add(index)

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
