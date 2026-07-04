"""注釈一覧ドック。文書内の注釈をページ順に並べ、クリックで選択・ジャンプ、
Delete で削除できるようにする。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)


class AnnotListDock(QDockWidget):
    """注釈の一覧パネル。"""

    annot_selected = Signal(int, int)          # (page_index, xref)
    annot_delete_requested = Signal(int, int)  # (page_index, xref)

    def __init__(self, parent=None) -> None:
        super().__init__("注釈一覧", parent)
        self.setObjectName("annot_list_dock")
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(2, 2, 2, 2)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setAlternatingRowColors(True)
        self.list.itemClicked.connect(self._on_clicked)
        self.list.itemActivated.connect(self._on_clicked)
        lay.addWidget(self.list)
        self.setWidget(body)

    @staticmethod
    def _swatch(rgb) -> QIcon:
        """注釈色の小さな色見本アイコン。"""
        pm = QPixmap(12, 12)
        if rgb:
            pm.fill(QColor(int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255)))
        else:
            pm.fill(QColor(180, 180, 180))
        return QIcon(pm)

    def load(self, items: list) -> None:
        """items: document.list_annots() の戻り値。現在の選択位置は保たない。"""
        self.list.clear()
        for it in items:
            text = f"P{it['page'] + 1}  [{it['label']}] {it['content']}".rstrip()
            row = QListWidgetItem(self._swatch(it.get("color")), text)
            row.setData(Qt.ItemDataRole.UserRole, (it["page"], it["xref"]))
            self.list.addItem(row)
        n = self.list.count()
        self.setWindowTitle(f"注釈一覧 ({n})" if n else "注釈一覧")

    def select_xref(self, page: int, xref: int) -> None:
        """指定注釈の行を選択状態にする（ビュー側の選択に追従表示）。"""
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == (page, xref):
                self.list.setCurrentRow(i)
                return
        self.list.clearSelection()

    def _on_clicked(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            self.annot_selected.emit(data[0], data[1])

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            item = self.list.currentItem()
            if item is not None:
                data = item.data(Qt.ItemDataRole.UserRole)
                if data:
                    self.annot_delete_requested.emit(data[0], data[1])
                    return
        super().keyPressEvent(event)
