"""PDF 未読込時に表示するウェルカム画面。

アプリ名と「開く」だけだと初回以降は用が無い画面になるので、最近使った
ファイルをカードで並べて、ここから 1 クリックで戻れるようにしている。
色はテーマ側（theme.py の #welcomeCard などのセレクタ）で当たる。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .util import exists_fast

MAX_RECENT = 5  # カードで出す最近のファイル数


class _RecentTile(QPushButton):
    """最近使ったファイル 1 件（ファイル名＋フォルダ名）。"""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.setObjectName("recentTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(path)
        self.setFlat(True)
        self.setMinimumHeight(48)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)

        icon = QLabel("📄")
        icon.setStyleSheet("font-size: 20px;")
        lay.addWidget(icon)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        name = QLabel(os.path.basename(path))
        name.setObjectName("recentName")
        folder = QLabel(os.path.dirname(path) or "")
        folder.setObjectName("recentDir")
        # 長いパスでカードが横に伸びないよう、末尾を省略して 1 行に収める
        for lbl in (name, folder):
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            lbl.setMinimumWidth(1)
        col.addWidget(name)
        col.addWidget(folder)
        lay.addLayout(col, 1)


class WelcomeWidget(QWidget):
    """中央にアプリ名・「開く」・最近使ったファイルを表示する。"""

    open_requested = Signal()
    file_requested = Signal(str)   # 最近使ったファイルのカードが押された

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)

        card = QWidget()
        card.setObjectName("welcomeCard")
        card.setFixedWidth(480)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(36, 34, 36, 30)
        lay.setSpacing(12)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        logo = QLabel("📕")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet("font-size: 52px;")
        lay.addWidget(logo)

        title = QLabel("TriV-Reader")
        title.setObjectName("welcomeTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)

        subtitle = QLabel("閲覧・編集・注釈・OCR・圧縮まで、これひとつで。")
        subtitle.setObjectName("welcomeSub")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setWordWrap(True)
        lay.addWidget(subtitle)

        lay.addSpacing(6)
        self.open_button = QPushButton("PDF を開く")
        self.open_button.setObjectName("primary")
        self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_button.clicked.connect(self.open_requested.emit)
        lay.addWidget(self.open_button, alignment=Qt.AlignmentFlag.AlignCenter)

        # --- 最近使ったファイル -----------------------------------
        self._recent_head = QLabel("最近使ったファイル")
        self._recent_head.setObjectName("welcomeHint")
        lay.addSpacing(6)
        lay.addWidget(self._recent_head)
        self._recent_box = QVBoxLayout()
        self._recent_box.setContentsMargins(0, 0, 0, 0)
        self._recent_box.setSpacing(5)
        lay.addLayout(self._recent_box)
        self._tiles: list[_RecentTile] = []
        self.set_recent([])

        hint = QLabel("ここに PDF をドラッグ&ドロップしても開けます")
        hint.setObjectName("welcomeHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addSpacing(4)
        lay.addWidget(hint)

        outer.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)

    # --- 最近使ったファイル ---------------------------------------
    def set_recent(self, paths: list[str]) -> None:
        """カードを作り直す。存在しないファイルは出さない。"""
        for tile in self._tiles:
            self._recent_box.removeWidget(tile)
            tile.deleteLater()
        self._tiles.clear()
        alive = [p for p in (paths or []) if p and exists_fast(p)][:MAX_RECENT]
        self._recent_head.setVisible(bool(alive))
        for path in alive:
            tile = _RecentTile(path)
            tile.clicked.connect(lambda _=False, p=path: self.file_requested.emit(p))
            self._recent_box.addWidget(tile)
            self._tiles.append(tile)

    def apply_theme(self, dark: bool) -> None:
        """テーマ切替時に、スタイルを当て直す（色は QSS 側）。"""
        for w in [self, self.open_button, *self._tiles]:
            w.style().unpolish(w)
            w.style().polish(w)
