"""連続スクロール対応のページ表示ウィジェット。

全ページを縦に並べて表示し、ビューポートに見えている（近い）ページだけを
遅延描画する。遠いページの QPixmap は解放してメモリ使用を抑える。
"""
from __future__ import annotations

from PySide6.QtCore import (
    QElapsedTimer, QEvent, QPoint, QRect, Qt, QTimer, Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QPolygon,
    QTextBlockFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QWidget,
)

from . import theme
from .document import _UNSET, FT_LINE, PdfDocument, ft_needed_size
from .govform_js import SIMPLE_CALL_RE

PAGE_SPACING = 16  # ページ間の余白(px)。落ち影が乗るので少し広めに取る
GESTURE_MIN_PX = 40  # これ以上動かしたらマウスジェスチャーとみなす


class _GovFormClickOverlay(QPushButton):
    """官公庁フォームの「丸で選択」JS トグル用のクリック領域。

    「ここはクリックで選べる欄」だと分かるよう、ごく薄い塗り＋点線枠を常時
    表示する（Adobe の「フィールドを強調表示」に相当）。PDF 側の見た目
    （選択中の〇/非選択）は薄塗り越しに透けて見え、ホバーで少し濃くなる。

    マウス操作の注意点:
    - フォーカスを取らない（NoFocus）。取ると QScrollArea が「フォーカス欄を
      見せよう」として、選択のたびに画面が勝手にスクロールしてしまう。
    - 左クリックのみ選択に使う。中/右ボタンやオートスクロール中は親（ページ）
      へ流し、欄の上でも中クリックのスクロール開始/停止が効くようにする。
    - ホイールは飲み込まず下のスクロール領域へ流す。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFlat(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet(
            "QPushButton { background: rgba(112, 150, 216, 45);"
            " border: 1px dashed rgba(112, 150, 216, 170); }"
            "QPushButton:hover { background: rgba(112, 150, 216, 95); }")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("クリックして選択")

    def _autoscrolling(self) -> bool:
        """親ページ経由で、オートスクロール（中クリック）中かを見る。"""
        view = getattr(self.parent(), "_view", None)
        return bool(view is not None and getattr(view, "_auto_active", False))

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        event.ignore()  # 欄の上でもホイールでページをスクロールできるように

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if (event.button() != Qt.MouseButton.LeftButton
                or self._autoscrolling()):
            event.ignore()   # 親へ流す（中クリック/オートスクロールを優先）
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if (event.button() != Qt.MouseButton.LeftButton
                or self._autoscrolling()):
            event.ignore()
            return
        super().mouseReleaseEvent(event)


class _FormTextArea(QPlainTextEdit):
    """複数行フォーム欄。フォーカスが外れたら committed を発火する。

    Tab は次の欄へ移る（Acrobat と同じ。以前はタブ文字が入って抜け出せなかった）。
    欄の文字数の上限（/MaxLen）を守る。
    """

    committed = Signal()

    def __init__(self, parent=None, maxlen: int = 0) -> None:
        super().__init__(parent)
        self.setTabChangesFocus(True)
        self._maxlen = int(maxlen or 0)
        if self._maxlen:
            self.textChanged.connect(self._schedule_limit)

    def _schedule_limit(self) -> None:
        # 入力の処理中に文字を消さない（IME の確定を壊さないよう次の周回で）
        QTimer.singleShot(0, self._apply_limit)

    def _apply_limit(self) -> None:
        try:
            text = self.toPlainText()
        except RuntimeError:
            return   # 欄が破棄済み
        if len(text) <= self._maxlen:
            return
        cur = self.textCursor()
        cur.setPosition(self._maxlen)
        cur.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()

    def focusOutEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().focusOutEvent(event)
        self.committed.emit()

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        # 欄の上でホイールしたらページをスクロール（自身の内容ではなく）。
        event.ignore()


class _FormComboBox(QComboBox):
    """フォーム用コンボ。閉じている状態でホイールしても値が変わらないよう、
    ホイールは飲み込まず下のスクロール領域へ流す。

    QComboBox は既定だとホイールで選択値が回ってしまい、ページをスクロール中に
    カーソルがコンボに乗るだけで値が勝手に書き換わる（＝誤入力）。ドロップダウンを
    開いている間はポップアップ側が別ウィジェットとしてホイールを処理するので、
    ここで無視しても一覧のスクロールには影響しない。

    また、QComboBox にスタイルシートを当てると Qt の仕様で標準の▽矢印が
    消えてしまい、テキスト入力欄と見分けがつかない。そこで▽を自前で描いて
    「選べる欄（ドロップダウン）」だと分かるようにする。
    """

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        event.ignore()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx = self.width() - 7
        cy = self.height() // 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0x2a, 0x4a, 0x8a))
        p.drawPolygon(QPolygon([QPoint(cx - 4, cy - 2),
                                QPoint(cx + 4, cy - 2),
                                QPoint(cx, cy + 3)]))
        p.end()


class _TextAnnotEditor(QTextEdit):
    """テキスト注釈を「その場で」入力する編集ボックス。

    見たまま入ること（確定しても文字が跳ねない・切れない）を優先している。
    - 余白 0・行送り 1.2 倍で、MuPDF が描く FreeText と同じ位置に文字が並ぶ
      （以前は余白と枠で 1 文字近くずれ、確定した瞬間に左上へ跳ねていた）
    - 打った分だけ広がる。クリックで置いた欄は横へ（ページの右端で折り返し）、
      ドラッグで幅を決めた欄は下へ。枠の外に隠れて見えない文字が出ない
    - 見えている折り返し位置を、そのまま確定後の改行にする（wrapped_lines）
    - 背景は透明（新規）か、注釈を除いた紙面（既存の編集）。下の罫線や記入欄に
      合わせて打てる
    確定＝ページの別の場所をクリック / Esc / Ctrl+Enter。空のまま離れたら取消。
    ツールバー（サイズ・色）を触ったときは確定させず編集を続けられる。
    """

    committed = Signal()
    cancelled = Signal()

    #: 1 行目のベースラインの位置（枠の上端から、文字サイズの何倍下か）。
    #: MuPDF は経路によって 0.8 と 0.9 を使い分けるので、その間に置く。
    BASELINE = 0.85
    CARET_ROOM = 3   # 行末にカーソルが見える分の余白(px)

    def __init__(self, parent, auto_width: bool) -> None:
        super().__init__(parent)
        self.auto_width = auto_width   # True=クリックで置いた欄（横に伸びる）
        self.setAcceptRichText(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.FixedPixelWidth)
        self.document().setDocumentMargin(0)
        self._done = False
        self._px = 14.0
        self._origin = (0.0, 0.0)   # 1 行目の枠の左上（ラベル座標）
        self._wrap_w = 200
        self._min_w = 0
        self._min_h = 0
        self._shift = 0.0           # 1 行目のベースラインを合わせるための上下補正
        self._backdrop: QPixmap | None = None
        self.document().contentsChanged.connect(self._schedule_fit)

    # --- 見た目と大きさ ---------------------------------------------------
    def configure(self, font, color: QColor, px: float, origin: tuple,
                  wrap_w: float, min_w: float = 0, min_h: float = 0) -> None:
        """文字・色・位置・折り返し幅を決める。IME の変換中には呼ばないこと
        （setFont が変換を取り消す。[[project-trivreader-ime-defer]] と同じ罠）。"""
        self._px = px
        self._origin = origin
        self._wrap_w = max(8, int(round(wrap_w)))
        self._min_w = int(round(min_w))
        self._min_h = int(round(min_h))
        self.setFont(font)
        self.document().setDefaultFont(font)
        # アプリ全体の QSS が入力欄に余白と角丸を付けているので、ここで 0 に
        # 戻す（残すと文字が枠の内側へ 9px ほどずれ、確定で跳ねる）
        self.setStyleSheet(
            "QTextEdit { background: transparent; border: 1px dashed #1a73e8;"
            " border-radius: 0px; padding: 0px; margin: 0px;"
            " color: rgb(%d,%d,%d); selection-background-color: rgba(26,115,232,90);"
            " selection-color: rgb(%d,%d,%d); }"
            % (color.red(), color.green(), color.blue(),
               color.red(), color.green(), color.blue()))
        self.setLineWrapColumnOrWidth(self._wrap_w)
        self._apply_line_height()
        self._shift = self.BASELINE * px - self._first_baseline()
        self._fit()

    def set_backdrop(self, pixmap: QPixmap | None) -> None:
        """編集中の下地（注釈を除いたページの絵。ラベルと同じ大きさ）。"""
        self._backdrop = pixmap
        self.viewport().update()

    def _apply_line_height(self) -> None:
        cur = QTextCursor(self.document())
        cur.select(QTextCursor.SelectionType.Document)
        fmt = QTextBlockFormat()
        fmt.setLineHeight(FT_LINE * self._px, 2)   # 2 = FixedHeight（行送り固定）
        cur.mergeBlockFormat(fmt)

    def _first_baseline(self) -> float:
        doc = self.document()
        doc.documentLayout().documentSize()   # 割り付けを確定させる
        blk = doc.firstBlock()
        lay = blk.layout()
        if lay is None or lay.lineCount() == 0:
            return self.BASELINE * self._px
        line = lay.lineAt(0)
        top = doc.documentLayout().blockBoundingRect(blk).y()
        return top + line.y() + line.ascent()

    def _schedule_fit(self) -> None:
        # 入力の処理中に大きさを変えない（IME の確定を壊さないよう次の周回で）
        QTimer.singleShot(0, self._fit_alive)

    def _fit_alive(self) -> None:
        try:
            self._fit()
        except RuntimeError:
            pass   # 発火までに欄が破棄済み

    def _fit(self) -> None:
        """中身に合わせて欄を広げる（狭めるのは最初の大きさまで）。"""
        doc = self.document()
        size = doc.documentLayout().documentSize()
        if self.auto_width:
            w = min(max(doc.idealWidth(), self._min_w), self._wrap_w)
        else:
            w = max(self._wrap_w, self._min_w)
        h = max(size.height(), self._min_h, FT_LINE * self._px)
        x, y = self._origin
        self.setGeometry(round(x) - 1, round(y + self._shift) - 1,
                         int(w) + self.CARET_ROOM + 2, int(h + 0.999) + 2)
        self.viewport().update()

    def text_area_origin(self) -> QPoint:
        """文字の左上（ラベル座標）。テスト・位置合わせ用。"""
        return self.geometry().topLeft() + QPoint(1, 1)

    def wrapped_lines(self) -> list[str]:
        """画面で見えている行（折り返しを改行に置き換えたもの）。"""
        out: list[str] = []
        blk = self.document().begin()
        while blk.isValid():
            text = blk.text()
            lay = blk.layout()
            n = lay.lineCount() if lay is not None else 0
            if n == 0:
                out.append(text)
            for i in range(n):
                ln = lay.lineAt(i)
                seg = text[ln.textStart(): ln.textStart() + ln.textLength()]
                out.append(seg.rstrip(" ") if i < n - 1 else seg)
            blk = blk.next()
        return out

    def setPlainText(self, text: str) -> None:  # noqa: N802 (Qt 命名)
        super().setPlainText(text)
        self._apply_line_height()   # 差し替えで行送りの指定が消えるため
        self._fit()

    def insertFromMimeData(self, source) -> None:  # noqa: N802 (Qt 命名)
        # 貼り付けは書式を捨てて文字だけ（行送りの指定を保つ）
        if source.hasText():
            self.textCursor().insertText(source.text())
            self._apply_line_height()

    def inputMethodEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().inputMethodEvent(event)
        self._schedule_fit()   # 変換中の文字が欄の外に隠れないように

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if self._backdrop is not None:
            p = QPainter(self.viewport())
            off = self.viewport().mapTo(self.parentWidget(), QPoint(0, 0))
            p.drawPixmap(-off.x(), -off.y(), self._backdrop)
            p.end()
        super().paintEvent(event)

    # --- 確定 / 取消 ------------------------------------------------------
    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            # Acrobat などと同じく Esc は「入力を終える」（消さない）。
            # 以前は打った文字ごと捨てていた。
            self._finish()
            return
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._finish()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        # フォーカスがツールバー（サイズ/色の調整）へ移っただけなら確定しない。
        # イベント確定後に判定するため次のループへ回す。
        QTimer.singleShot(0, self._finish_if_left)

    def _finish_if_left(self) -> None:
        from PySide6.QtWidgets import (QAbstractButton, QAbstractSpinBox,
                                       QComboBox)
        from PySide6.QtWidgets import QApplication as _App
        if self._done or self.hasFocus():
            return
        fw = _App.focusWidget()
        if isinstance(fw, (QAbstractSpinBox, QComboBox, QAbstractButton)):
            return  # ツールバーで設定調整中 → 編集を続ける
        self._finish()

    def _finish(self) -> None:
        # 空でも committed を出す（新規なら何も作らず、既存なら注釈を消す。
        # 判断は受け取る側 PageView._commit_text_edit が持つ）
        if self._done:
            return
        self._done = True
        self.committed.emit()


class _CombLineEdit(QLineEdit):
    """コム欄（1文字1マス）の入力欄。各文字をマスの中央に自前描画する。

    字間（letter-spacing）でのマス目再現は半角基準の均等割りになり、
    全角文字（かな・カナ・漢字）でマスからズレるため、Acrobat と同じく
    文字ごとにマス中央へ配置して描画する。IME の未確定文字（プリエディット）
    もカーソル位置のマスに下線付きで表示する。
    """

    def __init__(self, maxlen: int, parent=None, align: int = 0) -> None:
        super().__init__(parent)
        self._cells = max(1, int(maxlen))
        # 欄の寄せ（/Q）。金額欄は右寄せ（右のマスから詰める）で作られている
        self._align = align
        self._preedit = ""
        self.setMaxLength(self._cells)
        self.setFrame(False)
        # ネイティブ描画は使わない（paintEvent を完全に置き換える）
        self.setStyleSheet("QLineEdit { background: transparent; color: transparent; }")

    def inputMethodEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().inputMethodEvent(event)
        self._preedit = event.preeditString()
        self.update()

    def _first_cell(self, n: int) -> int:
        """n 文字を置くときの最初のマス（右寄せなら右に詰める）。"""
        free = max(0, self._cells - n)
        return {1: free // 2, 2: free}.get(self._align, 0)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        # ネイティブのヒットテストは字間前提でズレるため、マス単位で決める
        cell = self.width() / self._cells
        start = self._first_cell(len(self.text()))
        pos = int(event.position().x() / cell + 0.5) - start if cell > 0 else 0
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
        start = self._first_cell(len(disp))

        # 選択範囲のマスを強調
        if self.hasSelectedText():
            s = self.selectionStart() + start
            e = s + len(self.selectedText())
            p.fillRect(QRect(round(s * cell), 1,
                             round((e - s) * cell), r.height() - 2),
                       QColor(120, 160, 230, 120))

        p.setPen(QColor(0x11, 0x11, 0x11))
        for i, ch in enumerate(disp[:self._cells]):
            w_ch = fm.horizontalAdvance(ch)
            x = (start + i) * cell + (cell - w_ch) / 2
            p.drawText(int(x), int(baseline), ch)
            # プリエディット（未確定文字）には下線
            if self._preedit and cur <= i < cur + len(self._preedit):
                y = min(int(baseline) + 2, r.height() - 3)
                p.drawLine(int(x), y, int(x + w_ch), y)

        # 入力中の欄だと分かるよう、枠を濃く太くする
        if self.hasFocus():
            p.setPen(QPen(QColor(0x1a, 0x73, 0xe8), 2))
            p.drawRect(r.adjusted(1, 1, -1, -1))
        # カーソル（フォーカス時のみ・マスの左端に描く）
        if self.hasFocus() and not self.hasSelectedText():
            cpos = min(start + cur + len(self._preedit), self._cells)
            if cpos < self._cells:
                cx = round(cpos * cell) + 2
            else:
                cx = r.width() - 3
            p.setPen(QPen(QColor(0x11, 0x11, 0x11), 1))
            p.drawLine(cx, 3, cx, r.height() - 4)
        p.end()


class _FormCheckBox(QCheckBox):
    """フォームのチェック欄。欄の四角のどこを押しても切り替わる。

    Qt の QCheckBox は左端の小さな印だけが押せる場所で、PDF の四角の真ん中を
    押しても反応しなかった。チェックの印も四角の中央に描く。
    """

    def hitButton(self, pos) -> bool:  # noqa: N802 (Qt 命名)
        return self.rect().contains(pos)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        p.fillRect(r, QColor(225, 235, 255))
        focus = self.hasFocus()
        p.setPen(QPen(QColor(0x1a, 0x73, 0xe8) if focus else QColor(0x70, 0x96, 0xD8),
                      2 if focus else 1))
        p.drawRect(r.adjusted(0, 0, -1, -1))
        if self.isChecked():
            side = min(r.width(), r.height()) * 0.7
            cx, cy = r.center().x(), r.center().y()
            pen = QPen(QColor(0x11, 0x11, 0x11), max(1.5, side / 7))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            pts = [QPoint(int(cx - side * 0.40), int(cy + side * 0.02)),
                   QPoint(int(cx - side * 0.10), int(cy + side * 0.32)),
                   QPoint(int(cx + side * 0.42), int(cy - side * 0.34))]
            p.drawPolyline(QPolygon(pts))
        p.end()


# 小さいカナ → 大きいカナ（フォームの ReplaceChisaiKana と同じ表）
_SMALL_KANA = str.maketrans("ｧｨｩｪｫｬｭｮｯァィゥェォャュョッ", "ｱｲｳｴｵﾔﾕﾖﾂアイウエオヤユヨツ")


def _clean_input(text: str, rules: dict | None) -> str:
    """フォームの入力補助どおりに直した文字列（数字だけ・小さいカナを大きく）。"""
    import unicodedata

    if not rules:
        return text
    if rules.get("digits"):
        # 全角数字は半角にして受け付ける（Acrobat は弾くだけだが、日本語入力の
        # まま打っても入る方が親切）。数字以外は入れない
        text = "".join(c for c in unicodedata.normalize("NFKC", text) if "0" <= c <= "9")
    if rules.get("large_kana"):
        text = text.translate(_SMALL_KANA)
    return text


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
TOOL_STAMP = "stamp"    # クリックで枠付きテキストスタンプを置く
TOOL_SNAPSHOT = "snapshot"  # 範囲をドラッグして画像としてコピー（注釈は作らない）

# ドラッグ矩形系（始点・終点で矩形を作る）
_RECT_TOOLS = {TOOL_HIGHLIGHT, TOOL_RECT, TOOL_TEXT, TOOL_CIRCLE, TOOL_REDACT, TOOL_CROP,
               TOOL_SNAPSHOT}
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
        # 未描画のあいだも「紙」に見えるようにしておく（灰色の板を出さない）
        self.apply_theme(False)
        self.setCursor(Qt.CursorShape.IBeamCursor)  # 既定(選択ツール)=テキスト選択
        self._drawing = False
        self._start = QPoint()
        self._cur = QPoint()
        self._stroke: list[QPoint] = []  # ペン用
        self._sel_moving = False
        self._move_started = False   # 押してから一定以上動かして、移動が始まった
        self._edit_on_release = False  # テキストツールで既存の文字を押した（離せば編集）
        self._markup_press = False   # ハイライト等の上で押した（動かせば文字選択）
        self._markup_boxes: list = []  # ハイライト等のツールで塗られる文字の枠
        self._move_offset = QPoint()
        self._text_selecting = False
        self._resizing = False
        self._resize_handle = None
        self._box_start = None
        self._cur_box = None
        self._press = QPoint()
        # 3連クリック（行選択）の判定用。Qt はトリプルクリックを通知しない
        # ので、ダブルクリックの直後かどうかを自前で見る。
        self._dclick_at: QPoint | None = None
        self._dclick_timer = QElapsedTimer()
        # マウスジェスチャー（右ボタンを押しながら動かす）
        self._gesture_from: QPoint | None = None
        self._skip_context_menu = False
        # リンク（目次ジャンプ・URL）。押した位置のリンクを控え、動かさずに
        # 離したときだけ辿る（ドラッグはテキスト選択として扱う）。
        self.setMouseTracking(True)   # ボタンを押していなくても move が来る
        self._press_link = None
        self._hover_link = None

    def apply_theme(self, dark: bool) -> None:
        """紙（未描画時のプレースホルダ）の色をテーマに合わせる。"""
        self.setStyleSheet(f"background: {theme.paper_color(dark)}; border: 0;")

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
        if event.button() == Qt.MouseButton.RightButton:
            self._gesture_from = (event.position().toPoint()
                                  if self._view.gesture_enabled() else None)
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position().toPoint()
        if self._view._text_editor is not None:
            # 文字の入力中にページをクリックしたら、まずその入力を確定する。
            # ページはフォーカスを取らないので、ここで閉じないと入力欄が開いた
            # まま残り、次のドラッグで作った欄に打った文字ごと上書きされていた。
            self._view._text_editor._finish()
            event.accept()
            return
        if tool == TOOL_TEXT:
            # テキストツールで既存のテキスト注釈を押したら、新しく作らない。
            # そのまま離せば直す（入力欄を開く）、押したまま動かせば移動する
            # （Acrobat の「テキストを追加」と同じ。選択ツールに持ち替えずに済む）
            xref = self._view.hit_annot(self._index, pos)
            if xref is not None and self._view.is_freetext(self._index, xref):
                self._view.set_selection(self._index, xref)
                self._sel_moving = True
                self._move_started = False
                self._edit_on_release = True
                self._start = pos
                self._press = pos
                self._move_offset = QPoint()
                return
            self._view.clear_selection()
        if tool == TOOL_NONE:
            # 選択中なら、まずリサイズハンドルの上か判定（文字に付いた注釈は
            # 枠で伸縮するものではないのでハンドルを出さない）
            box = self._view.selection_box_label(self._index)
            if box is not None and not self._view.selection_is_markup():
                handle = self._handle_at(pos, box)
                if handle is not None:
                    self._resizing = True
                    self._resize_handle = handle
                    self._box_start = box
                    self._cur_box = box
                    self._press = pos
                    return
            # リンクの上なら、まずリンクとして扱う（離したときに辿る）
            link = self._view.link_at(self._index, pos)
            if link is not None:
                self._view.clear_selection()
                self._press_link = (link, pos)
                self._press = pos
                return
            # 注釈に当たれば選択して移動、外れたらテキスト選択を開始
            xref = self._view.hit_annot(self._index, pos)
            if xref is not None:
                self._view.set_selection(self._index, xref)
                self._press = pos
                if self._view.selection_is_markup():
                    # ハイライト・下線などは文字から動かすものではない。押した
                    # まま動かしたら、その上の文字を選ぶ（以前は掴んで文字から
                    # ずらしてしまい、ハイライトした所の文字が選べなかった）
                    self._markup_press = True
                else:
                    self._sel_moving = True
                    self._move_started = False
                    self._start = pos
                    self._move_offset = QPoint()
            else:
                self._view.clear_selection()
                # ダブルクリック直後の3回目なら行をまるごと選ぶ
                if self._is_triple_click(pos):
                    self._dclick_at = None
                    self._view.select_line_at(self._index, pos)
                    return
                self._text_selecting = True
                self._start = pos
                self._view.clear_text_selection()
                self._view.begin_text_selection()
            return
        if tool in (TOOL_NOTE, TOOL_ERASE, TOOL_STAMP):
            self._view.commit_point(self._index, tool, pos)
            return
        self._drawing = True
        self._start = pos
        self._cur = pos
        self._stroke = [pos]
        self._markup_boxes = []

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtWidgets import QApplication

        pos = event.position().toPoint()
        if self._markup_press:
            # ハイライト等の上で押して動かした → 注釈ではなく下の文字を選ぶ
            if (pos - self._press).manhattanLength() < QApplication.startDragDistance():
                return
            self._markup_press = False
            self._view.clear_selection()
            self._text_selecting = True
            self._start = self._press
            self._view.clear_text_selection()
            self._view.begin_text_selection()
        if self._press_link is not None:
            # リンクの上で押したまま動かした → リンクではなくテキスト選択
            if (pos - self._press).manhattanLength() > 4:
                self._press_link = None
                self._text_selecting = True
                self._start = self._press
                self._view.clear_text_selection()
                self._view.begin_text_selection()
            else:
                return
        if (self._view.tool == TOOL_NONE and not self._text_selecting
                and not self._resizing and not self._sel_moving
                and not self._drawing):
            self._update_link_hover(pos)
        if self._text_selecting:
            self._view.update_text_selection_to(
                self._index, self._start, event.globalPosition().toPoint()
            )
            return
        if self._resizing:
            d = event.position().toPoint() - self._press
            self._cur_box = _resize_box(self._box_start, self._resize_handle, d.x(), d.y())
            self.update()
            return
        if self._sel_moving:
            d = pos - self._start
            # 選ぶつもりのクリックで手が少し動いただけで注釈がずれないよう、
            # OS のドラッグ開始距離を超えてから動かし始める
            if (not self._move_started
                    and d.manhattanLength() < QApplication.startDragDistance()):
                return
            self._move_started = True
            self._move_offset = d
            self.update()
            return
        if not self._drawing:
            return
        self._cur = event.position().toPoint()
        tool = self._view.tool
        if tool == TOOL_PEN:
            self._stroke.append(self._cur)
        elif tool in (TOOL_HIGHLIGHT, TOOL_UNDERLINE, TOOL_STRIKEOUT):
            # 実際に付く範囲（なぞった文字）をその場で見せる
            self._markup_boxes = self._view.text_boxes_between(
                self._index, self._start, self._cur)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            if self._finish_gesture(event.position().toPoint()):
                event.accept()
                return
        if self._press_link is not None:
            link, at = self._press_link
            self._press_link = None
            if (event.button() == Qt.MouseButton.LeftButton
                    and (event.position().toPoint() - at).manhattanLength() <= 4):
                self._view.link_activated.emit(dict(link))
            return
        if self._markup_press:   # ハイライト等をクリックしただけ（選択のみ）
            self._markup_press = False
            return
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
            edit, self._edit_on_release = self._edit_on_release, False
            if self._move_started:
                self._view.move_selection(self._index, self._start, end)
            elif edit:   # テキストツールでクリックしただけ → その場で直す
                xref = self._view.current_annot_ref()
                if xref is not None:
                    self._view.edit_text_annot(*xref)
            self._move_started = False
            self._move_offset = QPoint()
            self.update()
            return
        if not self._drawing:
            return
        self._drawing = False
        tool = self._view.tool
        self._cur = event.position().toPoint()
        if tool == TOOL_PEN:
            self._view.commit_stroke(self._index, list(self._stroke))
        else:
            self._view.commit_drag(self._index, self._start, self._cur, tool)
        self._stroke = []
        self._markup_boxes = []
        self.update()

    def _update_link_hover(self, pos: QPoint) -> None:
        """リンクの上ならカーソルを手にして、行き先をステータスバーへ出す。"""
        link = self._view.link_at(self._index, pos)
        if link is self._hover_link or (
                link is not None and self._hover_link is not None
                and link["rect"] == self._hover_link["rect"]):
            return
        self._hover_link = link
        if link is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self._view.link_hovered.emit(link["label"])
        else:
            self.setCursor(Qt.CursorShape.IBeamCursor)
            self._view.link_hovered.emit("")

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover_link is not None:
            self._hover_link = None
            if self._view.tool == TOOL_NONE:
                self.setCursor(Qt.CursorShape.IBeamCursor)
            self._view.link_hovered.emit("")
        super().leaveEvent(event)

    def _finish_gesture(self, pos: QPoint) -> bool:
        """右ドラッグの向きからジェスチャーを決める。動かした量が少なければ
        ジェスチャーとせず、通常の右クリック（メニュー）に任せる。"""
        start, self._gesture_from = self._gesture_from, None
        if start is None:
            return False
        dx, dy = pos.x() - start.x(), pos.y() - start.y()
        if max(abs(dx), abs(dy)) < GESTURE_MIN_PX:
            return False
        if abs(dx) > abs(dy):
            direction = "right" if dx > 0 else "left"
        else:
            direction = "down" if dy > 0 else "up"
        self._skip_context_menu = True
        self._view.gesture.emit(direction)
        return True

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        if self._skip_context_menu:   # ジェスチャーの後にメニューを出さない
            self._skip_context_menu = False
            event.accept()
            return
        if self._view.tool != TOOL_NONE:   # 注釈ツール使用中は邪魔しない
            super().contextMenuEvent(event)
            return

        view = self._view
        menu = QMenu(self)
        run = {}          # QAction → 実行する処理

        def add(text: str, func) -> None:
            run[menu.addAction(text)] = func

        if view.has_text_selection():
            add("コピー", view.copy_selection)
            menu.addSeparator()
            add("ハイライト", lambda: view.markup_selection("highlight"))
            add("下線", lambda: view.markup_selection("underline"))
            add("取消線", lambda: view.markup_selection("strikeout"))
            menu.addSeparator()
            snippet = " ".join(view.selection_text().split())[:18]
            if snippet:
                add(f"「{snippet}」を検索",
                    lambda: view.search_requested.emit(view.selection_text()))
                menu.addSeparator()

        link = view.link_at(self._index, event.pos())
        if link is not None:
            if link["kind"] == "uri":
                add("リンクを開く", lambda: view.link_activated.emit(dict(link)))
                add("リンク先アドレスをコピー",
                    lambda: view.copy_text_to_clipboard(link["uri"]))
            elif link["kind"] == "goto":
                add(link["label"], lambda: view.link_activated.emit(dict(link)))
            menu.addSeparator()

        add("すべて選択", view.select_all_text)
        add("このページを画像としてコピー",
            lambda: view.copy_page_image(self._index))
        menu.addSeparator()
        add("幅に合わせる", view.fit_width)
        add("縦に合わせる", view.fit_height)
        menu.addSeparator()
        add("左に回転", lambda: view.rotate_requested.emit(-90))
        add("右に回転", lambda: view.rotate_requested.emit(90))

        chosen = menu.exec(event.globalPos())
        func = run.get(chosen)
        if func is not None:
            func()

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
            if xref is not None and not self._view.selection_is_markup():
                self._view.set_selection(self._index, xref)
                self._view.edit_selection_text()
                return
            if xref is not None:
                self._view.clear_selection()   # ハイライトの上なら下の語を選ぶ
            # 注釈でなければ語を選ぶ（もう1回クリックすれば行選択）
            self._dclick_at = pos
            self._dclick_timer.restart()
            if self._view.select_word_at(self._index, pos):
                event.accept()
                return
        if self._view.tool == TOOL_TEXT and event.button() == Qt.MouseButton.LeftButton:
            # 既定の処理は 2 回目の押下として扱い、開いたばかりの入力欄を閉じてしまう
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _is_triple_click(self, pos: QPoint) -> bool:
        """ダブルクリックのすぐ後の、ほぼ同じ場所へのクリックか。"""
        from PySide6.QtWidgets import QApplication

        if self._dclick_at is None or not self._dclick_timer.isValid():
            return False
        if self._dclick_timer.elapsed() > QApplication.doubleClickInterval():
            return False
        d = pos - self._dclick_at
        return abs(d.x()) <= 4 and abs(d.y()) <= 4

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
            if self._view._text_editor is not None:
                return   # 入力中は入力欄の枠だけ見せる（選択枠と重ねない）
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
            # リサイズハンドル（白地に青枠の四角）。文字に付いた注釈には出さない
            if not self._view.selection_is_markup():
                sp.setPen(QPen(QColor(26, 115, 232), 1, Qt.PenStyle.SolidLine))
                for hx, hy in _handle_centers(box).values():
                    hr = QRect(int(hx - HANDLE_DRAW / 2), int(hy - HANDLE_DRAW / 2),
                               HANDLE_DRAW, HANDLE_DRAW)
                    sp.fillRect(hr, QColor(255, 255, 255))
                    sp.drawRect(hr)
            sp.end()
            return
        painter = QPainter(self)
        tool = self._view.tool
        color = self._view.tool_color(tool)
        rect = QRect(self._start, self._cur).normalized()
        qt_dash = {"solid": Qt.PenStyle.SolidLine, "dashed": Qt.PenStyle.DashLine,
                   "dotted": Qt.PenStyle.DotLine}.get(self._view.line_style, Qt.PenStyle.SolidLine)
        if tool in (TOOL_HIGHLIGHT, TOOL_UNDERLINE, TOOL_STRIKEOUT) and self._markup_boxes:
            # 確定したときに付く文字の範囲そのものを見せる
            for (x0, y0, x1, y1) in self._markup_boxes:
                r = QRect(int(x0), int(y0), int(x1 - x0), int(y1 - y0))
                if tool == TOOL_HIGHLIGHT:
                    fill = QColor(color)
                    fill.setAlpha(round(110 * self._view.opacity))
                    painter.fillRect(r, fill)
                else:
                    painter.setPen(QPen(QColor(color), 2))
                    y = r.bottom() - 1 if tool == TOOL_UNDERLINE else r.center().y()
                    painter.drawLine(r.left(), y, r.right(), y)
        elif tool in (TOOL_HIGHLIGHT, TOOL_UNDERLINE, TOOL_STRIKEOUT):
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
        elif tool == TOOL_SNAPSHOT:
            # 切り取り範囲：青の破線＋薄い塗り（注釈の色に左右されない）
            painter.setPen(QPen(QColor(26, 115, 232), 1, Qt.PenStyle.DashLine))
            painter.fillRect(rect, QColor(26, 115, 232, 28))
            painter.drawRect(rect)
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


class _ResumeBar(QFrame):
    """「前回は N ページ目まで見ていました［続きから表示］」をページの上に浮かべる。

    開いた文書を勝手に前回のページへ飛ばすのではなく、先頭のまま開いて、
    続きを読みたいときだけ 1 クリックで飛べるようにする。モーダルの質問に
    しないのは、起動時の復元などで何枚も開いたときに質問攻めにしないため。
    レイアウトに挟まず浮かべるので、出し入れでページの倍率や位置は動かない。
    """

    def __init__(self, view: "PageView", page: int, total: int) -> None:
        super().__init__(view)
        self.setObjectName("resumeBar")
        self._view = view
        self.page = page
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 7, 8, 7)
        lay.setSpacing(8)
        text = QLabel(f"前回は {page + 1} ページ目（全 {total} ページ）まで見ていました")
        text.setObjectName("resumeText")
        self.btn_resume = QPushButton("続きから表示")
        self.btn_resume.setObjectName("primary")
        self.btn_close = QPushButton("閉じる")
        for b in (self.btn_resume, self.btn_close):
            # ページ側のキー操作（矢印・PageDown）を奪わない
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_resume.clicked.connect(self._resume)
        self.btn_close.clicked.connect(self.dismiss)
        lay.addWidget(text)
        lay.addSpacing(6)
        lay.addWidget(self.btn_resume)
        lay.addWidget(self.btn_close)
        self.adjustSize()

    def _resume(self) -> None:
        view, page = self._view, self.page
        self.dismiss()
        view.set_page(page)

    def dismiss(self) -> None:
        if self._view._resume_bar is self:
            self._view._resume_bar = None
        self.hide()
        self.deleteLater()


class _PageCanvas(QWidget):
    """ページを並べる土台。背景色と、各ページの落ち影を描く。

    影は QGraphicsDropShadowEffect（ページごとにオフスクリーン描画＋ぼかしが
    走って重い）を使わず、外側ほど薄い角丸矩形を数枚重ねて表現する。見えて
    いるページの分だけなので、スクロールしても描画コストはほぼ増えない。
    """

    LAYERS = 7      # 影の層数。多いほど滑らかで、そのぶん少しだけ重い
    SPREAD = 9      # 影の広がり(px)
    DROP = 3        # 影を下にずらす量(px)

    def __init__(self, view: "PageView") -> None:
        super().__init__()
        self._view = view
        self._bg = QColor(theme.canvas_color(False))
        self._shadow = QColor(0, 0, 0)
        self._shadow_alpha = 0.28

    def apply_theme(self, dark: bool) -> None:
        self._bg = QColor(theme.canvas_color(dark))
        rgb, alpha = theme.page_shadow(dark)
        r, g, b = (int(v) for v in rgb.split(","))
        self._shadow = QColor(r, g, b)
        self._shadow_alpha = alpha
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        # ページの外（灰色の地）をクリックしても、文字の入力は確定させる
        ed = self._view._text_editor
        if ed is not None and event.button() == Qt.MouseButton.LeftButton:
            ed._finish()
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        p = QPainter(self)
        p.fillRect(event.rect(), self._bg)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        area = event.rect().adjusted(-self.SPREAD * 3, -self.SPREAD * 3,
                                     self.SPREAD * 3, self.SPREAD * 3)
        for label in self._view._labels:
            rect = label.geometry()
            if not rect.intersects(area):
                continue
            for i in range(self.LAYERS, 0, -1):
                # 外側の層ほど薄く。重ねることで縁がぼけて見える。
                a = self._shadow_alpha * (0.9 / self.LAYERS) * (self.LAYERS - i + 1)
                self._shadow.setAlphaF(min(1.0, a))
                p.setBrush(self._shadow)
                grow = self.SPREAD * i // self.LAYERS
                p.drawRoundedRect(
                    rect.adjusted(-grow, -grow + self.DROP, grow, grow + self.DROP),
                    grow + 2, grow + 2)


class PageView(QScrollArea):
    """縦スクロールで全ページを連続表示する。"""

    page_changed = Signal(int)        # 表示中（中央付近）のページが変わった
    annotation_changed = Signal(int)  # 注釈を追加/削除したページ
    selection_changed = Signal(bool)       # 注釈の選択有無が変わった
    text_selection_changed = Signal(bool)  # テキスト選択の有無が変わった
    zoom_changed = Signal(float)           # ズーム倍率が変わった
    form_changed = Signal()                # フォーム入力欄の値が変わった
    gesture = Signal(str)   # マウスジェスチャー left/right/up/down
    search_requested = Signal(str)   # 右クリックメニューから検索
    rotate_requested = Signal(int)   # 右クリックメニューからページ回転(±90)
    link_activated = Signal(dict)    # ページ内リンクをクリック（document.links の要素）
    link_hovered = Signal(str)       # リンクの上にいる（行き先の説明。空文字=離れた）
    snapshot_copied = Signal(int, int)  # 範囲を画像としてコピーした (幅px, 高さpx)
    status_message = Signal(str)     # ステータスバーに出す知らせ（失敗など）

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._container = _PageCanvas(self)
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
        # ズーム時の overlay 再構築はデバウンスする。全 overlay（このフォームで
        # 約270個）の再生成は ~0.5秒かかり、Ctrl+ホイールの1ノッチごとにこれを
        # 走らせると拡大縮小が極端に重くなるため、ズームが落ち着いてから1回だけ
        # 作り直す（それまでは一旦消しておく）。
        self._overlay_rebuild_timer = QTimer(self)
        self._overlay_rebuild_timer.setSingleShot(True)
        self._overlay_rebuild_timer.setInterval(140)
        self._overlay_rebuild_timer.timeout.connect(self._build_form_overlays)
        # スクロールが落ち着いたら、次に見えそうなページを先に描いておく
        # （描画キャッシュに入れるだけ。送ったときに待たされない）。
        self._prefetch_timer = QTimer(self)
        self._prefetch_timer.setSingleShot(True)
        self._prefetch_timer.setInterval(120)
        self._prefetch_timer.timeout.connect(self._prefetch_around)
        self._index: int = 0
        self._zoom: float = 1.0
        self._cols = 1  # 1=通常 / 2=見開き
        self._suspend_scroll = False
        self._dark = False
        # 「幅/縦に合わせる」の状態。文書を開く前でもリサイズが飛んでくる
        # （予備ビューや作った直後の表示）ので、ここで用意しておく。
        self._fit_mode: str | None = None
        self._fit_page_size: tuple | None = None
        self._in_fit = False
        self._resume_bar: _ResumeBar | None = None   # 「前回の続き」の案内

        # 注釈ツールの状態
        self.tool = TOOL_NONE
        # 色はハイライトとそれ以外で分けて持つ。1 色を共有していた頃は、黄色の
        # ハイライトの後にテキストを入れると黄色い文字になっていた。
        self.color = QColor(0, 0, 0)                  # 文字・図形・ペンなど
        self.highlight_color = QColor(255, 219, 46)   # ハイライト
        self.pen_width = 2
        self.line_style = "solid"    # solid / dashed / dotted
        self.line_start = "none"     # 直線・矢印の始点スタイル（LINE_END_STYLES のキー）
        self.line_end = "none"       # 直線・矢印の終点スタイル
        self.fill_enabled = False    # 四角/円の塗りつぶし
        self.fill_color = QColor(255, 255, 255)
        self.opacity = 1.0           # 0.0-1.0
        self.text_size = 14          # テキスト注釈の文字サイズ(pt)
        self.text_font = "helv"      # テキスト注釈のフォント（helv/tiro/cour）
        self._text_editor = None     # その場入力中の _TextAnnotEditor（1個だけ）
        self._text_ctx = None        # {"index", "rect", "xref"(既存編集時のみ), "color"}
        self.pending_stamp = None    # 配置待ちスタンプ {text, color(QColor), fontsize}
        self._sel_index = -1
        self._sel_xref = None
        # 検索
        self._search_hits: list = []   # [(page_index, fitz.Rect)]
        self._search_cur = -1
        # テキスト選択
        # 選択中のテキスト。ページをまたげるよう {ページ番号: PDF座標の矩形}
        self._text_sel: dict[int, list] = {}
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
        self.dismiss_resume_offer()
        self._doc = doc
        doc.add_renumber_listener(self._on_doc_renumbered)
        self._index = 0
        self._zoom = 1.0
        self._gesture_enabled = True   # 右ドラッグのジェスチャーを使うか
        # 「幅/縦に合わせる」を選んでいるか（None=手動倍率）。
        # 選んでいる間はウィンドウ幅が変わるたびに合わせ直す。
        self._fit_mode = None
        self._fit_page_size = None  # 合わせた時のページの大きさ
        self._in_fit = False   # 合わせ直し中の再入防止
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

    def fit_mode(self) -> str | None:
        """"width"（幅に合わせる）/ "height"（縦に合わせる）/ None（手動倍率）。"""
        return self._fit_mode

    # --- 前回の続き -------------------------------------------------------
    def show_resume_offer(self, page: int) -> None:
        """前回見ていたページへ飛ぶかを、ページの上に浮かべた案内で尋ねる。"""
        self.dismiss_resume_offer()
        if not self._doc or not self._doc.is_open:
            return
        page = max(0, min(page, self._doc.page_count - 1))
        if page <= 0:
            return
        self._resume_bar = _ResumeBar(self, page, self._doc.page_count)
        self._place_resume_bar()
        self._resume_bar.show()
        self._resume_bar.raise_()

    def resume_offer_page(self) -> int | None:
        """案内を出しているなら、その飛び先のページ番号（0 始まり）。"""
        return self._resume_bar.page if self._resume_bar is not None else None

    def dismiss_resume_offer(self) -> None:
        if self._resume_bar is not None:
            self._resume_bar.dismiss()

    def _place_resume_bar(self) -> None:
        bar = self._resume_bar
        if bar is None:
            return
        vp = self.viewport().geometry()
        bar.adjustSize()
        w = min(bar.sizeHint().width(), max(120, vp.width() - 24))
        bar.setGeometry(vp.x() + (vp.width() - w) // 2, vp.y() + 12,
                        w, bar.sizeHint().height())

    def apply_theme(self, dark: bool) -> None:
        """ページ周辺の背景・落ち影・紙の色をテーマに合わせる。"""
        self._dark = dark
        self.viewport().setStyleSheet(f"background: {theme.canvas_color(dark)};")
        self._container.apply_theme(dark)
        for label in self._labels:
            label.apply_theme(dark)

    # --- 注釈ツール -----------------------------------------------------
    def set_tool(self, tool: str) -> None:
        # 入力中のテキスト注釈があれば、まず確定させてからツールを切り替える。
        if self._text_editor is not None:
            self._text_editor._finish()
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
            # テキストはクリックした所から打てるので、文字入力のカーソル
            TOOL_TEXT: Qt.CursorShape.IBeamCursor,
        }.get(tool, Qt.CursorShape.CrossCursor)
        for lbl in self._labels:
            lbl.setCursor(cursor)

    def set_color(self, qcolor: QColor) -> None:
        """文字・図形・ペンなど（ハイライト以外）の色。"""
        self.color = QColor(qcolor)
        # その場入力中なら文字色を即反映（「入力しながら色を変える」）。
        if self._text_editor is not None:
            if self._text_ctx is not None:
                self._text_ctx["color"] = QColor(qcolor)
            self._apply_text_editor_style()

    def set_highlight_color(self, qcolor: QColor) -> None:
        self.highlight_color = QColor(qcolor)

    def tool_color(self, tool: str | None = None) -> QColor:
        """そのツールで描くときの色（ハイライトだけ別の色を持つ）。"""
        tool = self.tool if tool is None else tool
        return self.highlight_color if tool == TOOL_HIGHLIGHT else self.color

    def set_text_size(self, size: int) -> None:
        self.text_size = max(6, int(size))
        if self._text_editor is not None:
            self._apply_text_editor_style()
        # テキスト注釈を選択中なら、入力後でもサイズを変更できる
        if self._selection_is_freetext():
            self.restyle_selection(fontsize=self.text_size)

    def set_text_font(self, fontname: str) -> None:
        """テキスト注釈のフォント（helv/tiro/cour）を設定する。

        その場入力中はプレビューへ、テキスト注釈を選択中はその注釈へ即反映する。
        """
        self.text_font = fontname or "helv"
        if self._text_editor is not None:
            self._apply_text_editor_style()
        if self._selection_is_freetext():
            self.restyle_selection(fontname=self.text_font)

    def _selection_is_freetext(self) -> bool:
        if self._sel_xref is None or not self._doc:
            return False
        style = self._doc.annot_textstyle(self._sel_index, self._sel_xref)
        return bool(style) and style["kind"] == "FreeText"

    def selection_textstyle(self):
        """選択中の注釈の文字スタイル（annot_textstyle の dict）。無ければ None。"""
        if self._sel_xref is None or not self._doc:
            return None
        return self._doc.annot_textstyle(self._sel_index, self._sel_xref)

    def set_stamp(self, text: str, qcolor: QColor, fontsize: int = 22) -> None:
        """スタンプ配置モードにする。以後クリックした位置に text を置く。"""
        self.pending_stamp = {"text": text, "color": qcolor, "fontsize": fontsize}
        self.set_tool(TOOL_STAMP)

    def set_pen_width(self, width: int) -> None:
        self.pen_width = max(1, int(width))
        # 注釈を選択中なら、その注釈にも即座に反映する（新規描画用の設定も更新）。
        self.restyle_selection(width=self.pen_width)

    def set_line_style(self, style: str) -> None:
        self.line_style = style
        self.restyle_selection(dashes=self._dashes())

    def set_line_start(self, key: str) -> None:
        self.line_start = key
        # 注釈を選択中なら、その注釈の線端にも即座に反映する。
        self.restyle_selection(line_ends=self._line_ends())

    def set_line_end(self, key: str) -> None:
        self.line_end = key
        self.restyle_selection(line_ends=self._line_ends())

    def _line_ends(self):
        """現在の始点/終点キーを PDF 線端コード (始点, 終点) のタプルに変換する。"""
        from .document import LINE_END_STYLES
        import fitz
        s = LINE_END_STYLES.get(self.line_start, fitz.PDF_ANNOT_LE_NONE)
        e = LINE_END_STYLES.get(self.line_end, fitz.PDF_ANNOT_LE_NONE)
        return (s, e)

    def set_fill_enabled(self, on: bool) -> None:
        self.fill_enabled = bool(on)
        self.restyle_selection(fill=self._fill_rgb())

    def set_fill_color(self, qcolor: QColor) -> None:
        self.fill_color = qcolor
        if self.fill_enabled:
            self.restyle_selection(fill=self._fill_rgb())

    def set_opacity(self, opacity: float) -> None:
        self.opacity = max(0.05, min(1.0, float(opacity)))
        self.restyle_selection(opacity=self.opacity)

    def _rgb(self, tool: str | None = None) -> tuple:
        c = self.tool_color(tool)
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

        if tool == TOOL_SNAPSHOT:
            r = QRect(p0, p1).normalized()
            if r.width() < 4 or r.height() < 4:
                return
            self.copy_region_image(index, r.left(), r.top(), r.right() + 1,
                                   r.bottom() + 1)
            return

        if tool == TOOL_TEXT:
            # モーダルではなく、その場で打てる編集ボックスを重ねる。ほぼ動かさずに
            # 離したら「その位置から打つ」（打った分だけ横に伸びる）、ドラッグなら
            # その幅で折り返す欄にする。
            z = self._zoom
            if abs(p1.x() - p0.x()) < 12:
                self._begin_text_edit(index, (p0.x() / z, p0.y() / z), click=True)
            else:
                x0, y0 = min(p0.x(), p1.x()) / z, min(p0.y(), p1.y()) / z
                self._begin_text_edit(
                    index, (x0, y0), box_w=abs(p1.x() - p0.x()) / z,
                    min_h=abs(p1.y() - p0.y()) / z)
            return

        rect = self._doc.label_to_pdf_rect(
            index, p0.x(), p0.y(), p1.x(), p1.y(), self._zoom
        )
        if tool == TOOL_HIGHLIGHT:
            # なぞった範囲の文字を 1 文字単位で塗る（以前は「語」単位で、区切りの
            # 無い日本語だと行ごと塗られた）。文字が無い所（スキャン画像など）は
            # ドラッグした四角を塗る。
            _text, rects = self._doc.select_text(index, a, b)
            if rects:
                self._doc.markup_rects(index, rects, "highlight", self._rgb(tool),
                                       opacity=self.opacity)
            elif rect.width >= 2 and rect.height >= 2:
                self._doc.add_highlight(index, rect, color=self._rgb(tool),
                                        opacity=self.opacity)
            else:
                return
            self._after_annot(index)
            return
        if rect.width < 2 or rect.height < 2:
            return  # 誤クリックの極小矩形は無視
        if tool == TOOL_RECT:
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
        self._after_annot(index)

    # --- テキスト注釈のその場入力 --------------------------------------
    #: 入力欄の字形。欧文は MuPDF の基本 14 フォントと字幅が同じもの、和文は
    #: MuPDF が描くのに近いゴシック（どれも全角 1 em なので折り返しが揃う）
    _EDITOR_FAMILIES = {"helv": "Arial", "tiro": "Times New Roman",
                        "cour": "Courier New"}
    # 和文は確定後と同じ MS ゴシック / MS 明朝（jpfonts が外観に書き込む字形）。
    # 「UI」「P」の付く方はかなが詰まる（プロポーショナル）ので使わない
    _EDITOR_JA = {"tiro": ["MS Mincho", "Yu Mincho"],
                  "default": ["MS Gothic", "Yu Gothic", "Meiryo"]}

    def _begin_text_edit(self, index: int, at: tuple, *, click: bool = False,
                         box_w: float | None = None, min_h: float = 0.0) -> None:
        """新しいテキストを打つ欄を出す。座標はページの pt（表示の向き）。

        click=True はクリックした位置から打つ欄（打った分だけ横に伸びる。
        カーソルの縦の真ん中がクリック位置に来るよう少し上に置く）。それ以外は
        ドラッグした幅 box_w で折り返す欄。確定で FreeText 注釈にする。
        """
        x, y = at
        if click:
            y = max(0.0, y - FT_LINE * self.text_size / 2)
            box_w = None
        self._open_text_editor(index, (x, y), box_w=box_w, min_h=min_h)

    def _begin_text_edit_existing(self, index: int, xref) -> None:
        """既存のテキスト注釈（FreeText）を、その場の編集ボックスで直接編集する。

        別ウィンドウ（ダイアログ）を開かず、注釈の位置に本文を載せて編集できる。
        確定（外側クリック / Esc / Ctrl+Enter）で本文を書き戻す。
        """
        if not self._doc:
            return
        desc = self._doc.describe_annot(index, xref)
        if not desc or desc.get("kind") != "FreeText":
            return
        if (self._sel_index, self._sel_xref) != (index, xref):
            # 選んでおくと、ツールバーの文字サイズ・フォントがこの注釈に揃う
            self.set_selection(index, xref)
        style = self._doc.annot_textstyle(index, xref) or {}
        self.text_size = max(4, int(round(style.get("fontsize") or self.text_size)))
        self.text_font = style.get("fontname") or self.text_font
        x0, y0, x1, y1 = self._doc.pdf_rect_to_label(index, desc["rect"], 1.0)
        tc = style.get("text_color") or (0.0, 0.0, 0.0)
        # こちらで折り返しを入れていない（クリックで置いた）注釈は、直すときも
        # 横に伸びる欄で開く。幅を決めて作った注釈は、その幅で折り返す。
        soft_wrapped = desc.get("soft", "") != desc.get("content", "")
        self._open_text_editor(
            index, (x0, y0),
            box_w=(x1 - x0) if soft_wrapped else None,
            min_h=(y1 - y0) if soft_wrapped else 0.0,
            text=desc.get("soft", ""), xref=xref,
            color=QColor.fromRgbF(*[max(0.0, min(1.0, v)) for v in tc[:3]]))

    def edit_text_annot(self, index: int, xref) -> None:
        """テキスト注釈をその場で編集する（テキストツールでクリックしたとき）。"""
        self._begin_text_edit_existing(index, xref)

    def finish_text_edit(self) -> None:
        """入力中の文字があれば確定させる（保存・印刷・タブを閉じる前に呼ぶ）。

        テキスト注釈の入力欄と、カーソルがあるフォームの入力欄の両方。フォームの
        欄は離れたときに書き込むので、カーソルを置いたまま Ctrl+S すると（上書き
        の確認を切っていると）その欄だけ保存から漏れていた。
        """
        if self._text_editor is not None:
            self._text_editor._finish()
        self.commit_focused_field()

    def commit_focused_field(self) -> None:
        """カーソルがあるフォームの入力欄の値を書き込む（入力は続けられる）。"""
        from PySide6.QtWidgets import QApplication

        w = QApplication.focusWidget()
        overlays = set(self._form_overlays)
        while w is not None and w not in overlays:
            w = w.parentWidget()
        if isinstance(w, QLineEdit):
            w.editingFinished.emit()
        elif isinstance(w, _FormTextArea):
            w.committed.emit()

    def _on_doc_renumbered(self) -> None:
        """文書のオブジェクト番号が付け直された（パスワード付きの保存など）。

        入力欄・選択中の注釈は古い番号を覚えているので作り直す。入力欄の
        処理の途中で呼ばれることもあるので、次の周回で行う。
        """
        def redo():
            if not self._doc or not self._doc.is_open:
                return
            self.clear_selection()
            if self._form_mode:
                self._build_form_overlays()
            self._after_annot(self._index)   # 注釈一覧などを読み直させる
        QTimer.singleShot(0, redo)

    def _open_text_editor(self, index: int, origin: tuple, *, box_w=None,
                          min_h: float = 0.0, text: str = "", xref=None,
                          color: QColor | None = None) -> None:
        if self._text_editor is not None:
            self._text_editor._finish()   # 前の入力は捨てずに確定させる
        if not (0 <= index < len(self._labels)):
            return
        ed = _TextAnnotEditor(self._labels[index], auto_width=box_w is None)
        self._text_editor = ed
        self._text_ctx = {"index": index, "xref": xref, "color": color,
                          "origin": origin, "box_w": box_w, "min_h": min_h}
        self._apply_text_editor_style()
        if xref is not None:
            # 直す前の注釈が下に透けて二重に見えないよう、注釈を除いた紙面を敷く
            try:
                ed.set_backdrop(self._doc.render_page_without_annots(index, self._zoom))
            except Exception:  # noqa: BLE001  下地が無くても編集はできる
                pass
        if text:
            ed.setPlainText(text)
        ed.committed.connect(self._commit_text_edit)
        ed.cancelled.connect(self._discard_text_editor)
        ed.show()
        ed.raise_()
        ed.setFocus()
        cur = ed.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        ed.setTextCursor(cur)

    def _apply_text_editor_style(self) -> None:
        """入力欄の文字・色・位置を、今の倍率と設定に合わせる。"""
        ed, ctx = self._text_editor, self._text_ctx
        if ed is None or ctx is None:
            return
        z = self._zoom
        px = max(4.0, self.text_size * z)
        f = QFont(ed.font())
        f.setFamilies([self._EDITOR_FAMILIES.get(self.text_font, "Arial"),
                       *self._EDITOR_JA.get(self.text_font, self._EDITOR_JA["default"])])
        # 端数まで合わせる（整数 px に丸めると 1 行で数 px ずれ、確定後の方が
        # 長く見える）。ヒンティングも切って MuPDF の字幅に寄せる。
        f.setPointSizeF(px * 72.0 / max(1, ed.logicalDpiY()))
        f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        # 既存注釈の編集中はその注釈の文字色、新規はツールバーの色
        color = ctx.get("color") or self.color
        label = self._labels[ctx["index"]]
        ox, oy = ctx["origin"][0] * z, ctx["origin"][1] * z
        if ctx["box_w"] is None:
            wrap_w = max(px, label.width() - ox - _TextAnnotEditor.CARET_ROOM - 4)
            min_w = px
        else:
            wrap_w = min_w = ctx["box_w"] * z
        ed.configure(f, color, px, (ox, oy), wrap_w, min_w, ctx["min_h"] * z)

    def _commit_text_edit(self) -> None:
        ed, ctx = self._text_editor, self._text_ctx
        if ed is None or ctx is None:
            return
        soft = ed.toPlainText()
        lines = ed.wrapped_lines()
        self._discard_text_editor()
        if not self._doc or not self._doc.is_open:
            return
        index, xref = ctx["index"], ctx.get("xref")
        if xref is not None and self._sel_index == index and self._sel_xref is not None:
            # 編集中にサイズ変更等で作り直されていたら、選択中の xref（最新）を使う
            xref = self._sel_xref
        if not soft.strip():
            if xref is not None:   # 中身を全部消して確定した = 注釈ごと消す
                self.clear_selection()
                self._doc.delete_annot_xref(index, xref)
                self._after_annot(index)
            return
        fs, fn = self.text_size, self.text_font
        wrapped = "\n".join(lines)
        need_w, need_h = ft_needed_size(wrapped, fs, fn)
        x, y = ctx["origin"]
        w = need_w + 1 if ctx["box_w"] is None else max(ctx["box_w"], need_w)
        h = max(ctx["min_h"], need_h)
        rect = self._doc.label_to_pdf_rect(index, x, y, x + w, y + h, 1.0)
        c = ctx.get("color") or self.color
        rgb = (c.redF(), c.greenF(), c.blueF())
        if xref is None:
            self._doc.add_freetext(index, rect, soft, color=rgb, fontsize=fs,
                                   fontname=fn, wrapped=wrapped)
        else:
            new_xref = self._doc.set_annot_text(
                index, xref, soft, wrapped=wrapped, rect=rect, color=rgb,
                fontsize=fs, fontname=fn)
            if new_xref is not None and self._sel_index == index:
                self._sel_xref = new_xref
            if self.tool != TOOL_NONE:
                self.clear_selection()   # テキストツールでは選択枠を残さない
        self._after_annot(index)

    def _discard_text_editor(self) -> None:
        ed = self._text_editor
        self._text_editor = None
        self._text_ctx = None
        if ed is not None:
            ed._done = True
            ed.setParent(None)
            ed.deleteLater()

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
            # 掴むときと同じく形どおりに判定する（外接矩形の空白では消さない）。
            # 消しゴムはこすって消す道具なので、外れの許し幅は倍にする。
            xref = self.hit_annot(index, p, slop=self.HIT_PX * 2)
            if xref is not None:
                if self._sel_index == index and self._sel_xref == xref:
                    self.clear_selection()
                self._doc.delete_annot_xref(index, xref)
                self._after_annot(index)
        elif tool == TOOL_STAMP and self.pending_stamp:
            s = self.pending_stamp
            c = s["color"]
            self._doc.add_stamp(index, pt, s["text"],
                                color=(c.redF(), c.greenF(), c.blueF()),
                                fontsize=s["fontsize"])
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
        """同じページの中でのドラッグ選択。"""
        if not self._doc:
            return
        a = self._doc.label_to_pdf_point(index, p0.x(), p0.y(), self._zoom)
        b = self._doc.label_to_pdf_point(index, p1.x(), p1.y(), self._zoom)
        text, rects = self._doc.select_text(index, a, b)
        self._apply_selection(text, {index: rects} if rects else {})

    def update_text_selection_to(self, start_index: int, start_pos: QPoint,
                                 global_pos) -> None:
        """ドラッグ選択の更新。終点が別のページに入っていてもよい。

        マウスは押したページがグラブし続けるので、終点は画面座標で受け取って
        どのページの上かを調べ直す。
        """
        if not self._doc or not self._doc.is_open:
            return
        end_index, end_pos = self._page_at_global(global_pos)
        a = self._doc.label_to_pdf_point(
            start_index, start_pos.x(), start_pos.y(), self._zoom)
        b = self._doc.label_to_pdf_point(
            end_index, end_pos.x(), end_pos.y(), self._zoom)
        text, per_page = self._doc.select_across(start_index, a, end_index, b)
        self._apply_selection(text, per_page)

    def _page_at_global(self, global_pos) -> tuple:
        """画面座標がどのページのどこかを返す。外れていれば最寄りのページ。"""
        nearest = None
        for i, label in enumerate(self._labels):
            local = label.mapFromGlobal(global_pos)
            if label.rect().contains(local):
                return i, local
            if local.y() < 0:
                dist = -local.y()
            elif local.y() >= label.height():
                dist = local.y() - label.height()
            else:
                dist = 0
            if nearest is None or dist < nearest[0]:
                nearest = (dist, i, local)
        if nearest is None:
            return self._index, QPoint(0, 0)
        _d, i, local = nearest
        label = self._labels[i]
        return i, QPoint(max(0, min(local.x(), label.width() - 1)),
                         max(0, min(local.y(), label.height() - 1)))

    def _apply_selection(self, text: str, per_page: dict) -> None:
        """選択結果（文字列と、ページごとの矩形）を画面に反映する。"""
        had = bool(self._text_sel_text)
        touched = set(self._text_sel) | set(per_page)
        self._text_sel = {i: r for i, r in per_page.items() if r}
        self._text_sel_text = text
        for i in touched:
            if 0 <= i < len(self._labels):
                self._labels[i].update()
        if bool(text) != had:
            self.text_selection_changed.emit(bool(text))

    def select_all_text(self) -> bool:
        """文書全体のテキストを選ぶ（Ctrl+A）。"""
        if not self._doc or not self._doc.is_open:
            return False
        self.begin_text_selection()
        text, per_page = self._doc.select_all_text()
        self._apply_selection(text, per_page)
        return bool(text.strip())

    def markup_selection(self, kind: str) -> bool:
        """選択中のテキストにハイライト/下線/取消線を付ける。"""
        if not self._doc or not self._text_sel:
            return False
        color = self._rgb(TOOL_HIGHLIGHT if kind == "highlight" else TOOL_UNDERLINE)
        done = False
        for index, rects in sorted(self._text_sel.items()):
            if self._doc.markup_rects(index, rects, kind, color):
                done = True
                self.refresh_page(index)
                self.annotation_changed.emit(index)
        if done:
            self.clear_text_selection()
        return done

    def gesture_enabled(self) -> bool:
        return self._gesture_enabled

    def set_gesture_enabled(self, on: bool) -> None:
        """マウスジェスチャーを使うかどうか（設定から切り替える）。"""
        self._gesture_enabled = bool(on)

    def begin_text_selection(self) -> None:
        """選択を始めるときに、憶えている文字レイアウトを捨てる。

        ドラッグ中は同じ解析結果を使い回して速くするので、開始時に一度だけ
        捨てておけば編集後の古い情報を掴み続けることがない。
        """
        if self._doc and self._doc.is_open:
            self._doc.invalidate_text_layout()

    def select_word_at(self, index: int, pos: QPoint) -> bool:
        """ダブルクリックした位置の語を選ぶ。選べたら True。"""
        if not self._doc or not self._doc.is_open:
            return False
        self.begin_text_selection()
        p = self._doc.label_to_pdf_point(index, pos.x(), pos.y(), self._zoom)
        text, rects = self._doc.select_word(index, p)
        self._apply_selection(text, {index: rects} if rects else {})
        return bool(text.strip())

    def select_line_at(self, index: int, pos: QPoint) -> bool:
        """トリプルクリックした位置の行を丸ごと選ぶ。選べたら True。"""
        if not self._doc or not self._doc.is_open:
            return False
        self.begin_text_selection()
        p = self._doc.label_to_pdf_point(index, pos.x(), pos.y(), self._zoom)
        text, rects = self._doc.select_line(index, p)
        self._apply_selection(text, {index: rects} if rects else {})
        return bool(text.strip())

    def clear_text_selection(self) -> None:
        self._apply_selection("", {})

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
        rects = self._text_sel.get(index)
        if not rects or not self._doc:
            return []
        return [self._doc.pdf_rect_to_label(index, r, self._zoom)
                for r in rects]

    def selection_pages(self) -> list:
        """選択がまたがっているページ番号（若い順）。"""
        return sorted(self._text_sel)

    # --- 注釈の選択・移動・編集 ---------------------------------------
    # --- リンク / スナップショット --------------------------------------
    SNAPSHOT_DPI = 200   # クリップボードへ入れる画像の解像度（印刷しても粗くない）

    def link_at(self, index: int, pos: QPoint):
        """表示座標 pos にあるリンク（document.links の要素）。無ければ None。"""
        if not self._doc or not self._doc.is_open:
            return None
        return self._doc.link_at(index, pos.x(), pos.y(), self._zoom)

    def copy_region_image(self, index: int, x0: int, y0: int, x1: int, y1: int) -> bool:
        """表示座標の矩形を画像にしてクリップボードへ入れる。"""
        from PySide6.QtWidgets import QApplication

        if not self._doc or not self._doc.is_open:
            return False
        try:
            img = self._doc.snapshot_image(index, x0, y0, x1, y1, self._zoom,
                                           dpi=self.SNAPSHOT_DPI)
        except ValueError:
            return False
        QApplication.clipboard().setImage(img)
        self.snapshot_copied.emit(img.width(), img.height())
        return True

    def copy_page_image(self, index: int) -> bool:
        """ページ全体を画像にしてクリップボードへ入れる。"""
        from PySide6.QtWidgets import QApplication

        if not self._doc or not self._doc.is_open:
            return False
        img = self._doc.page_image(index, dpi=self.SNAPSHOT_DPI)
        QApplication.clipboard().setImage(img)
        self.snapshot_copied.emit(img.width(), img.height())
        return True

    @staticmethod
    def copy_text_to_clipboard(text: str) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text or "")

    #: 注釈を掴める距離（画面上の px）。細い線でも少し外して押せば当たる
    HIT_PX = 5

    def hit_annot(self, index: int, pos: QPoint, slop: float | None = None):
        """表示座標 pos で掴める注釈の xref（形どおりの当たり判定）。

        slop は線からどれだけ外れても当たりにするか（画面 px。既定 HIT_PX）。
        """
        if not self._doc:
            return None
        pt = self._doc.label_to_pdf_point(index, pos.x(), pos.y(), self._zoom)
        px = self.HIT_PX if slop is None else slop
        tol = px / max(self._zoom, 0.05)
        xref = self._doc.annot_hit(index, pt, tol=tol)
        if xref is None and not self._doc.text_at(index, pt):
            # 文字の無い所なら、塗りの無い四角・円の内側をつまんでも掴める
            # （文字の上なら掴まず、枠の中の文字を選べるようにする）
            xref = self._doc.annot_hit(index, pt, tol=tol, interior=True)
        return xref

    def is_freetext(self, index: int, xref) -> bool:
        return bool(self._doc) and self._doc.annot_kind(index, xref) == "FreeText"

    def selection_is_markup(self) -> bool:
        """選択中の注釈が、文字に付けるもの（ハイライト・下線・取消線）か。"""
        if self._sel_xref is None or not self._doc:
            return False
        return self._doc.annot_kind(self._sel_index, self._sel_xref) in \
            PdfDocument.MARKUP_KINDS

    def text_boxes_between(self, index: int, p0: QPoint, p1: QPoint) -> list:
        """p0→p1 でなぞった文字の枠（表示座標）。ハイライト等の下見に使う。"""
        if not self._doc or not self._doc.is_open:
            return []
        a = self._doc.label_to_pdf_point(index, p0.x(), p0.y(), self._zoom)
        b = self._doc.label_to_pdf_point(index, p1.x(), p1.y(), self._zoom)
        _text, rects = self._doc.select_text(index, a, b)
        return [self._doc.pdf_rect_to_label(index, r, self._zoom) for r in rects]

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

    def restyle_selection(self, *, width=None, dashes=_UNSET,
                          opacity=_UNSET, fill=_UNSET, line_ends=_UNSET,
                          fontsize=None, fontname=None) -> bool:
        """選択中の注釈の太さ・線種・不透明度・塗り・線端・文字サイズ/フォントを変更する。

        注釈を選択していなければ何もしない（新規描画用の設定変更のときは無視）。
        指定した項目だけ変え、残りは元の値を保つ。
        """
        if self._sel_xref is None or not self._doc:
            return False
        new_xref = self._doc.restyle_annot(
            self._sel_index, self._sel_xref,
            width=width, dashes=dashes, opacity=opacity, fill=fill,
            line_ends=line_ends, fontsize=fontsize, fontname=fontname)
        if new_xref is not None:
            self._sel_xref = new_xref
        self._after_annot(self._sel_index)
        return True

    def delete_selection(self) -> None:
        if self._sel_xref is None or not self._doc:
            return
        index = self._sel_index
        xref = self._sel_xref
        self.clear_selection()
        self._doc.delete_annot_xref(index, xref)
        self._after_annot(index)

    def current_annot_ref(self):
        """選択中の注釈の (page_index, xref)。無ければ None。"""
        if self._sel_xref is None:
            return None
        return (self._sel_index, self._sel_xref)

    def selected_annot_desc(self):
        """選択中の注釈の内容（コピー用の dict）を返す。無ければ None。"""
        if self._sel_xref is None or not self._doc:
            return None
        return self._doc.describe_annot(self._sel_index, self._sel_xref)

    def paste_annot(self, desc) -> bool:
        """コピーした注釈を現在のページに少しずらして貼り付け、選択状態にする。"""
        if not desc or not self._doc:
            return False
        index = self._index
        new_xref = self._doc.paste_annot(index, desc)
        if new_xref is None:
            return False
        self._after_annot(index)
        self.set_selection(index, new_xref)
        return True

    def undo(self):
        """直前の操作（注釈でもページ操作でも）を取り消す。

        戻り値は (kind, page_index, label)。kind が "doc" のときは文書の構成
        そのものが変わっているので、呼び出し側が全体を作り直すこと。
        """
        if not self._doc:
            return None
        if self._text_editor is not None:
            self._text_editor._finish()  # 入力中なら確定してから取り消す
        return self._apply_history(self._doc.undo())

    def redo(self):
        """取り消した操作をやり直す。戻り値は undo と同じ。"""
        if not self._doc:
            return None
        return self._apply_history(self._doc.redo())

    def _apply_history(self, result):
        if result is None:
            return None
        kind, index, _label = result
        self.clear_selection()
        self.clear_text_selection()
        if kind == "annot" and index is not None:
            self._after_annot(index)
        # kind == "doc" の作り直しは呼び出し側（MainWindow._reload）に任せる。
        # ここで reload するとサムネイルや整理画面と食い違うため。
        return result

    def can_undo(self) -> bool:
        return bool(self._doc and self._doc.can_undo())

    def can_redo(self) -> bool:
        return bool(self._doc and self._doc.can_redo())

    def undo_label(self) -> str:
        return self._doc.undo_label() if self._doc else ""

    def redo_label(self) -> str:
        return self._doc.redo_label() if self._doc else ""

    # 旧名（注釈だけだった頃の呼び名）。テストや外部からの呼び出し用に残す。
    def undo_annot(self) -> None:
        self.undo()

    def redo_annot(self) -> None:
        self.redo()

    def can_undo_annot(self) -> bool:
        return self.can_undo()

    def can_redo_annot(self) -> bool:
        return self.can_redo()

    def edit_selection_text(self) -> None:
        if self._sel_xref is None or not self._doc:
            return
        index = self._sel_index
        if not self._doc.annot_is_textual(index, self._sel_xref):
            return
        style = self._doc.annot_textstyle(index, self._sel_xref) or {}
        if style.get("kind") == "FreeText":
            # テキスト注釈はダイアログではなく、注釈の位置でその場編集する
            self._begin_text_edit_existing(index, self._sel_xref)
            return
        # 付箋（アイコン注釈）は矩形が小さすぎるため従来どおりダイアログで編集
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

    def set_zoom(self, zoom: float, _keep_fit: bool = False) -> None:
        """倍率を指定する。手動指定なら「幅/縦に合わせる」の追従はやめる。"""
        if not _keep_fit:
            self._fit_mode = None
        zoom = max(0.1, min(zoom, 8.0))
        if not self._doc or not self._doc.is_open:
            self._zoom = zoom
            return
        anchor = self._index  # ズーム後も同じページを画面に保つ
        self._zoom = zoom
        self._resize_all()
        self.set_page(anchor)
        if self._text_editor is not None:
            # 入力中の欄も新しい倍率の位置・文字の大きさに合わせ直す
            self._apply_text_editor_style()
            if self._text_ctx and self._text_ctx.get("xref") is not None:
                try:
                    self._text_editor.set_backdrop(self._doc.render_page_without_annots(
                        self._text_ctx["index"], self._zoom))
                except Exception:  # noqa: BLE001
                    pass
        if self._form_mode:
            # 旧位置の overlay を即座に消し、ズームが落ち着いてから1回だけ
            # 作り直す（毎ノッチ 270個を再生成して固まるのを防ぐ）。入力中の
            # 欄は控えておき、作り直したらそこへカーソルを戻す（以前は打った
            # 文字の続きがどこにも入らず消えていた）。
            self._remember_focused_field()
            self._clear_form_overlays()
            self._overlay_rebuild_timer.start()
        self.zoom_changed.emit(self._zoom)

    def fit_width(self) -> None:
        """ページの幅がビューポートに収まる倍率にする（幅に合わせる）。"""
        self._fit_mode = "width"
        if not self._doc or not self._doc.is_open:
            return
        w, _ = self._doc.page_pixel_size(self._index, zoom=1.0)
        if w:
            # 見開き時は横に self._cols ページ＋ページ間余白が並ぶ
            margin = PAGE_SPACING * (self._cols + 2) + 8
            # ウィンドウが未表示だとビューポートが極小のことがある。そのまま
            # 割ると極端な倍率になるので下限を置き、表示後に合わせ直す。
            avail = max(120, self.viewport().width() - margin)
            self._fit_page_size = self._doc.page_pixel_size(self._index, zoom=1.0)
            self.set_zoom(avail / (w * self._cols), _keep_fit=True)

    def fit_height(self) -> None:
        """ページ全体が縦に収まる倍率にする（縦に合わせる）。"""
        self._fit_mode = "height"
        if not self._doc or not self._doc.is_open:
            return
        _, h = self._doc.page_pixel_size(self._index, zoom=1.0)
        if h:
            avail = max(160, self.viewport().height() - PAGE_SPACING * 2)
            self._fit_page_size = self._doc.page_pixel_size(self._index, zoom=1.0)
            self.set_zoom(max(0.1, avail / h), _keep_fit=True)

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

    def _remember_focused_field(self) -> None:
        from PySide6.QtWidgets import QApplication

        w = QApplication.focusWidget()
        while w is not None and w not in self._form_overlays:
            w = w.parentWidget()
        if w is None:
            return
        pos = w.cursorPosition() if isinstance(w, QLineEdit) else None
        self._refocus = (w.property("trv_page"), w.property("trv_field"), pos)

    def _restore_focused_field(self) -> None:
        target = getattr(self, "_refocus", None)
        self._refocus = None
        if not target:
            return
        page, name, pos = target
        for ctl in self._form_overlays:
            if ctl.property("trv_page") == page and ctl.property("trv_field") == name:
                ctl.setFocus()
                if pos is not None and isinstance(ctl, QLineEdit):
                    ctl.setCursorPosition(min(pos, len(ctl.text())))
                return

    def _clear_form_overlays(self) -> None:
        for w in self._form_overlays:
            w.setParent(None)
            w.deleteLater()
        self._form_overlays.clear()

    def _build_form_overlays(self) -> None:
        """入力欄の overlay を作る。今見ているページを先に、残りは次の周回で。

        欄が数百あるフォームは全部作るのに 0.3〜0.5 秒かかる。開いた直後や
        拡大縮小の後は、まず見えているページの欄を使えるようにする。作る順は
        ページ順を回したもの（今のページ→後ろ→先頭から前）なので、Tab で
        回る順はページ順のまま変わらない。
        """
        # 作り直しで入力中の欄が消えても、作り直した欄へカーソルを戻す
        self._remember_focused_field()
        self._clear_form_overlays()
        self._overlay_gen = getattr(self, "_overlay_gen", 0) + 1
        if not self._form_mode or not self._doc or not self._doc.is_open:
            return
        n = min(self._doc.page_count, len(self._labels))
        if n <= 0:
            return
        # 入力中の欄があるページ（無ければ今のページ）から。カーソルをすぐ戻せる
        refocus = getattr(self, "_refocus", None)
        if refocus and isinstance(refocus[0], int) and 0 <= refocus[0] < n:
            first = refocus[0]
        else:
            first = max(0, min(self._index, n - 1))
        order = [(first + k) % n for k in range(n)]
        self._build_page_overlays(order[0])
        self._restore_focused_field()
        self._build_rest_overlays(self._overlay_gen, order[1:], first != 0)

    def _build_rest_overlays(self, gen: int, pages: list, reorder: bool) -> None:
        """残りのページを 1 ページずつ、入力の合間に作る（画面を止めない）。"""
        if gen != self._overlay_gen or not self._form_mode:
            return   # その後に作り直しが始まった・フォーム入力を止めた
        if not self._doc or not self._doc.is_open:
            return
        if not pages:
            if reorder:
                self._fix_form_tab_order()
            return
        if pages[0] < len(self._labels):
            self._build_page_overlays(pages[0])
        # 0 だと同じ周回でまとめて処理されてしまう（入力が割り込めない）ので 1ms。
        # self に結びつけて、タブを閉じたら呼ばれないように
        QTimer.singleShot(1, self, lambda: self._build_rest_overlays(gen, pages[1:], reorder))

    def _fix_form_tab_order(self) -> None:
        """途中のページから作ったときは、Tab の順をページ順に並べ直す。

        作った順のままだと、先に作ったページの手前（＝最後に作ったページの
        最後の欄）で Tab が画面の外（リボン）へ抜けてしまう。
        """
        ordered = sorted((c for c in self._form_overlays
                          if c.focusPolicy() & Qt.FocusPolicy.TabFocus),
                         key=lambda c: c.property("trv_page") or 0)
        for a, b in zip(ordered, ordered[1:]):
            QWidget.setTabOrder(a, b)

    def _build_page_overlays(self, index: int) -> None:
        """指定ページの overlay を作って self._form_overlays に足す。

        （このページの既存 overlay の掃除は呼び出し側の責務。）
        """
        label = self._labels[index]
        try:
            fields = self._doc.page_fields(index)
        except Exception:  # noqa: BLE001
            fields = []
        # 選択欄（コンボ）と同じ位置に重なる文字欄は作らない。フォームが
        # 「押されたら本物の選択欄へ渡す」ための見せかけの欄で、上に被さると
        # 選択欄が押せなかった（都道府県）。
        combo_rects = [f["rect"] for f in fields if f.get("kind") in ("combo", "list")]
        for field in fields:
            # 画面に出ない欄（PDF の Hidden=1 / 印刷のみ=3）は編集オーバー
            # レイを作らない。官公庁フォームの相棒 _PRINT 欄（印刷のみ）や
            # 選択ペアの隠れている側がこれに当たり、重なりや誤入力を防ぐ。
            # ただしコンボ/リストは「選択できる部品を消さない」ため常に描く
            # （都道府県コンボのように稀に Hidden 指定でも操作が要る欄がある）。
            if (int(field.get("display", 0)) in (1, 3)
                    and field.get("kind") not in ("combo", "list")):
                continue
            rules = field.get("rules") or {}
            if field.get("kind") not in ("combo", "list") and (
                    rules.get("focus_to")
                    or any(abs(r.x0 - field["rect"].x0) < 1 and abs(r.y0 - field["rect"].y0) < 1
                           and abs(r.x1 - field["rect"].x1) < 1 for r in combo_rects)):
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
            # xref ではなく矩形で束ねる。クリック時に「その矩形で今表示中の
            # 対象」を引き直すので、選択/解除で overlay を作り直さずに済む。
            ctl.clicked.connect(
                lambda checked=False, i=index, r=field["rect"]:
                self._click_govform_cell(i, r))
        elif kind == "checkbox":
            ctl = _FormCheckBox(label)
            checked = str(value) not in ("", "Off", "/Off", "No", "false", "False", "0")
            ctl.setChecked(checked)
            ctl.toggled.connect(
                lambda on, i=index, x=xref, n=field["name"]: self._commit_field(i, x, on, n))
        elif kind in ("combo", "list"):
            ctl = _FormComboBox(label)
            ctl.setEditable(kind == "combo")
            ctl.addItems(field["choices"])
            if value and value not in field["choices"]:
                ctl.addItem(str(value))
            ctl.setCurrentText(str(value))
            ctl.currentTextChanged.connect(
                lambda text, i=index, x=xref, n=field["name"]:
                self._commit_combo(i, x, text, n))
        elif field.get("multiline"):  # 複数行のテキスト欄
            ctl = _FormTextArea(label, maxlen=field.get("maxlen") or 0)
            ctl.setPlainText(str(value))
            ctl.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            ctl.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            ctl.committed.connect(
                lambda i=index, x=xref, c=ctl, n=field["name"]: self._commit_field(
                    i, x, c.toPlainText(), n))
        else:  # text / その他 → テキスト入力（radio は上の govform 分岐かここに来る）
            if field.get("comb") and field.get("maxlen"):
                # コム欄: 1文字1マス。全角/半角が混ざってもマス中央に描画する
                ctl = _CombLineEdit(field["maxlen"], label, align=field.get("align", 0))
            else:
                ctl = _AutoFitLineEdit(label)
                if field.get("maxlen"):
                    ctl.setMaxLength(field["maxlen"])
            ctl.setText(str(value))
            ctl.editingFinished.connect(
                lambda i=index, x=xref, c=ctl, n=field["name"]:
                self._commit_field(i, x, c.text(), n))
            if field.get("password"):
                ctl.setEchoMode(QLineEdit.EchoMode.Password)   # 伏せ字の欄
            else:
                # 入力候補（以前に入れた内容）。欄に入ったときに付け、確定で覚える
                rules = field.get("rules") or {}
                ctl.setProperty("trv_hkey", rules.get("label") or field.get("label")
                                or field["name"])
                ctl.installEventFilter(self)
                ctl.editingFinished.connect(lambda c=ctl: self._remember_entry(c))
        ctl.setGeometry(round(x0), round(y0), w, h)
        # どの欄か（ズームで作り直したあと、入力中の欄へカーソルを戻すため）
        ctl.setProperty("trv_field", field["name"])
        ctl.setProperty("trv_page", index)
        self._apply_field_rules(ctl, field)

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
        elif isinstance(ctl, _FormComboBox):
            ctl.setFont(f)
            # 選択欄（ドロップダウン）はテキスト入力欄と見分けられるよう、
            # やや濃い地色＋右端に区切り線付きのボタン領域を出す（▽は自前描画）。
            # padding-right で本文が▽に重ならないよう右側を空ける。
            ctl.setStyleSheet(
                "QComboBox { background: #d7e6ff;"
                " border: 1px solid #5b82c8; padding: 0px 16px 0px 3px;"
                " color: #111; }"
                "QComboBox:disabled { background: #eeeeee; border: 1px solid #c4c4c4;"
                " color: #666; }"
                "QComboBox::drop-down { subcontrol-origin: padding;"
                " subcontrol-position: top right; width: 14px;"
                " border-left: 1px solid #5b82c8; background: #b9d0f2; }"
                "QComboBox QAbstractItemView {"
                " background: #ffffff; color: #111;"
                " selection-background-color: #7096d8; }")
        else:
            ctl.setFont(f)
            # Adobe 風の薄青ハイライト。半透明だと下に描画済みの PDF 側の
            # 文字が透けて二重に見えるため不透明にする
            ctl.setStyleSheet(
                "QLineEdit, QPlainTextEdit {"
                " background: #e1ebff;"
                " border: 1px solid #7096d8; padding: 0px 2px; color: #111; }"
                # 入力中の欄は、ほかの青い欄と見分けがつくようにする
                "QLineEdit:focus, QPlainTextEdit:focus {"
                " background: #fffbe6; border: 2px solid #1a73e8; }"
                # 書き換えられない欄（フォームの他の欄の選択で入力できない等）
                "QLineEdit[trv_ro=\"true\"], QPlainTextEdit[trv_ro=\"true\"] {"
                " background: #eeeeee; border: 1px solid #c4c4c4; color: #555; }"
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

    def _apply_field_rules(self, ctl, field) -> None:
        """フォームの入力補助と、書き換え禁止の欄を overlay に反映する。"""
        if field.get("readonly"):
            self._set_overlay_readonly(ctl, True)
        rules = field.get("rules") or {}
        hint = rules.get("hint")
        if hint:
            # 入力の注意（Acrobat ではフォームが吹き出しで出す）。マウスを
            # 載せたときと、欄に入ったときのステータスバーに出す
            ctl.setToolTip(hint)
            ctl.setProperty("trv_hint", hint)
            ctl.installEventFilter(self)
        if isinstance(ctl, QLineEdit) and (rules.get("digits") or rules.get("large_kana")):
            def fix(_text="", c=ctl, r=rules):
                QTimer.singleShot(0, lambda: self._clean_field_text(c, r))
            ctl.textEdited.connect(fix)

    @staticmethod
    def _set_overlay_readonly(ctl, readonly: bool) -> None:
        """書き換え禁止の欄にする／戻す（フォームの JS が切り替えることがある）。

        Acrobat と同じく、書き換えられない欄にはカーソルが入らず Tab でも止まらない。
        〇で選ぶ欄は押したときに確かめる（_click_govform_cell）。
        """
        if isinstance(ctl, _GovFormClickOverlay):
            return
        if isinstance(ctl, (QLineEdit, QPlainTextEdit)):
            ctl.setReadOnly(readonly)
            ctl.setFocusPolicy(Qt.FocusPolicy.NoFocus if readonly
                               else Qt.FocusPolicy.StrongFocus)
        else:
            ctl.setEnabled(not readonly)
        ctl.setProperty("trv_ro", readonly)
        ctl.style().unpolish(ctl)   # 灰色の見た目（trv_ro）に切り替える
        ctl.style().polish(ctl)
        ctl.update()

    def _apply_form_effects(self, index: int, effects, source: str | None) -> None:
        """フォームの JS が変えた欄（書き換え禁止・値）を、作り直さずに入力欄へ
        反映し、フォームが出す知らせを見せる。"""
        if effects is None:
            return
        anchor = None
        for ctl in list(self._form_overlays):
            try:
                if ctl.property("trv_page") != index:
                    continue
                name = ctl.property("trv_field")
            except RuntimeError:
                continue   # 破棄済み
            if name == source:
                anchor = ctl
            if name in effects.readonly:
                self._set_overlay_readonly(ctl, effects.readonly[name])
            if name in effects.values:
                self._show_overlay_value(ctl, effects.values[name])
        for tip in effects.tips:
            self.status_message.emit(str(tip).replace("\n", " "))
        if effects.tips and anchor is not None:
            from PySide6.QtWidgets import QToolTip
            QToolTip.showText(anchor.mapToGlobal(QPoint(0, anchor.height())),
                              "\n".join(effects.tips), anchor, anchor.rect(), 8000)
        for msg in effects.alerts:
            from PySide6.QtWidgets import QMessageBox
            box = QMessageBox(QMessageBox.Icon.Warning, "フォームからのお知らせ", str(msg),
                              QMessageBox.StandardButton.Ok, self)
            box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            box.setModal(False)   # 入力を止めない（閉じなくても続けられる）
            box.show()

    @staticmethod
    def _show_overlay_value(ctl, value: str) -> None:
        """欄の表示を value に合わせる（書き込みはしない＝確定の合図を出さない）。"""
        ctl.blockSignals(True)
        try:
            if isinstance(ctl, QLineEdit):
                ctl.setText(value)
            elif isinstance(ctl, QPlainTextEdit):
                ctl.setPlainText(value)
            elif isinstance(ctl, QComboBox):
                # 書き込まれるのは選択肢の値（例 "2"）、項目の表示は " 2" のことがある
                items = [ctl.itemText(i) for i in range(ctl.count())]
                hit = next((t for t in items if t == value), None) or next(
                    (t for t in items if t.strip() == value.strip()), value)
                ctl.setCurrentText(hit)
            elif isinstance(ctl, QCheckBox):
                ctl.setChecked(value not in ("", "Off", "/Off", "No", "false", "0"))
        finally:
            ctl.blockSignals(False)
        if isinstance(ctl, QLineEdit):
            ctl.textChanged.emit(ctl.text())   # 文字の大きさの自動調整だけ走らせる

    @staticmethod
    def _attach_completer(ctl) -> None:
        """以前に入れた内容を候補に出す（同じ項目で入れたものが先）。

        候補の部品は欄ごとに 1 つだけ作り、入るたびに一覧を入れ替える（部品を
        差し替えると、Qt が古い部品を使っている最中だと落ちる）。
        """
        from PySide6.QtCore import QStringListModel
        from PySide6.QtWidgets import QCompleter

        from . import form_history
        items = []
        if form_history.enabled() and not ctl.isReadOnly():
            items = form_history.history().suggestions(
                ctl.property("trv_hkey"),
                ctl.maxLength() if ctl.maxLength() < 32767 else 0)
        comp = ctl.completer()
        if comp is None:
            if not items:
                return
            comp = QCompleter(ctl)
            comp.setModel(QStringListModel(items, comp))
            comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            comp.setFilterMode(Qt.MatchFlag.MatchStartsWith)
            comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            comp.setMaxVisibleItems(8)
            ctl.setCompleter(comp)
        else:
            comp.model().setStringList(items)

    @staticmethod
    def _remember_entry(ctl) -> None:
        from . import form_history
        try:
            if not form_history.enabled() or ctl.isReadOnly():
                return
            form_history.history().add(ctl.text(), ctl.property("trv_hkey") or "",
                                       ctl.property("trv_field") or "")
        except RuntimeError:
            pass   # 欄が破棄済み

    @staticmethod
    def _clean_field_text(ctl, rules) -> None:
        """入力補助に合わない文字を直す（全角数字→半角・数字以外は入れない等）。"""
        try:
            if getattr(ctl, "composing", False):
                return   # IME の変換中は触らない（確定後にもう一度来る）
            text = ctl.text()
        except RuntimeError:
            return   # 欄が破棄済み
        clean = _clean_input(text, rules)
        if clean == text:
            return
        pos = len(_clean_input(text[:ctl.cursorPosition()], rules))
        ctl.setText(clean)
        ctl.setCursorPosition(min(pos, len(clean)))

    def _commit_field(self, index, xref, value, name=None) -> None:
        if not self._doc:
            return
        # 番号（xref）が保存などで付け直されていても、欄の名前で探して書き込む
        if self._doc.set_field_value(index, xref, value, name=name):
            self.form_changed.emit()
        else:
            # 黙って捨てない（以前は保存後の入力がこうして消えていた）
            self.status_message.emit(
                f"入力欄「{name or xref}」に値を書き込めませんでした")

    def _commit_combo(self, index, xref, text, name=None) -> None:
        """コンボ選択: 値を確定し、官公庁フォームなら JS 連動（_PRINT 転写・
        枝ラベル表示切替）も再現してからページを再描画する。"""
        if not self._doc:
            return
        committed = self._doc.set_field_value(index, xref, text, name=name)
        js_changed = False
        if self._doc.is_govform_js_eligible():
            js_changed = self._doc.commit_govform_combo(index, xref, text, name=name)
            effects = self._doc.last_form_effects
            if effects is not None and (effects.readonly or effects.values
                                        or effects.tips or effects.alerts):
                # コンボの選択の処理が終わってから（このコンボ自身の表示も
                # 変わることがあるので、選択の通知の中では触らない）
                QTimer.singleShot(0, self, lambda e=effects, i=index, n=name:
                                  self._apply_form_effects(i, e, n))
        if committed or js_changed:
            self.form_changed.emit()
        if js_changed:
            # 枝ラベル(都/道/府/県)の表示切替や _PRINT の値変化はページ再描画で
            # 反映される。overlay は作り直さない――作り直すと、ユーザーが今まさに
            # 操作しているコンボ自体を破棄・再生成してしまい、選択が確定しない
            # （＝「都道府県が選べない」）。枝ラベルはクリック対象ではないので
            # overlay の増減は不要。
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

    def _click_govform_cell(self, index: int, pdf_rect) -> None:
        """官公庁フォームの「〇で選択」セルをクリックしたときの処理。

        _01（選択）と _OFF（非選択）は同じ矩形を共有し、常にどちらか一方だけ
        表示される。矩形に紐づく「その時点で表示中のクリック対象」を毎回引き
        直して実行するので、select / deselect のどちらも overlay を再バインド
        せずに正しく動く。

        重要（体感速度）: ここで overlay 一式を作り直すと 4 ページ分＝約 270 個の
        ウィジェットが毎クリック再生成され、1 クリックごとに ~0.5 秒フリーズして
        「選択がうまく効かない」体感になる。セルの集合はクリックで変わらない
        （各セルは常に1つだけ表示ウィジェットを持つ）ため、overlay は作り直さず
        ページ再描画だけ行う（Adobe 同等の即応）。
        """
        if not self._doc or not self._doc.is_govform_js_eligible():
            return
        xref = self._doc.govform_click_target_at(index, pdf_rect)
        if xref is None:
            return
        if self._doc.field_is_readonly(index, xref):
            # Acrobat でも押せない（例: 公金受取口座で「2」を選ぶまで口座の種類は
            # 選べない）
            self.status_message.emit("この項目は今は選べません（フォームの他の欄の選択による）")
            return
        if self._doc.click_govform_widget(index, xref):
            self.form_changed.emit()
            self._after_annot(index)  # 再描画のみ（overlay は据え置き）

    # --- 構築・レイアウト ----------------------------------------------
    def _rebuild(self, fit: bool, target: int) -> None:
        self._discard_text_editor()  # 入力中の編集ボックスはページ再構築で捨てる
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
        if event.type() == QEvent.Type.FocusIn and obj is not self.viewport():
            hint = obj.property("trv_hint") if hasattr(obj, "property") else None
            if hint:
                self.status_message.emit(str(hint).replace("\n", " "))
            if (isinstance(obj, QLineEdit) and obj.property("trv_hkey")
                    and event.reason() != Qt.FocusReason.PopupFocusReason):
                # 候補の一覧を閉じて戻ってきたときは触らない（一覧の処理の最中に
                # 差し替えると落ちる）
                self._attach_completer(obj)
            return False
        if obj is self.viewport():
            et = event.type()
            # 押下・ダブルクリックの両方を拾う（連続中クリックの取りこぼし対策）
            if et in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
                if self._auto_active:
                    # オートスクロール中はどのボタンでも停止（ブラウザと同様）
                    self._stop_autoscroll()
                    return True
                if (self._text_editor is not None
                        and event.button() == Qt.MouseButton.LeftButton):
                    self._text_editor._finish()   # ページの外のクリックでも確定
                    return True
                if event.button() == Qt.MouseButton.MiddleButton:
                    self._start_autoscroll(event.position().toPoint())
                    return True
        return super().eventFilter(obj, event)

    def _auto_marker_widget(self) -> QLabel:
        """オートスクロールの起点マーカー（丸に上下左右の矢印）。有効中の目印。"""
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
        # 上下左右の4方向（どちらへも動かせることが見て分かるように）
        p.drawPolygon(QPolygon([QPoint(c, 6), QPoint(c - 5, 13), QPoint(c + 5, 13)]))
        p.drawPolygon(QPolygon([QPoint(c, size - 6), QPoint(c - 5, size - 13),
                                QPoint(c + 5, size - 13)]))
        p.drawPolygon(QPolygon([QPoint(6, c), QPoint(13, c - 5), QPoint(13, c + 5)]))
        p.drawPolygon(QPolygon([QPoint(size - 6, c), QPoint(size - 13, c - 5),
                                QPoint(size - 13, c + 5)]))
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
        self.viewport().setCursor(Qt.CursorShape.SizeAllCursor)
        self._auto_timer.start()

    def _stop_autoscroll(self) -> None:
        self._auto_active = False
        self._auto_timer.stop()
        if getattr(self, "_auto_marker", None) is not None:
            self._auto_marker.hide()
        self.viewport().unsetCursor()

    def _auto_step(self, delta: int) -> int:
        """開始点からのずれを、1 tick ぶんのスクロール量にする。

        デッドゾーンの内側では動かさず、外に出た分だけ距離に比例して速くする。
        """
        if abs(delta) <= self.AUTO_DEADZONE:
            return 0
        offset = delta - self.AUTO_DEADZONE if delta > 0 else delta + self.AUTO_DEADZONE
        return int(offset / self.AUTO_DIVISOR)

    def _auto_scroll_tick(self) -> None:
        # 現在のカーソル位置（ビューポート基準）と開始点の差で速度を決める。
        # 上下だけでなく左右にも動かす（拡大して横にはみ出したページ用）。
        pos = self.viewport().mapFromGlobal(QCursor.pos())
        for delta, bar in ((pos.y() - self._auto_origin.y(), self.verticalScrollBar()),
                           (pos.x() - self._auto_origin.x(),
                            self.horizontalScrollBar())):
            step = self._auto_step(delta)
            if step:
                bar.setValue(bar.value() + step)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.matches(QKeySequence.StandardKey.Copy) and self.has_text_selection():
            self.copy_selection()
            return
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.select_all_text()
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
        # 「幅/縦に合わせる」は選んだ瞬間だけでなく、ウィンドウやパネルの幅が
        # 変わっても追従させる（一般的な PDF リーダーと同じ挙動）。合わせ直しが
        # スクロールバーの出入りを起こして再び resize が飛ぶので再入を止める。
        if self._fit_mode and not self._in_fit:
            self._in_fit = True
            try:
                if self._fit_mode == "width":
                    self.fit_width()
                else:
                    self.fit_height()
            finally:
                self._in_fit = False
        self._place_resume_bar()
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
                # 画面外はラベルの参照を外す（実体は描画キャッシュが持つ）
                label.clear()
                self._rendered.discard(i)
        self._prefetch_timer.start()

    #: 先読みするページ数（前後それぞれ）
    PREFETCH = 2

    def _prefetch_around(self) -> None:
        """表示中の前後のページを、描画キャッシュに載せておく。

        スクロールが止まってから動くので、操作の邪魔はしない。描くだけで
        ラベルには渡さない（画面に出るときはキャッシュから即座に返る）。
        """
        if not self._doc or not self._doc.is_open or not self._rendered:
            return
        lo, hi = min(self._rendered), max(self._rendered)
        targets = [i for i in range(lo - self.PREFETCH, hi + self.PREFETCH + 1)
                   if 0 <= i < self._doc.page_count and i not in self._rendered]
        for i in targets:
            try:
                self._doc.render_page(i, zoom=self._zoom)
            except Exception:  # noqa: BLE001  先読みは失敗しても無視
                return

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
            self._refit_for_page()
            if self._resume_bar is not None and self._resume_bar.page == index:
                self.dismiss_resume_offer()   # 自分でそこまで来たなら案内は不要
            self.page_changed.emit(index)

    def _refit_for_page(self) -> None:
        """ページごとに大きさが違う PDF で、送った先の大きさに合わせ直す。

        「幅/縦に合わせる」は現在ページの寸法で倍率を決めるので、A4 と A3 が
        混ざった文書ではページを送るたびに幅がずれる。大きさが変わったときだけ
        測り直す（同じ大きさなら何もしないので、普通の文書では無駄がない）。
        """
        if not self._fit_mode or self._in_fit:
            return
        if not self._doc or not self._doc.is_open:
            return
        size = self._doc.page_pixel_size(self._index, zoom=1.0)
        if size == self._fit_page_size:
            return
        self._in_fit = True
        try:
            if self._fit_mode == "width":
                self.fit_width()
            else:
                self.fit_height()
        finally:
            self._in_fit = False
