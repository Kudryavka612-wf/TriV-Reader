"""しおり/目次（アウトライン）のナビゲーション用ドックと編集ダイアログ。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

PAGE_ROLE = Qt.ItemDataRole.UserRole


class TocDock(QDockWidget):
    """目次をツリー表示し、クリックでそのページへ移動する。"""

    page_requested = Signal(int)  # 0 始まりページ

    def __init__(self, parent=None) -> None:
        super().__init__("しおり", parent)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemClicked.connect(self._on_clicked)
        # キーボード（↑↓ で選んで Enter）でも飛べるように
        self.tree.itemActivated.connect(self._on_clicked)
        self.setWidget(self.tree)

    def load(self, toc: list) -> None:
        """toc: [[level, title, page(1始まり)], ...]"""
        self.tree.clear()
        # レベルに応じた親子関係を stack で構築
        last_at_level: dict[int, QTreeWidgetItem] = {}
        for level, title, page in toc:
            item = QTreeWidgetItem([title])
            item.setData(0, PAGE_ROLE, page)
            parent = last_at_level.get(level - 1)
            if parent is not None:
                parent.addChild(item)
            else:
                self.tree.addTopLevelItem(item)
            last_at_level[level] = item
            # より深い階層の記録は無効化
            for lv in list(last_at_level):
                if lv > level:
                    del last_at_level[lv]
        self.tree.expandAll()

    def _on_clicked(self, item: QTreeWidgetItem) -> None:
        page = item.data(0, PAGE_ROLE)
        # 行き先の無いしおり（ページ番号 -1）は何もしない。飛ぶと先頭ページへ行ってしまう
        if page and int(page) > 0:
            self.page_requested.emit(int(page) - 1)


class TocEditDialog(QDialog):
    """目次を表で編集するダイアログ。"""

    def __init__(self, toc: list, page_count: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("目次を編集")
        self.resize(480, 420)
        self._page_count = page_count

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("レベル(1〜)・タイトル・ページ番号を編集できます。"))

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["レベル", "タイトル", "ページ"])
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.table)
        for level, title, page in toc:
            self._add_row(level, title, page)

        btns = QHBoxLayout()
        b_add = QPushButton("追加")
        b_del = QPushButton("削除")
        b_up = QPushButton("上へ")
        b_down = QPushButton("下へ")
        for b in (b_add, b_del, b_up, b_down):
            btns.addWidget(b)
        btns.addStretch(1)
        layout.addLayout(btns)
        b_add.clicked.connect(lambda: self._add_row(1, "新しいしおり", 1))
        b_del.clicked.connect(self._delete_row)
        b_up.clicked.connect(lambda: self._move_row(-1))
        b_down.clicked.connect(lambda: self._move_row(1))

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self._on_accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _add_row(self, level, title, page) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(str(level)))
        self.table.setItem(r, 1, QTableWidgetItem(str(title)))
        self.table.setItem(r, 2, QTableWidgetItem(str(page)))

    def _delete_row(self) -> None:
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def _move_row(self, step: int) -> None:
        r = self.table.currentRow()
        nr = r + step
        if r < 0 or not (0 <= nr < self.table.rowCount()):
            return
        cells = [self.table.takeItem(r, c) for c in range(3)]
        self.table.removeRow(r)
        self.table.insertRow(nr)
        for c, it in enumerate(cells):
            self.table.setItem(nr, c, it)
        self.table.setCurrentCell(nr, 1)

    def _on_accept(self) -> None:
        try:
            self.result_toc()  # 検証
        except ValueError as exc:
            QMessageBox.warning(self, "入力エラー", str(exc))
            return
        self.accept()

    def result_toc(self) -> list:
        toc = []
        for r in range(self.table.rowCount()):
            lv_item = self.table.item(r, 0)
            t_item = self.table.item(r, 1)
            p_item = self.table.item(r, 2)
            try:
                level = int((lv_item.text() if lv_item else "1").strip())
                page = int((p_item.text() if p_item else "1").strip())
            except ValueError:
                raise ValueError(f"{r + 1} 行目: レベルとページは整数で入力してください")
            title = (t_item.text() if t_item else "").strip()
            if level < 1:
                raise ValueError(f"{r + 1} 行目: レベルは1以上です")
            if not (1 <= page <= self._page_count):
                raise ValueError(f"{r + 1} 行目: ページは 1〜{self._page_count} です")
            if not title:
                raise ValueError(f"{r + 1} 行目: タイトルが空です")
            # 階層は 1 つずつ深くなる（レベル 1 の次にいきなり 3 は PDF の決まりで
            # 作れず、保存の段階で例外になって入力が失われていた）
            if toc and level > toc[-1][0] + 1:
                raise ValueError(
                    f"{r + 1} 行目: レベルは、前の項目（レベル {toc[-1][0]}）より"
                    f" 1 つ深い {toc[-1][0] + 1} までです")
            toc.append([level, title, page])
        # 先頭はレベル1である必要がある
        if toc and toc[0][0] != 1:
            raise ValueError("最初の項目はレベル1にしてください")
        return toc
