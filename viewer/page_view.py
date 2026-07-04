"""連続スクロール対応のページ表示ウィジェット。

全ページを縦に並べて表示し、ビューポートに見えている（近い）ページだけを
遅延描画する。遠いページの QPixmap は解放してメモリ使用を抑える。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QPolygon,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QWidget,
)

from .document import PdfDocument
from .govform_js import SIMPLE_CALL_RE

PAGE_SPACING = 12  # ページ間の余白(px)


class _GovFormClickOverlay(QPushButton):
    """官公庁フォームの「丸で選択」JS トグル用の透明クリック領域。

    PDF 側の外観（選択中/非選択の2枚のうち見えている方）がそのまま
    透けて見えるよう、枠なし・背景なしのボタンをかぶせるだけにする。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFlat(True)
        self.setStyleSheet(
            "QPushButton { background: transparent; border: none; }"
            "QPushButton:hover { background: rgba(112, 150, 216, 60); }")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("クリックして選択")


class _FormTextArea(QPlainTextEdit):
    """複数行フォーム欄。フォーカスが外れたら committed を発火する。"""

    committed = Signal()

    def focusOutEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().focusOutEvent(event)
        self.committed.emit()


class _CombLineEdit(QLineEdit):
    """コム欄（1文字1マス）の入力欄。各文字をマスの中央に自前描画する。

    字間（letter-spacing）でのマス目再現は半角基準の均等割りになり、
    全角文字（かな・カナ・漢字）でマスからズレるため、Acrobat と同じく
    文字ごとにマス中央へ配置して描画する。IME の未確定文字（プリエディット）
    もカーソル位置のマスに下線付きで表示する。
    """

    def __init__(self, maxlen: int, parent=None) -> None:
        super().__init__(parent)
        self._cells = max(1, int(maxlen))
        self._preedit = ""
        self.setMaxLength(self._cells)
        self.setFrame(False)
        # ネイティブ描画は使わない（paintEvent を完全に置き換える）
        self.setStyleSheet("QLineEdit { background: transparent; color: transparent; }")

    def inputMethodEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().inputMethodEvent(event)
        self._preedit = event.preeditString()
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        # ネイティブのヒットテストは字間前提でズレるため、マス単位で決める
        cell = self.width() / self._cells
        pos = int(event.position().x() / cell + 0.5) if cell > 0 else 0
        self.setCursorPosition(max(0, min(pos, len(self.text()))))
        event.accept()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        p = QPainter(self)
        r = self.rect()
        cell = r.width() / self._cells
        # 背景と枠（Adobe 風の薄青ハイライト）。下に描画済みの PDF 側の文字が
        # 透けて二重に見えないよう不透明にする
        p.fillRect(r, QColor(225, 235, 255))
        p.setPen(QPen(QColor(0x70, 0x96, 0xD8)))
        p.drawRect(r.adjusted(0, 0, -1, -1))
        # マスの区切り線（薄く）
        sep = QColor(0x70, 0x96, 0xD8)
        sep.setAlpha(80)
        p.setPen(QPen(sep))
        for i in range(1, self._cells):
            x = round(i * cell)
            p.drawLine(x, 2, x, r.height() - 3)

        text = self.text()
        cur = self.cursorPosition()
        disp = text[:cur] + self._preedit + text[cur:]
        fm = p.fontMetrics()
        baseline = (r.height() + fm.ascent() - fm.descent()) / 2

        # 選択範囲のマスを強調
        if self.hasSelectedText():
            s = self.selectionStart()
            e = s + len(self.selectedText())
            p.fillRect(QRect(round(s * cell), 1,
                             round((e - s) * cell), r.height() - 2),
                       QColor(120, 160, 230, 120))

        p.setPen(QColor(0x11, 0x11, 0x11))
        for i, ch in enumerate(disp[:self._cells]):
            w_ch = fm.horizontalAdvance(ch)
            x = i * cell + (cell - w_ch) / 2
            p.drawText(int(x), int(baseline), ch)
            # プリエディット（未確定文字）には下線
            if self._preedit and cur <= i < cur + len(self._preedit):
                y = min(int(baseline) + 2, r.height() - 3)
                p.drawLine(int(x), y, int(x + w_ch), y)

        # カーソル（フォーカス時のみ・マスの左端に描く）
        if self.hasFocus() and not self.hasSelectedText():
            cpos = min(cur + len(self._preedit), self._cells)
            if cpos < self._cells:
                cx = round(cpos * cell) + 2
            else:
                cx = r.width() - 3
            p.setPen(QPen(QColor(0x11, 0x11, 0x11), 1))
            p.drawLine(cx, 3, cx, r.height() - 4)
        p.end()


class _AutoFitLineEdit(QLineEdit):
    """入力に応じて文字サイズを自動縮小する1行フォーム欄。

    setFont() を IME 変換中（プリエディット表示中）に呼ぶと、Windows の
    日本語IMEでは変換中の内容がキャンセルされ、確定（Enter）しても入力
    した文字が消えてしまうことがある。変換中はフォント変更を保留し、
    確定した「次のイベントループ」でまとめて反映することでこれを避ける。

    重要: 確定直後のフィットを inputMethodEvent の処理中（同じターン内）に
    同期実行すると、setFont が「まだ IME イベント処理中」に走るため、上記の
    キャンセルが起きて入力文字が丸ごと消える。必ず QTimer.singleShot(0, ...) で
    イベント処理が完了した後に回すこと（合成 QInputMethodEvent のテストでは
    native IME の取り消しが再現されないため、ここは実機の日本語IMEで確認する）。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.composing = False

    def inputMethodEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().inputMethodEvent(event)
        was_composing = self.composing
        self.composing = bool(event.preeditString())
        if was_composing and not self.composing:
            # 変換確定直後にフィットさせる。ただし今このターンで setFont すると
            # IME が確定を取り消すため、次のイベントループへ必ず遅延させる。
            QTimer.singleShot(0, self._refit_after_commit)

    def _refit_after_commit(self) -> None:
        # タイマー発火までに新しい変換が始まっていたら、その確定を待つ
        # （フォント変更が再び変換を壊さないように）。
        if self.composing:
            return
        try:
            self.textChanged.emit(self.text())
        except RuntimeError:
            pass  # 遅延発火までに欄が破棄済み（ページ切替・フォーム解除等）


# 注釈ツール
TOOL_NONE = "none"
TOOL_HIGHLIGHT = "highlight"
TOOL_RECT = "rect"
TOOL_PEN = "pen"
TOOL_TEXT = "text"
TOOL_NOTE = "note"
TOOL_ERASE = "erase"
TOOL_UNDERLINE = "underline"
TOOL_STRIKEOUT = "strikeout"
TOOL_LINE = "line"
TOOL_ARROW = "arrow"
TOOL_CIRCLE = "circle"
TOOL_REDACT = "redact"
TOOL_CROP = "crop"

# ドラッグ矩形系（始点・終点で矩形を作る）
_RECT_TOOLS = {TOOL_HIGHLIGHT, TOOL_RECT, TOOL_TEXT, TOOL_CIRCLE, TOOL_REDACT, TOOL_CROP}
# 始点→終点の線/テキストマーク系
_LINE_TOOLS = {TOOL_LINE, TOOL_ARROW, TOOL_UNDERLINE, TOOL_STRIKEOUT}

HANDLE_DRAW = 9   # ハンドルの描画サイズ(px)
HANDLE_HIT = 8    # ハンドルの当たり判定半径(px)


def _handle_centers(box):
    """選択枠 (x0,y0,x1,y1) の8ハンドル中心。0-3=四隅, 4-7=各辺中点。"""
    x0, y0, x1, y1 = box
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    return {0: (x0, y0), 1: (x1, y0), 2: (x1, y1), 3: (x0, y1),
            4: (mx, y0), 5: (x1, my), 6: (mx, y1), 7: (x0, my)}


def _resize_box(box, handle, dx, dy):
    """ハンドルに応じて枠を変形（最小サイズ確保・正規化）。"""
    x0, y0, x1, y1 = box
    if handle in (0, 3, 7):
        x0 += dx
    if handle in (1, 2, 5):
        x1 += dx
    if handle in (0, 1, 4):
        y0 += dy
    if handle in (2, 3, 6):
        y1 += dy
    nx0, nx1 = sorted((x0, x1))
    ny0, ny1 = sorted((y0, y1))
    if nx1 - nx0 < 6:
        nx1 = nx0 + 6
    if ny1 - ny0 < 6:
        ny1 = ny0 + 6
    return (nx0, ny0, nx1, ny1)


class _PageLabel(QLabel):
    """1 ページ分の表示ラベル。注釈ツール使用時はマウス描画を処理する。"""

    def __init__(self, index: int, view: "PageView") -> None:
        super().__init__()
        self._index = index
        self._view = view
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background:#e6e6e6; border:1px solid #b0b0b0;")
        self.setCursor(Qt.CursorShape.IBeamCursor)  # 既定(選択ツール)=テキスト選択
        self._drawing = False
        self._start = QPoint()
        self._cur = QPoint()
        self._stroke: list[QPoint] = []  # ペン用
        self._sel_moving = False
        self._move_offset = QPoint()
        self._text_selecting = False
        self._resizing = False
        self._resize_handle = None
        self._box_start = None
        self._cur_box = None
        self._press = QPoint()

    # --- マウス ---------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 中クリック: ブラウザ風オートスクロールの開始/停止（ページ上でも効くように）
        if self._view._auto_active:
            self._view._stop_autoscroll()
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            vp = self._view.viewport().mapFromGlobal(event.globalPosition().toPoint())
            self._view._start_autoscroll(vp)
            event.accept()
            return
        tool = self._view.tool
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position().toPoint()
        if tool == TOOL_NONE:
            # 選択中なら、まずリサイズハンドルの上か判定
            box = self._view.selection_box_label(self._index)
            if box is not None:
                handle = self._handle_at(pos, box)
                if handle is not None:
                    self._resizing = True
                    self._resize_handle = handle
                    self._box_start = box
                    self._cur_box = box
                    self._press = pos
                    return
            # 注釈に当たれば選択して移動、外れたらテキスト選択を開始
            xref = self._view.hit_annot(self._index, pos)
            if xref is not None:
                self._view.set_selection(self._index, xref)
                self._sel_moving = True
                self._start = pos
                self._move_offset = QPoint()
            else:
                self._view.clear_selection()
                self._text_selecting = True
                self._start = pos
                self._view.clear_text_selection()
            return
        if tool in (TOOL_NOTE, TOOL_ERASE):
            self._view.commit_point(self._index, tool, pos)
            return
        self._drawing = True
        self._start = pos
        self._cur = pos
        self._stroke = [pos]

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._text_selecting:
            self._view.update_text_selection(
                self._index, self._start, event.position().toPoint()
            )
            return
        if self._resizing:
            d = event.position().toPoint() - self._press
            self._cur_box = _resize_box(self._box_start, self._resize_handle, d.x(), d.y())
            self.update()
            return
        if self._sel_moving:
            self._move_offset = event.position().toPoint() - self._start
            self.update()
            return
        if not self._drawing:
            return
        self._cur = event.position().toPoint()
        if self._view.tool == TOOL_PEN:
            self._stroke.append(self._cur)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._text_selecting:
            self._text_selecting = False
            return
        if self._resizing:
            self._resizing = False
            box = self._cur_box
            self._cur_box = None
            self._box_start = None
            self._resize_handle = None
            if box is not None:
                self._view.resize_selection(self._index, box)
            self.update()
            return
        if self._sel_moving:
            self._sel_moving = False
            end = event.position().toPoint()
            if (end - self._start).manhattanLength() > 3:
                self._view.move_selection(self._index, self._start, end)
            self._move_offset = QPoint()
            self.update()
            return
        if not self._drawing:
            return
        self._drawing = False
        tool = self._view.tool
        if tool == TOOL_PEN:
            self._view.commit_stroke(self._index, list(self._stroke))
        else:
            self._view.commit_drag(self._index, self._start, self._cur, tool)
        self._stroke = []
        self.update()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        if self._view.tool == TOOL_NONE and self._view.has_text_selection():
            menu = QMenu(self)
            act = menu.addAction("コピー")
            if menu.exec(event.globalPos()) == act:
                self._view.copy_selection()
            return
        super().contextMenuEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        # 連続した中クリックの2回目はダブルクリックとして届くため、ここでも処理する
        if self._view._auto_active:
            self._view._stop_autoscroll()
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            vp = self._view.viewport().mapFromGlobal(event.globalPosition().toPoint())
            self._view._start_autoscroll(vp)
            event.accept()
            return
        if self._view.tool == TOOL_NONE and event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            xref = self._view.hit_annot(self._index, pos)
            if xref is not None:
                self._view.set_selection(self._index, xref)
                self._view.edit_selection_text()
                return
        super().mouseDoubleClickEvent(event)

    def _handle_at(self, pos, box):
        for hid, (hx, hy) in _handle_centers(box).items():
            if abs(pos.x() - hx) <= HANDLE_HIT and abs(pos.y() - hy) <= HANDLE_HIT:
                return hid
        return None

    # --- 描画中プレビュー -----------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        # テキスト選択の塗り
        tboxes = self._view.text_sel_boxes_label(self._index)
        if tboxes:
            tp = QPainter(self)
            sel = QColor(51, 153, 255, 90)
            for (x0, y0, x1, y1) in tboxes:
                tp.fillRect(QRect(int(x0), int(y0), int(x1 - x0), int(y1 - y0)), sel)
            tp.end()
        # 検索ヒットのハイライト
        boxes = self._view.search_boxes_label(self._index)
        if boxes:
            hp = QPainter(self)
            for (x0, y0, x1, y1), is_cur in boxes:
                rect = QRect(int(x0), int(y0), int(x1 - x0), int(y1 - y0))
                if is_cur:
                    hp.fillRect(rect, QColor(255, 140, 0, 110))
                    hp.setPen(QPen(QColor(230, 100, 0), 2))
                    hp.drawRect(rect)
                else:
                    hp.fillRect(rect, QColor(255, 235, 0, 90))
            hp.end()
        # 選択中の注釈の枠（移動/リサイズプレビュー・ハンドル）
        if not self._drawing:
            if self._resizing and self._cur_box is not None:
                box = self._cur_box
            else:
                b = self._view.selection_box_label(self._index)
                if b is None:
                    return
                ox, oy = self._move_offset.x(), self._move_offset.y()
                box = (b[0] + ox, b[1] + oy, b[2] + ox, b[3] + oy)
            x0, y0, x1, y1 = box
            rect = QRect(int(x0), int(y0), int(x1 - x0), int(y1 - y0))
            sp = QPainter(self)
            sp.setPen(QPen(QColor(26, 115, 232), 2, Qt.PenStyle.DashLine))
            sp.drawRect(rect)
            sp.fillRect(rect, QColor(26, 115, 232, 30))
            # リサイズハンドル（白地に青枠の四角）
            sp.setPen(QPen(QColor(26, 115, 232), 1, Qt.PenStyle.SolidLine))
            for hx, hy in _handle_centers(box).values():
                hr = QRect(int(hx - HANDLE_DRAW / 2), int(hy - HANDLE_DRAW / 2),
                           HANDLE_DRAW, HANDLE_DRAW)
                sp.fillRect(hr, QColor(255, 255, 255))
                sp.drawRect(hr)
            sp.end()
            return
        painter = QPainter(self)
        color = self._view.color
        tool = self._view.tool
        rect = QRect(self._start, self._cur).normalized()
        qt_dash = {"solid": Qt.PenStyle.SolidLine, "dashed": Qt.PenStyle.DashLine,
                   "dotted": Qt.PenStyle.DotLine}.get(self._view.line_style, Qt.PenStyle.SolidLine)
        if tool in (TOOL_HIGHLIGHT, TOOL_UNDERLINE, TOOL_STRIKEOUT):
            fill = QColor(color)
            fill.setAlpha(round(70 * self._view.opacity))
            painter.fillRect(rect, fill)
        elif tool == TOOL_PEN:
            pen_color = QColor(color)
            pen_color.setAlphaF(self._view.opacity)
            painter.setPen(QPen(pen_color, max(1, self._view.pen_width), Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            for i in range(1, len(self._stroke)):
                painter.drawLine(self._stroke[i - 1], self._stroke[i])
        elif tool in (TOOL_LINE, TOOL_ARROW):
            painter.setOpacity(self._view.opacity)
            painter.setPen(QPen(QColor(color), max(1, self._view.pen_width), qt_dash))
            painter.drawLine(self._start, self._cur)
            end_key = self._view.line_end
            if end_key == "none" and tool == TOOL_ARROW:
                end_key = "closed_arrow"
            self._draw_arrowhead(painter, self._start, self._cur, end_key, color)
            self._draw_arrowhead(painter, self._cur, self._start, self._view.line_start, color)
        elif tool == TOOL_CIRCLE:
            painter.setOpacity(self._view.opacity)
            painter.setPen(QPen(QColor(color), max(1, self._view.pen_width), qt_dash))
            fill_rgb = self._view._fill_rgb()
            if fill_rgb is not None:
                painter.setBrush(self._view.fill_color)
            painter.drawEllipse(rect)
        elif tool == TOOL_REDACT:
            painter.fillRect(rect, QColor(0, 0, 0, 160))
        elif tool == TOOL_RECT:
            painter.setOpacity(self._view.opacity)
            painter.setPen(QPen(QColor(color), max(1, self._view.pen_width), qt_dash))
            fill_rgb = self._view._fill_rgb()
            if fill_rgb is not None:
                painter.setBrush(self._view.fill_color)
            painter.drawRect(rect)
        else:  # テキスト枠等のプレビュー
            painter.setPen(QPen(QColor(color), 1, Qt.PenStyle.DashLine))
            painter.drawRect(rect)
        painter.end()

    @staticmethod
    def _draw_arrowhead(painter: QPainter, tail: QPoint, tip: QPoint, style: str, color) -> None:
        """tail→tip 方向の終端に、style に応じた簡易的な線端記号を描く（ドラッグ中プレビュー用）。"""
        if style in ("none", None):
            return
        import math
        dx, dy = tip.x() - tail.x(), tip.y() - tail.y()
        length = math.hypot(dx, dy)
        if length < 1:
            return
        ux, uy = dx / length, dy / length
        size = 10
        if style in ("open_arrow", "closed_arrow", "r_open_arrow", "r_closed_arrow"):
            spread = 0.5
            back = -ux if style.startswith("r_") else ux
            backy = -uy if style.startswith("r_") else uy
            p1 = QPoint(int(tip.x() - size * (back + spread * -uy)),
                        int(tip.y() - size * (backy + spread * ux)))
            p2 = QPoint(int(tip.x() - size * (back - spread * -uy)),
                        int(tip.y() - size * (backy - spread * ux)))
            poly = QPolygon([tip, p1, p2])
            if "closed" in style:
                painter.setBrush(QColor(color))
            else:
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(poly)
        elif style == "square":
            painter.drawRect(tip.x() - size // 2, tip.y() - size // 2, size, size)
        elif style == "circle":
            painter.drawEllipse(tip, size // 2, size // 2)
        elif style == "diamond":
            pts = [QPoint(tip.x(), tip.y() - size // 2), QPoint(tip.x() + size // 2, tip.y()),
                   QPoint(tip.x(), tip.y() + size // 2), QPoint(tip.x() - size // 2, tip.y())]
            painter.drawPolygon(QPolygon(pts))


class PageView(QScrollArea):
    """縦スクロールで全ページを連続表示する。"""

    page_changed = Signal(int)        # 表示中（中央付近）のページが変わった
    annotation_changed = Signal(int)  # 注釈を追加/削除したページ
    selection_changed = Signal(bool)       # 注釈の選択有無が変わった
    text_selection_changed = Signal(bool)  # テキスト選択の有無が変わった
    zoom_changed = Signal(float)           # ズーム倍率が変わった
    form_changed = Signal()                # フォーム入力欄の値が変わった

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._container = QWidget()
        # グリッド配置（1列=通常、2列=見開き）。ラベルはコンテナ直下なので
        # label.y() はコンテナ基準のままで、遅延描画ロジックがそのまま使える。
        self._layout = QGridLayout(self._container)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self._layout.setSpacing(PAGE_SPACING)
        self._layout.setContentsMargins(PAGE_SPACING, PAGE_SPACING, PAGE_SPACING, PAGE_SPACING)
        self._layout.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        self.setWidget(self._container)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.setBackgroundRole(self.backgroundRole())

        self._doc: PdfDocument | None = None
        self._labels: list[QLabel] = []
        self._rendered: set[int] = set()
        # フォーム（入力欄）入力モード
        self._form_mode = False
        self._form_overlays: list[QWidget] = []
        self._index: int = 0
        self._zoom: float = 1.0
        self._cols = 1  # 1=通常 / 2=見開き
        self._suspend_scroll = False

        # 注釈ツールの状態
        self.tool = TOOL_NONE
        self.color = QColor(255, 219, 46)   # 既定はハイライト用の黄
        self.pen_width = 2
        self.line_style = "solid"    # solid / dashed / dotted
        self.line_start = "none"     # 直線・矢印の始点スタイル（LINE_END_STYLES のキー）
        self.line_end = "none"       # 直線・矢印の終点スタイル
        self.fill_enabled = False    # 四角/円の塗りつぶし
        self.fill_color = QColor(255, 255, 255)
        self.opacity = 1.0           # 0.0-1.0
        self._sel_index = -1
        self._sel_xref = None
        # 検索
        self._search_hits: list = []   # [(page_index, fitz.Rect)]
        self._search_cur = -1
        # テキスト選択
        self._text_sel_index = -1
        self._text_sel_rects: list = []   # PDF座標の矩形
        self._text_sel_text = ""

        # 中クリックによるオートスクロール（ブラウザ風）
        self._auto_active = False
        self._auto_origin = QPoint()
        self._auto_pos = QPoint()
        self._auto_moved = False
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(16)  # 約60fps
        self._auto_timer.timeout.connect(self._auto_scroll_tick)
        self.viewport().installEventFilter(self)
        # キーボード操作（矢印でページ送り/スクロール）を受けられるように
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    # --- スクロール位置（タブ切替時の記憶に使う）------------------------
    def scroll_value(self) -> int:
        return self.verticalScrollBar().value()

    def set_scroll_value(self, value: int) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(max(0, min(value, bar.maximum())))

    # --- 公開 API -------------------------------------------------------
    def set_document(self, doc: PdfDocument) -> None:
        self._doc = doc
        self._index = 0
        self._zoom = 1.0
        self._form_mode = False
        self._clear_form_overlays()
        self._rebuild(fit=True, target=0)

    def reload(self, target: int = 0) -> None:
        """ページ構成が変わった後（削除・並べ替え・統合）に再構築する。

        現在のズーム倍率は維持し、target ページへスクロールする。
        """
        if self._doc and self._doc.is_open:
            target = max(0, min(target, self._doc.page_count - 1))
        self._rebuild(fit=False, target=target)

    @property
    def index(self) -> int:
        return self._index

    @property
    def zoom(self) -> float:
        return self._zoom

    def set_canvas_color(self, color: str) -> None:
        """ページ周辺(余白)の背景色をテーマに合わせる。"""
        self.viewport().setStyleSheet(f"background: {color};")

    # --- 注釈ツール -----------------------------------------------------
    def set_tool(self, tool: str) -> None:
        self.tool = tool
        if tool != TOOL_NONE:
            self.clear_selection()       # 描画ツール中は注釈選択を解除
            self.clear_text_selection()  # テキスト選択も解除
        # ツール使用時は手のひらカーソル等にして分かりやすく
        if tool == TOOL_NONE:
            self.viewport().unsetCursor()
        cursor = {
            TOOL_ERASE: Qt.CursorShape.PointingHandCursor,
            # 選択モードはテキスト選択ができることを示す I ビームカーソル
            TOOL_NONE: Qt.CursorShape.IBeamCursor,
        }.get(tool, Qt.CursorShape.CrossCursor)
        for lbl in self._labels:
            lbl.setCursor(cursor)

    def set_color(self, qcolor: QColor) -> None:
        self.color = qcolor

    def set_pen_width(self, width: int) -> None:
        self.pen_width = max(1, int(width))

    def set_line_style(self, style: str) -> None:
        self.line_style = style

    def set_line_start(self, key: str) -> None:
        self.line_start = key

    def set_line_end(self, key: str) -> None:
        self.line_end = key

    def set_fill_enabled(self, on: bool) -> None:
        self.fill_enabled = bool(on)

    def set_fill_color(self, qcolor: QColor) -> None:
        self.fill_color = qcolor

    def set_opacity(self, opacity: float) -> None:
        self.opacity = max(0.05, min(1.0, float(opacity)))

    def _rgb(self) -> tuple:
        c = self.color
        return (c.redF(), c.greenF(), c.blueF())

    def _fill_rgb(self):
        if not self.fill_enabled:
            return None
        c = self.fill_color
        return (c.redF(), c.greenF(), c.blueF())

    def _dashes(self):
        from .document import LINE_DASH_PATTERNS
        pattern = LINE_DASH_PATTERNS.get(self.line_style)
        if not pattern:
            return None
        return [max(1, round(v * self.pen_width / 2)) for v in pattern]

    def commit_drag(self, index: int, p0: QPoint, p1: QPoint, tool: str) -> None:
        if not self._doc:
            return
        a = self._doc.label_to_pdf_point(index, p0.x(), p0.y(), self._zoom)
        b = self._doc.label_to_pdf_point(index, p1.x(), p1.y(), self._zoom)

        # 線・矢印・下線・取消線は始点→終点で扱う
        if tool in (TOOL_LINE, TOOL_ARROW):
            if (p1 - p0).manhattanLength() < 3:
                return
            self._doc.add_line(index, a, b, color=self._rgb(), width=self.pen_width,
                               arrow=(tool == TOOL_ARROW),
                               start=self.line_start, end=self.line_end,
                               dashes=self._dashes(), opacity=self.opacity)
            self._after_annot(index)
            return
        if tool == TOOL_UNDERLINE:
            self._doc.add_underline(index, a, b, color=self._rgb())
            self._after_annot(index)
            return
        if tool == TOOL_STRIKEOUT:
            self._doc.add_strikeout(index, a, b, color=self._rgb())
            self._after_annot(index)
            return

        rect = self._doc.label_to_pdf_rect(
            index, p0.x(), p0.y(), p1.x(), p1.y(), self._zoom
        )
        if rect.width < 2 or rect.height < 2:
            return  # 誤クリックの極小矩形は無視
        if tool == TOOL_HIGHLIGHT:
            if not self._doc.add_text_highlight(index, a, b, color=self._rgb()):
                self._doc.add_highlight(index, rect, color=self._rgb())
        elif tool == TOOL_RECT:
            self._doc.add_rect(index, rect, color=self._rgb(), width=self.pen_width,
                               fill=self._fill_rgb(), dashes=self._dashes(),
                               opacity=self.opacity)
        elif tool == TOOL_CIRCLE:
            self._doc.add_circle(index, rect, color=self._rgb(), width=self.pen_width,
                                 fill=self._fill_rgb(), dashes=self._dashes(),
                                 opacity=self.opacity)
        elif tool == TOOL_REDACT:
            self._doc.add_redaction(index, rect)
        elif tool == TOOL_CROP:
            self._doc.set_crop(index, rect)
            self.reload(index)  # ページサイズが変わるので再構築
            return
        elif tool == TOOL_TEXT:
            from PySide6.QtWidgets import QInputDialog
            text, ok = QInputDialog.getMultiLineText(self, "テキスト注釈", "本文:")
            if not ok or not text.strip():
                return
            self._doc.add_freetext(index, rect, text, color=self._rgb())
        self._after_annot(index)

    def commit_stroke(self, index: int, points: list) -> None:
        if not self._doc or len(points) < 2:
            return
        stroke = [self._doc.label_to_pdf_point(index, p.x(), p.y(), self._zoom)
                  for p in points]
        self._doc.add_ink(index, [stroke], color=self._rgb(), width=self.pen_width,
                          opacity=self.opacity)
        self._after_annot(index)

    def commit_point(self, index: int, tool: str, p: QPoint) -> None:
        if not self._doc:
            return
        pt = self._doc.label_to_pdf_point(index, p.x(), p.y(), self._zoom)
        if tool == TOOL_NOTE:
            from PySide6.QtWidgets import QInputDialog
            text, ok = QInputDialog.getMultiLineText(self, "付箋メモ", "本文:")
            if not ok or not text.strip():
                return
            self._doc.add_text_note(index, pt, text)
            self._after_annot(index)
        elif tool == TOOL_ERASE:
            if self._doc.delete_annot_at(index, pt):
                self._after_annot(index)

    def _after_annot(self, index: int) -> None:
        self._rendered.discard(index)
        self._render_visible()
        self.annotation_changed.emit(index)

    def apply_redactions(self) -> None:
        """登録済みの墨消しを全ページに適用し、全ページを再描画する。"""
        if not self._doc:
            return
        self._doc.apply_redactions()
        self.reload(self._index)
        self.annotation_changed.emit(self._index)

    # --- 検索 -----------------------------------------------------------
    def set_search_results(self, hits: list) -> None:
        self._search_hits = hits
        self._search_cur = 0 if hits else -1
        self._repaint_labels()
        if hits:
            self.goto_hit(0)

    def clear_search(self) -> None:
        self._search_hits = []
        self._search_cur = -1
        self._repaint_labels()

    def search_count(self) -> int:
        return len(self._search_hits)

    def search_current(self) -> int:
        return self._search_cur

    def goto_hit(self, i: int) -> None:
        if not self._search_hits:
            return
        i %= len(self._search_hits)
        self._search_cur = i
        page_index, rect = self._search_hits[i]
        self.set_page(page_index)  # まずページ先頭へ
        # ヒット位置が見えるよう微調整スクロール
        label = self._labels[page_index]
        box = self._doc.pdf_rect_to_label(page_index, rect, self._zoom)
        self._suspend_scroll = True
        self.verticalScrollBar().setValue(int(label.y() + box[1]) - 80)
        self._suspend_scroll = False
        self._render_visible()
        self._repaint_labels()

    def search_boxes_label(self, index: int) -> list:
        """index ページ上の検索ヒット枠 [(box, is_current), ...]。"""
        if not self._search_hits or not self._doc:
            return []
        out = []
        for j, (pi, rect) in enumerate(self._search_hits):
            if pi == index:
                out.append((self._doc.pdf_rect_to_label(index, rect, self._zoom),
                            j == self._search_cur))
        return out

    def _repaint_labels(self) -> None:
        for lbl in self._labels:
            lbl.update()

    # --- テキスト選択 / コピー ----------------------------------------
    def update_text_selection(self, index: int, p0: QPoint, p1: QPoint) -> None:
        if not self._doc:
            return
        a = self._doc.label_to_pdf_point(index, p0.x(), p0.y(), self._zoom)
        b = self._doc.label_to_pdf_point(index, p1.x(), p1.y(), self._zoom)
        text, rects = self._doc.select_text(index, a, b)
        prev_index = self._text_sel_index
        had = bool(self._text_sel_text)
        self._text_sel_index = index
        self._text_sel_rects = rects
        self._text_sel_text = text
        if 0 <= prev_index < len(self._labels) and prev_index != index:
            self._labels[prev_index].update()
        if 0 <= index < len(self._labels):
            self._labels[index].update()
        if bool(text) != had:
            self.text_selection_changed.emit(bool(text))

    def clear_text_selection(self) -> None:
        idx = self._text_sel_index
        had = bool(self._text_sel_text)
        self._text_sel_index = -1
        self._text_sel_rects = []
        self._text_sel_text = ""
        if 0 <= idx < len(self._labels):
            self._labels[idx].update()
        if had:
            self.text_selection_changed.emit(False)

    def has_text_selection(self) -> bool:
        return bool(self._text_sel_text)

    def selection_text(self) -> str:
        return self._text_sel_text

    def copy_selection(self) -> bool:
        if not self._text_sel_text:
            return False
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self._text_sel_text)
        return True

    def text_sel_boxes_label(self, index: int) -> list:
        if index != self._text_sel_index or not self._doc:
            return []
        return [self._doc.pdf_rect_to_label(index, r, self._zoom)
                for r in self._text_sel_rects]

    # --- 注釈の選択・移動・編集 ---------------------------------------
    def hit_annot(self, index: int, pos: QPoint):
        if not self._doc:
            return None
        pt = self._doc.label_to_pdf_point(index, pos.x(), pos.y(), self._zoom)
        return self._doc.annot_at(index, pt)

    def set_selection(self, index: int, xref) -> None:
        prev = self._sel_index
        self._sel_index = index
        self._sel_xref = xref
        if 0 <= prev < len(self._labels) and prev != index:
            self._labels[prev].update()
        if 0 <= index < len(self._labels):
            self._labels[index].update()
        self.selection_changed.emit(self.has_selection())

    def clear_selection(self) -> None:
        idx = self._sel_index
        self._sel_index = -1
        self._sel_xref = None
        if 0 <= idx < len(self._labels):
            self._labels[idx].update()
        self.selection_changed.emit(False)

    def has_selection(self) -> bool:
        return self._sel_xref is not None

    def selection_box_label(self, index: int):
        """index ページが選択中なら、注釈枠のラベル座標 (x0,y0,x1,y1) を返す。"""
        if index != self._sel_index or self._sel_xref is None or not self._doc:
            return None
        rect = self._doc.annot_rect(index, self._sel_xref)
        if rect is None:
            return None
        return self._doc.pdf_rect_to_label(index, rect, self._zoom)

    def move_selection(self, index: int, p0: QPoint, p1: QPoint) -> None:
        if self._sel_xref is None or not self._doc:
            return
        a = self._doc.label_to_pdf_point(index, p0.x(), p0.y(), self._zoom)
        b = self._doc.label_to_pdf_point(index, p1.x(), p1.y(), self._zoom)
        new_xref = self._doc.move_annot(index, self._sel_xref, b.x - a.x, b.y - a.y)
        if new_xref is not None:
            self._sel_xref = new_xref
        self._after_annot(index)

    def resize_selection(self, index: int, box_label) -> None:
        if self._sel_xref is None or not self._doc:
            return
        x0, y0, x1, y1 = box_label
        new_rect = self._doc.label_to_pdf_rect(index, x0, y0, x1, y1, self._zoom)
        new_xref = self._doc.resize_annot(index, self._sel_xref, new_rect)
        if new_xref is not None:
            self._sel_xref = new_xref
        self._after_annot(index)

    def recolor_selection(self, qcolor: QColor) -> None:
        if self._sel_xref is None or not self._doc:
            return
        col = (qcolor.redF(), qcolor.greenF(), qcolor.blueF())
        new_xref = self._doc.recolor_annot(self._sel_index, self._sel_xref, col)
        if new_xref is not None:
            self._sel_xref = new_xref
        self._after_annot(self._sel_index)

    def delete_selection(self) -> None:
        if self._sel_xref is None or not self._doc:
            return
        index = self._sel_index
        xref = self._sel_xref
        self.clear_selection()
        self._doc.delete_annot_xref(index, xref)
        self._after_annot(index)

    def edit_selection_text(self) -> None:
        if self._sel_xref is None or not self._doc:
            return
        index = self._sel_index
        if not self._doc.annot_is_textual(index, self._sel_xref):
            return
        from PySide6.QtWidgets import QInputDialog
        current = self._doc.annot_text(index, self._sel_xref)
        text, ok = QInputDialog.getMultiLineText(self, "注釈の編集", "本文:", current)
        if not ok:
            return
        new_xref = self._doc.set_annot_text(index, self._sel_xref, text)
        if new_xref is not None:
            self._sel_xref = new_xref
        self._after_annot(index)

    def set_page(self, index: int) -> None:
        """指定ページの先頭までスクロールする。"""
        if not self._doc or not self._doc.is_open:
            return
        index = max(0, min(index, self._doc.page_count - 1))
        label = self._labels[index]
        self._suspend_scroll = True
        self.verticalScrollBar().setValue(label.y() - PAGE_SPACING)
        self._suspend_scroll = False
        self._set_index(index)
        self._render_visible()

    def set_zoom(self, zoom: float) -> None:
        zoom = max(0.1, min(zoom, 8.0))
        if not self._doc or not self._doc.is_open:
            self._zoom = zoom
            return
        anchor = self._index  # ズーム後も同じページを画面に保つ
        self._zoom = zoom
        self._resize_all()
        self.set_page(anchor)
        if self._form_mode:
            self._build_form_overlays()  # ズーム変更で位置がずれるため作り直す
        self.zoom_changed.emit(self._zoom)

    def fit_width(self) -> None:
        if not self._doc or not self._doc.is_open:
            return
        w, _ = self._doc.page_pixel_size(self._index, zoom=1.0)
        if w:
            # 見開き時は横に self._cols ページ＋ページ間余白が並ぶ
            margin = PAGE_SPACING * (self._cols + 2) + 8
            avail = self.viewport().width() - margin
            self.set_zoom(avail / (w * self._cols))

    WHEEL_ZOOM_STEP = 1.1  # Ctrl+ホイール1ノッチあたりの倍率変化

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            notches = event.angleDelta().y() / 120.0
            if notches:
                factor = self.WHEEL_ZOOM_STEP ** notches
                self.set_zoom(self._zoom * factor)
            event.accept()
            return
        super().wheelEvent(event)

    def refresh_page(self, index: int) -> None:
        """1 ページを再描画（回転後などサイズが変わる場合に対応）。"""
        if not self._doc or not (0 <= index < len(self._labels)):
            return
        self._apply_placeholder_size(index)
        self._rendered.discard(index)
        self._render_visible()

    def render(self) -> None:
        """表示中ページ周辺を再描画する。"""
        self._render_visible()

    # --- フォーム入力（入力欄付きPDF）----------------------------------
    def form_mode(self) -> bool:
        return self._form_mode

    def set_form_mode(self, on: bool) -> None:
        """入力欄の上に編集用コントロールを重ねる/外す。"""
        self._form_mode = on
        if on:
            self._build_form_overlays()
        else:
            self._clear_form_overlays()
            # 入力済みの値を反映するため再描画する
            self._rendered.clear()
            self._render_visible()

    def _clear_form_overlays(self) -> None:
        for w in self._form_overlays:
            w.setParent(None)
            w.deleteLater()
        self._form_overlays.clear()

    def _build_form_overlays(self) -> None:
        self._clear_form_overlays()
        if not self._form_mode or not self._doc or not self._doc.is_open:
            return
        for index in range(min(self._doc.page_count, len(self._labels))):
            label = self._labels[index]
            try:
                fields = self._doc.page_fields(index)
            except Exception:  # noqa: BLE001
                fields = []
            for field in fields:
                # 画面に出ない欄（PDF の Hidden=1 / 印刷のみ=3）は編集オーバー
                # レイを作らない。官公庁フォームの相棒 _PRINT 欄（印刷のみ）や
                # 選択ペアの隠れている側がこれに当たり、重なりや誤入力を防ぐ。
                # ただしコンボ/リストは「選択できる部品を消さない」ため常に描く
                # （都道府県コンボのように稀に Hidden 指定でも操作が要る欄がある）。
                if (int(field.get("display", 0)) in (1, 3)
                        and field.get("kind") not in ("combo", "list")):
                    continue
                ctl = self._make_form_control(index, field, label)
                if ctl is not None:
                    self._form_overlays.append(ctl)

    def _make_form_control(self, index, field, label):
        x0, y0, x1, y1 = self._doc.pdf_rect_to_label(index, field["rect"], self._zoom)
        # 切り捨てだとマス目が最大1px ずれるため四捨五入で合わせる
        w = max(round(x1 - x0), 14)
        h = max(round(y1 - y0), 14)
        xref = field["xref"]
        kind = field["kind"]
        value = field["value"]
        if kind == "radio" and self._is_govform_click_field(field):
            # 官公庁フォームの「丸で選択」JS トグル。対象外（非対応文書や
            # 想定外のスクリプト形）の場合は下の通常の text 分岐にフォール
            # バックする（file 冒頭の分岐コメント参照）。
            ctl = _GovFormClickOverlay(label)
            ctl.clicked.connect(
                lambda checked=False, i=index, x=xref: self._click_govform(i, x))
        elif kind == "checkbox":
            ctl = QCheckBox(label)
            checked = str(value) not in ("", "Off", "/Off", "No", "false", "False", "0")
            ctl.setChecked(checked)
            ctl.toggled.connect(
                lambda on, i=index, x=xref: self._commit_field(i, x, on))
        elif kind in ("combo", "list"):
            ctl = QComboBox(label)
            ctl.setEditable(kind == "combo")
            ctl.addItems(field["choices"])
            if value and value not in field["choices"]:
                ctl.addItem(str(value))
            ctl.setCurrentText(str(value))
            ctl.currentTextChanged.connect(
                lambda text, i=index, x=xref: self._commit_combo(i, x, text))
        elif field.get("multiline"):  # 複数行のテキスト欄
            ctl = _FormTextArea(label)
            ctl.setPlainText(str(value))
            ctl.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            ctl.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            ctl.committed.connect(
                lambda i=index, x=xref, c=ctl: self._commit_field(
                    i, x, c.toPlainText()))
        else:  # text / その他 → テキスト入力（radio は上の govform 分岐かここに来る）
            if field.get("comb") and field.get("maxlen"):
                # コム欄: 1文字1マス。全角/半角が混ざってもマス中央に描画する
                ctl = _CombLineEdit(field["maxlen"], label)
            else:
                ctl = _AutoFitLineEdit(label)
                if field.get("maxlen"):
                    ctl.setMaxLength(field["maxlen"])
            ctl.setText(str(value))
            ctl.editingFinished.connect(
                lambda i=index, x=xref, c=ctl: self._commit_field(i, x, c.text()))
        ctl.setGeometry(round(x0), round(y0), w, h)

        if isinstance(ctl, _GovFormClickOverlay):
            # クリック領域のみ。PDF 側の見た目を透かすため文字サイズ調整や
            # ハイライト用スタイルシートの適用は不要（下の共通処理をスキップ）。
            ctl.show()
            return ctl

        # --- 文字サイズ: PDF 指定サイズ（pt）を尊重。0 は自動（枠に収める） ---
        pdf_fs = float(field.get("fontsize", 0) or 0)
        if pdf_fs > 0:
            base_px = max(8, int(pdf_fs * self._zoom))
        else:
            base_px = max(10, min(int(h * 0.55), 28))
        f = ctl.font()
        f.setPixelSize(base_px)

        if isinstance(ctl, _CombLineEdit):
            # コム欄はマス幅に収まるよう上限をかける（全角の横幅を基準に）
            cell = w / field["maxlen"]
            size = base_px
            while size > 7 and QFontMetrics(f).horizontalAdvance("あ") > cell - 2:
                size -= 1
                f.setPixelSize(size)
            ctl.setFont(f)
        else:
            ctl.setFont(f)
            # Adobe 風の薄青ハイライト。半透明だと下に描画済みの PDF 側の
            # 文字が透けて二重に見えるため不透明にする
            ctl.setStyleSheet(
                "QLineEdit, QComboBox, QPlainTextEdit {"
                " background: #e1ebff;"
                " border: 1px solid #7096d8; padding: 0px 2px; color: #111; }"
                "QCheckBox { background: #e1ebff; }")
            # 自動サイズの1行欄: 入力に応じて縮小し、枠からはみ出さない（Adobe同等）。
            # IME変換中（composing）はフォントを変更しない（変換内容の消失を防ぐ）。
            if pdf_fs == 0 and isinstance(ctl, _AutoFitLineEdit):
                def _fit_font(text, c=ctl, base=base_px):
                    if c.composing:
                        return
                    fnt = c.font()
                    size = base
                    avail = max(10, c.width() - 8)
                    while size > 7:
                        fnt.setPixelSize(size)
                        if QFontMetrics(fnt).horizontalAdvance(text) <= avail:
                            break
                        size -= 1
                    if fnt.pixelSize() != c.font().pixelSize():
                        c.setFont(fnt)
                ctl.textChanged.connect(_fit_font)
                _fit_font(ctl.text())
        ctl.show()
        return ctl

    def _commit_field(self, index, xref, value) -> None:
        if self._doc and self._doc.set_field_value(index, xref, value):
            self.form_changed.emit()

    def _commit_combo(self, index, xref, text) -> None:
        """コンボ選択: 値を確定し、官公庁フォームなら JS 連動（_PRINT 転写・
        枝ラベル表示切替）も再現してからページを再描画する。"""
        if not self._doc:
            return
        committed = self._doc.set_field_value(index, xref, text)
        js_changed = False
        if self._doc.is_govform_js_eligible():
            js_changed = self._doc.commit_govform_combo(index, xref, text)
        if committed or js_changed:
            self.form_changed.emit()
        if js_changed:
            # 枝ラベルの表示切替や _PRINT の値が変わり得るので描画を作り直す
            self._build_form_overlays()
            self._after_annot(index)

    def _is_govform_click_field(self, field) -> bool:
        """kind=="radio" の欄が、官公庁フォームJSトグルの対象として妥当そうか。

        文書自体が対応ヘルパーを定義している（is_govform_js_eligible）ことに
        加え、「単純な identifier(event) 呼び出し」という形だけを見る
        （実行可否の最終判断は govform_js 側。ここでは overlay を出すか
        どうかの UI 判定）。
        """
        if not self._doc or not self._doc.is_govform_js_eligible():
            return False
        script = (field.get("script") or "").strip()
        return bool(script) and bool(SIMPLE_CALL_RE.match(script))

    def _click_govform(self, index: int, xref: int) -> None:
        """官公庁フォームの選択ウィジェットをクリックしたときの処理。

        表示状態（Hidden フラグ）が変わり、同じページの他ウィジェット
        （ペア相手など）も見え方が変わり得るため、フォームオーバーレイ
        一式を作り直す（_after_annot 等と同じ「変更後に作り直す」方針）。
        """
        if not self._doc:
            return
        if not self._doc.is_govform_js_eligible():
            return
        if self._doc.click_govform_widget(index, xref):
            self.form_changed.emit()
            # 同じページ内の別ウィジェット（ペア相手や他の選択肢）も表示が
            # 変わり得るため、オーバーレイ一式を作り直す。
            self._build_form_overlays()
            self._after_annot(index)

    # --- 構築・レイアウト ----------------------------------------------
    def _rebuild(self, fit: bool, target: int) -> None:
        # 既存ラベルを破棄
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._labels.clear()
        self._rendered.clear()
        self._clear_form_overlays()  # 旧ラベルと一緒に消えるので参照も掃除

        if not self._doc or not self._doc.is_open:
            self._relayout_container()
            return

        for i in range(self._doc.page_count):
            label = _PageLabel(i, self)
            self._labels.append(label)
            self._layout.addWidget(label, i // self._cols, i % self._cols)
        self._index = 0
        self._resize_all()
        if fit:
            self.fit_width()
        self.set_page(target)
        if self._form_mode:
            self._build_form_overlays()

    def set_facing(self, on: bool) -> None:
        """見開き表示(2列)のオン/オフ。"""
        cols = 2 if on else 1
        if cols == self._cols:
            return
        self._cols = cols
        if self._doc and self._doc.is_open:
            self.reload(self._index)

    def _resize_all(self) -> None:
        for i in range(len(self._labels)):
            self._apply_placeholder_size(i)
        self._rendered.clear()
        self._relayout_container()

    def _relayout_container(self) -> None:
        """レイアウトを確定させる。

        コンテナ寸法はレイアウトの SetFixedSize 制約が自動で追従させるため、
        ここでは activate でジオメトリを即時確定するだけでよい。
        """
        self._layout.activate()

    def _apply_placeholder_size(self, index: int) -> None:
        w, h = self._doc.page_pixel_size(index, self._zoom)
        self._labels[index].setFixedSize(w, h)

    # --- 中クリック オートスクロール -----------------------------------
    AUTO_DEADZONE = 18  # この距離以内は動かさない(px)
    AUTO_DIVISOR = 8     # 大きいほどゆっくり

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt 命名)
        if obj is self.viewport():
            et = event.type()
            # 押下・ダブルクリックの両方を拾う（連続中クリックの取りこぼし対策）
            if et in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
                if self._auto_active:
                    # オートスクロール中はどのボタンでも停止（ブラウザと同様）
                    self._stop_autoscroll()
                    return True
                if event.button() == Qt.MouseButton.MiddleButton:
                    self._start_autoscroll(event.position().toPoint())
                    return True
        return super().eventFilter(obj, event)

    def _auto_marker_widget(self) -> QLabel:
        """オートスクロールの起点マーカー（丸に上下矢印）。有効中の目印。"""
        if getattr(self, "_auto_marker", None) is not None:
            return self._auto_marker
        size = 34
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(255, 255, 255, 235))
        p.setPen(QPen(QColor(80, 80, 80), 2))
        p.drawEllipse(QRect(2, 2, size - 4, size - 4))
        c = size // 2
        p.setBrush(QColor(60, 60, 60))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(QPolygon([QPoint(c, 6), QPoint(c - 5, 13), QPoint(c + 5, 13)]))
        p.drawPolygon(QPolygon([QPoint(c, size - 6), QPoint(c - 5, size - 13),
                                QPoint(c + 5, size - 13)]))
        # 中央の点
        p.setBrush(QColor(60, 60, 60))
        p.drawEllipse(QRect(c - 2, c - 2, 4, 4))
        p.end()
        lbl = QLabel(self.viewport())
        lbl.setPixmap(pm)
        lbl.setFixedSize(size, size)
        lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lbl.hide()
        self._auto_marker = lbl
        return lbl

    def _start_autoscroll(self, pos: QPoint) -> None:
        # pos はビューポート座標。以後の移動量はカーソル位置(QCursor)で判定するので
        # ラベルがビューポートを覆っていても確実に動く。
        self._auto_active = True
        self._auto_origin = pos
        marker = self._auto_marker_widget()
        marker.move(pos.x() - marker.width() // 2, pos.y() - marker.height() // 2)
        marker.show()
        marker.raise_()
        self.viewport().setCursor(Qt.CursorShape.SizeVerCursor)
        self._auto_timer.start()

    def _stop_autoscroll(self) -> None:
        self._auto_active = False
        self._auto_timer.stop()
        if getattr(self, "_auto_marker", None) is not None:
            self._auto_marker.hide()
        self.viewport().unsetCursor()

    def _auto_scroll_tick(self) -> None:
        # 現在のカーソル位置（ビューポート基準）と開始点の差でスクロール速度を決める
        pos = self.viewport().mapFromGlobal(QCursor.pos())
        dy = pos.y() - self._auto_origin.y()
        if abs(dy) <= self.AUTO_DEADZONE:
            return
        # デッドゾーンを越えた分だけ、距離に比例した速度でスクロール
        offset = dy - self.AUTO_DEADZONE if dy > 0 else dy + self.AUTO_DEADZONE
        step = int(offset / self.AUTO_DIVISOR)
        if step:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() + step)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.matches(QKeySequence.StandardKey.Copy) and self.has_text_selection():
            self.copy_selection()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.has_selection():
            self.delete_selection()
            return
        key = event.key()
        # ← → でページ送り、↑ ↓ でスクロール
        if key == Qt.Key.Key_Right:
            self.set_page(self._index + 1)
            return
        if key == Qt.Key.Key_Left:
            self.set_page(self._index - 1)
            return
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            bar = self.verticalScrollBar()
            step = max(bar.singleStep(), 60)
            bar.setValue(bar.value() + (step if key == Qt.Key.Key_Down else -step))
            return
        # Home/End で先頭/最終ページ、Space で1画面送り（Shift+Space で戻り）
        if key == Qt.Key.Key_Home:
            self.set_page(0)
            return
        if key == Qt.Key.Key_End and self._doc and self._doc.is_open:
            self.set_page(self._doc.page_count - 1)
            return
        if key == Qt.Key.Key_Space:
            bar = self.verticalScrollBar()
            step = self.viewport().height() - 40
            back = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            bar.setValue(bar.value() + (-step if back else step))
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        self._render_visible()

    # --- 遅延描画 -------------------------------------------------------
    def _on_scroll(self, _value: int) -> None:
        if self._suspend_scroll:
            return
        self._render_visible()
        self._update_current_page()

    def _visible_range(self) -> tuple[int, int]:
        """描画対象とするビューポート上下端(コンテナ座標)。前後に1画面分の余白。"""
        top = self.verticalScrollBar().value()
        h = self.viewport().height()
        return (top - h, top + 2 * h)

    def _render_visible(self) -> None:
        if not self._doc or not self._doc.is_open:
            return
        lo, hi = self._visible_range()
        page_count = self._doc.page_count
        for i, label in enumerate(self._labels):
            if i >= page_count:
                # リロード中の保留イベントなどでラベルが一時的に多い場合の保険
                continue
            y0 = label.y()
            y1 = y0 + label.height()
            visible = y1 >= lo and y0 <= hi
            if visible and i not in self._rendered:
                pixmap = self._doc.render_page(i, zoom=self._zoom)
                label.setPixmap(pixmap)
                self._rendered.add(i)
            elif not visible and i in self._rendered:
                # 画面外はメモリ解放（プレースホルダに戻す）
                label.clear()
                self._rendered.discard(i)

    def _update_current_page(self) -> None:
        """ビューポート中央にあるページを現在ページとして扱う。"""
        if not self._labels:
            return
        center = self.verticalScrollBar().value() + self.viewport().height() // 2
        for i, label in enumerate(self._labels):
            if label.y() <= center < label.y() + label.height() + PAGE_SPACING:
                self._set_index(i)
                return

    def _set_index(self, index: int) -> None:
        if index != self._index:
            self._index = index
            self.page_changed.emit(index)
