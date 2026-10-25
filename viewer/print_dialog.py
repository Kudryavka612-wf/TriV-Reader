"""Adobe Reader 風の印刷ダイアログ。

左側に設定（プリンター/部数/対象ページ/サイズ処理/向き/グレースケール）、
右側にプレビュー。OK で選択内容を options() として返し、実際の印刷処理は
呼び出し側（main_window.print_document）が行う。
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QPageLayout, QPageSize, QPainter, QPen, QPixmap
from PySide6.QtPrintSupport import QPrinter, QPrinterInfo
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
)

from .mw_print import paper_for_page, place_on_paper, select_pages, svg_is_safe
from .util import parse_ranges

#: 「印刷する内容」の選択肢（PdfDocument.PRINT_* と同じ値）
CONTENT_CHOICES = [
    ("all", "文書と注釈（すべて）"),
    ("filled", "文書と、書き込んだ文字・スタンプ（ハイライト等は除く）"),
    ("doc", "文書のみ（入力欄の値は残す）"),
    ("fields", "書き込んだ文字と入力値だけ（印刷済みの用紙に刷る）"),
]

# サイズ処理モード
SIZE_FIT, SIZE_ACTUAL, SIZE_SHRINK, SIZE_CUSTOM = range(4)

#: 1 枚に載せるページ数の選択肢
NUP_CHOICES = (1, 2, 4, 6, 9)


def nup_slots(rect: QRectF, count: int, page_w: float, page_h: float) -> list[QRectF]:
    """rect を count 個の区画に分ける（読む順＝左上から右へ）。

    列×行の割り方は「そのページがいちばん大きく載る」ものを選ぶ。縦のページを
    2 面付けするときは、90 度回して上下に並べた方が大きく載る（一般的な
    プリンタードライバーの 2 ページ/枚と同じ見え方）ので、回転も含めて比べる。
    """
    if count <= 1:
        return [rect]
    page_w = max(1.0, page_w)
    page_h = max(1.0, page_h)
    best = None
    for cols in range(1, count + 1):
        if count % cols:
            continue
        rows = count // cols
        w = rect.width() / cols
        h = rect.height() / rows
        scale = max(min(w / page_w, h / page_h), min(w / page_h, h / page_w))
        if best is None or scale > best[0]:
            best = (scale, cols, rows)
    _s, cols, rows = best
    w = rect.width() / cols
    h = rect.height() / rows
    return [QRectF(rect.x() + c * w, rect.y() + r * h, w, h)
            for r in range(rows) for c in range(cols)]


class PrintDialog(QDialog):
    """印刷設定＋プレビュー。"""

    PREVIEW_W = 300   # プレビューの最小幅。ダイアログを広げれば追従して大きくなる

    def __init__(self, doc, current_index: int, settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("印刷")
        self._doc = doc
        self._settings = settings
        self._preview_index = max(0, min(current_index, doc.page_count - 1))
        self._current_index = self._preview_index

        root = QHBoxLayout(self)

        # ── 左: 設定 ────────────────────────────────────────────
        left = QVBoxLayout()
        form = QFormLayout()
        self.printer_combo = QComboBox()
        default = QPrinterInfo.defaultPrinter()
        names = [p.printerName() for p in QPrinterInfo.availablePrinters()]
        self.printer_combo.addItems(names)
        # 前回使ったプリンターを憶えておく（毎回選び直さなくて済むように）
        last = settings.value("print/printer", "", str) if settings else ""
        if last in names:
            self.printer_combo.setCurrentText(last)
        elif default and default.printerName() in names:
            self.printer_combo.setCurrentText(default.printerName())
        # ドライバーの設定（給紙トレイ・用紙の種類・ホチキス等）はプリンター
        # ごとに違うので、Windows の印刷ダイアログ（詳細設定）に任せる
        self.props_button = QPushButton("プロパティ…")
        self.props_button.setToolTip(
            "給紙トレイ・用紙の種類・部門コードなど、プリンターの詳細設定を開きます")
        self.props_button.clicked.connect(self._open_printer_properties)
        row = QHBoxLayout()
        row.addWidget(self.printer_combo, 1)
        row.addWidget(self.props_button)
        form.addRow("プリンター:", row)
        self.copies = QSpinBox()
        self.copies.setRange(1, 999)
        self.copies.setValue(1)
        form.addRow("部数:", self.copies)

        # 用紙。既定はプリンター任せ。「PDF のページサイズに合わせる」は
        # B5 の届出書やはがきなど、用紙が A4 でないものをそのまま出すため。
        self.paper_combo = QComboBox()
        form.addRow("用紙:", self.paper_combo)

        # 両面。対応していないプリンターでは選べないようにする。
        self.duplex_combo = QComboBox()
        self.duplex_combo.addItem("片面", "none")
        self.duplex_combo.addItem("両面（長辺とじ）", "long")
        self.duplex_combo.addItem("両面（短辺とじ）", "short")
        form.addRow("両面:", self.duplex_combo)

        self.collate_check = QCheckBox("部ごとにまとめて印刷（1部ずつ）")
        self.collate_check.setChecked(True)
        self.collate_check.setToolTip(
            "2 部以上刷るとき、1・2・3／1・2・3 の順に出します。"
            "外すと 1・1／2・2／3・3 の順（同じページをまとめて刷る）です。")
        form.addRow("", self.collate_check)
        left.addLayout(form)

        # 印刷するページ
        gb_pages = QGroupBox("印刷するページ")
        v = QVBoxLayout(gb_pages)
        self.rb_all = QRadioButton("すべて")
        # プレビューを ◀▶ で送っても「現在のページ」は開いている画面のページ。
        # どのページが刷られるのか迷わないよう、番号を添える。
        self.rb_current = QRadioButton(f"現在のページ（{self._current_index + 1}）")
        row = QHBoxLayout()
        self.rb_range = QRadioButton("ページ指定:")
        self.range_edit = QLineEdit()
        self.range_edit.setPlaceholderText("例: 1-3, 5, 8")
        self.range_edit.setToolTip("ページ番号をカンマ区切りで。範囲は 1-3 のように書きます")
        row.addWidget(self.rb_range)
        row.addWidget(self.range_edit)
        self.rb_all.setChecked(True)
        v.addWidget(self.rb_all)
        v.addWidget(self.rb_current)
        v.addLayout(row)
        # 書き方の誤りは見えるところに出す（印刷ボタンが押せない理由が分かるように）
        self.range_error_label = QLabel()
        self.range_error_label.setStyleSheet("color: #d64545;")
        self.range_error_label.setWordWrap(True)
        self.range_error_label.hide()
        v.addWidget(self.range_error_label)
        self._page_group = QButtonGroup(self)
        for rb in (self.rb_all, self.rb_current, self.rb_range):
            self._page_group.addButton(rb)
        # 奇数/偶数だけ・逆順。両面に対応していないプリンターで手差し両面を
        # するときの定番（奇数を刷る → 裏返す → 偶数を逆順で刷る）。
        row = QHBoxLayout()
        self.subset_combo = QComboBox()
        self.subset_combo.addItem("すべてのページ", "all")
        self.subset_combo.addItem("奇数ページのみ", "odd")
        self.subset_combo.addItem("偶数ページのみ", "even")
        self.subset_combo.setToolTip(
            "両面に対応していないプリンターで手差し両面をするときに。\n"
            "奇数ページを刷ってから紙を裏返し、偶数ページを刷ります。")
        self.reverse_check = QCheckBox("逆順に印刷")
        self.reverse_check.setToolTip(
            "最後のページから刷ります。排紙が裏向きになるプリンターで、"
            "刷り上がりをページ順に揃えたいときに。")
        row.addWidget(self.subset_combo)
        row.addWidget(self.reverse_check)
        row.addStretch(1)
        v.addLayout(row)
        left.addWidget(gb_pages)

        # ページサイズ処理
        gb_size = QGroupBox("ページサイズ処理")
        v = QVBoxLayout(gb_size)
        self.size_combo = QComboBox()
        self.size_combo.addItems([
            "合わせる（用紙に合わせて拡大/縮小）",
            "実際のサイズ",
            "特大ページを縮小",
            "カスタム倍率",
        ])
        self.size_combo.setCurrentIndex(
            int(settings.value("print/size_mode", SIZE_SHRINK, int)))
        v.addWidget(self.size_combo)
        row = QHBoxLayout()
        row.addWidget(QLabel("倍率:"))
        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(10.0, 400.0)
        self.scale_spin.setSuffix(" %")
        self.scale_spin.setValue(float(settings.value("print/custom_scale", 100.0, float)))
        row.addWidget(self.scale_spin)
        row.addStretch(1)
        v.addLayout(row)
        left.addWidget(gb_size)

        # 向き
        gb_ori = QGroupBox("向き")
        row = QHBoxLayout(gb_ori)
        self.rb_auto = QRadioButton("自動（縦/横）")
        self.rb_portrait = QRadioButton("縦")
        self.rb_landscape = QRadioButton("横")
        self.rb_auto.setChecked(True)
        for rb in (self.rb_auto, self.rb_portrait, self.rb_landscape):
            row.addWidget(rb)
        left.addWidget(gb_ori)

        # 割り付け（1 枚に複数ページ）。紙を減らしたいときに使う。
        row = QHBoxLayout()
        row.addWidget(QLabel("割り付け:"))
        self.nup_combo = QComboBox()
        for n in NUP_CHOICES:
            self.nup_combo.addItem("1ページ/枚（そのまま）" if n == 1
                                   else f"{n}ページ/枚", n)
        self.nup_combo.setToolTip(
            "1 枚の紙に何ページ載せるか。2 以上のときは、それぞれの区画に"
            "収まるよう自動で縮小します（ページサイズ処理は使いません）。")
        row.addWidget(self.nup_combo)
        row.addStretch(1)
        left.addLayout(row)

        self.gray_check = QCheckBox("グレースケール（白黒）で印刷")
        left.addWidget(self.gray_check)
        row = QHBoxLayout()
        row.addWidget(QLabel("印刷する内容:"))
        self.content_combo = QComboBox()
        for key, label in CONTENT_CHOICES:
            self.content_combo.addItem(label, key)
        self.content_combo.setToolTip(
            "ハイライトやペン・付箋などの注釈を刷るかどうか。\n"
            "「書き込んだ文字・スタンプ」は、テキストツールで入れた文字と承認印などを残し、\n"
            "赤入れ（ハイライト・ペン・付箋）だけを外します。\n"
            "「印刷済みの用紙に刷る」は、紙面を刷らず、入れた文字と入力欄の値だけを刷ります。")
        row.addWidget(self.content_combo, 1)
        left.addLayout(row)

        # 印刷品質（ラスタライズの解像度）。プリンタ解像度より粗く描くと、
        # 紙面へ拡大されるぶんだけ文字の輪郭が甘くなる。
        row = QHBoxLayout()
        row.addWidget(QLabel("品質:"))
        self.quality_combo = QComboBox()
        # ベクターはプリンタのネイティブ解像度で出るので、拡大してもぼやけず
        # データ量も小さい。画像化は、ベクターで崩れる特殊な PDF 向けの逃げ道。
        self.quality_combo.addItem("ベクター（文字がくっきり・既定）", "vector")
        self.quality_combo.addItem("画像化 600dpi（高品質）", 600)
        self.quality_combo.addItem("画像化 300dpi（軽い）", 300)
        self.quality_combo.addItem("画像化 プリンター解像度（最高精細・重い）", 0)
        saved = settings.value("print/quality", "vector")
        if saved not in ("vector",):
            try:
                saved = int(saved)
            except (TypeError, ValueError):
                saved = "vector"
        idx = self.quality_combo.findData(saved)
        self.quality_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.quality_combo.setToolTip(
            "ベクターは拡大してもぼやけず、印刷データも小さくなります。\n"
            "図版が崩れるときだけ画像化を選んでください。")
        row.addWidget(self.quality_combo)
        row.addStretch(1)
        left.addLayout(row)
        left.addStretch(1)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        self.print_button = bb.button(QDialogButtonBox.StandardButton.Ok)
        self.print_button.setText("印刷")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        left.addWidget(bb)
        root.addLayout(left, 0)

        # ── 右: プレビュー ──────────────────────────────────────
        # ダイアログを広げたぶんプレビューも大きくなる（300px 固定だと、
        # 大きくしても灰色の余白が増えるだけで細部が見えない）。
        right = QVBoxLayout()
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(self.PREVIEW_W, int(self.PREVIEW_W * 1.4142))
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Expanding)
        self.preview.setStyleSheet("background: #888; border: 1px solid #666;")
        right.addWidget(self.preview, 1)
        self._preview_timer = QTimer(self)      # リサイズ中の描き直しをまとめる
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(60)
        self._preview_timer.timeout.connect(self._refresh)
        nav = QHBoxLayout()
        btn_prev = QPushButton("◀")
        btn_next = QPushButton("▶")
        self.page_pos_label = QLabel()
        self.page_pos_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nav.addStretch(1)
        nav.addWidget(btn_prev)
        nav.addWidget(self.page_pos_label)
        nav.addWidget(btn_next)
        nav.addStretch(1)
        right.addLayout(nav)
        root.addLayout(right, 1)
        self.resize(900, 700)

        # 配線: 設定変更でプレビュー更新
        btn_prev.clicked.connect(lambda: self._step_preview(-1))
        btn_next.clicked.connect(lambda: self._step_preview(1))
        self.size_combo.currentIndexChanged.connect(self._refresh)
        self.scale_spin.valueChanged.connect(self._refresh)
        for rb in (self.rb_auto, self.rb_portrait, self.rb_landscape):
            rb.toggled.connect(self._refresh)
        self.gray_check.toggled.connect(self._refresh)
        self.size_combo.currentIndexChanged.connect(
            lambda i: self.scale_spin.setEnabled(i == SIZE_CUSTOM))
        self.scale_spin.setEnabled(self.size_combo.currentIndex() == SIZE_CUSTOM)
        self.printer_combo.currentTextChanged.connect(self._on_printer_changed)
        self.paper_combo.currentIndexChanged.connect(self._refresh)
        self.nup_combo.currentIndexChanged.connect(self._refresh)
        self.copies.valueChanged.connect(self._refresh)   # 用紙の枚数の表示
        self.content_combo.currentIndexChanged.connect(self._on_content_changed)
        # ページ指定欄に打ち始めたら、そのラジオを選ぶ（打ったのに「すべて」の
        # まま刷ってしまうのを防ぐ）。中身は打つたびに検査して、誤りなら
        # 印刷ボタンを押せなくする。
        self.range_edit.textEdited.connect(lambda _t: self.rb_range.setChecked(True))
        self.range_edit.textChanged.connect(self._validate_range)
        for rb in (self.rb_all, self.rb_current, self.rb_range):
            rb.toggled.connect(self._validate_range)
        # 刷るページが変わったらプレビューも（刷るページだけを送れるように）
        self.range_edit.textChanged.connect(self._on_pages_changed)
        for w in (self.rb_all, self.rb_current, self.rb_range):
            w.toggled.connect(self._on_pages_changed)
        self.subset_combo.currentIndexChanged.connect(self._on_pages_changed)
        self.reverse_check.toggled.connect(self._on_pages_changed)

        self._printer_name = None
        self._sync_printer_caps()
        if settings:
            # 両面は、前に選んだものが無ければプリンター（ドライバー）の既定に
            # 従う。会社の複合機は既定が両面のことが多い。
            duplex = settings.value("print/duplex", "", str) or self._driver_duplex()
            idx = self.duplex_combo.findData(duplex)
            if self.duplex_combo.isEnabled():
                self.duplex_combo.setCurrentIndex(max(0, idx))
            self.collate_check.setChecked(
                settings.value("print/collate", True, bool))
            idx = self.nup_combo.findData(int(settings.value("print/nup", 1, int)))
            self.nup_combo.setCurrentIndex(max(0, idx))
            content = settings.value("print/content", "", str)
            if not content:   # 以前のチェックボックス（注釈も印刷する）から読み替え
                content = "all" if settings.value("print/annots", True, bool) else "doc"
            idx = self.content_combo.findData(content)
            self.content_combo.setCurrentIndex(max(0, idx))
            ori = settings.value("print/orientation", "auto", str)
            {"portrait": self.rb_portrait, "landscape": self.rb_landscape}.get(
                ori, self.rb_auto).setChecked(True)
            self.gray_check.setChecked(settings.value("print/grayscale", False, bool))
        self._preview_pos = 0
        self._preview_pages = self._pages_to_print()
        if self._current_index in self._preview_pages:
            self._preview_pos = self._preview_pages.index(self._current_index)
        self._refresh()

    def _on_printer_changed(self, _name: str) -> None:
        self._sync_printer_caps()
        self._refresh()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        self._preview_timer.start()

    def _driver_duplex(self) -> str:
        info = QPrinterInfo.printerInfo(self.printer_combo.currentText())
        if info.isNull():
            return "none"
        return {QPrinter.DuplexMode.DuplexLongSide: "long",
                QPrinter.DuplexMode.DuplexShortSide: "short",
                QPrinter.DuplexMode.DuplexAuto: "long"}.get(
                    info.defaultDuplexMode(), "none")

    def printer(self) -> QPrinter:
        """選んだプリンターの QPrinter（「プロパティ」で変えた詳細設定ごと）。"""
        return self._printer()

    def _open_printer_properties(self) -> None:
        """Windows の印刷ダイアログ（詳細設定）を開き、変えた分をこちらにも映す。"""
        from PySide6.QtPrintSupport import QPrintDialog

        pr = self._printer()
        pr.setCopyCount(self.copies.value())
        dlg = QPrintDialog(pr, self)
        dlg.setWindowTitle("プリンターの詳細設定")
        if dlg.exec() != QPrintDialog.DialogCode.Accepted:
            return
        name = pr.printerName()
        if name and name != self.printer_combo.currentText():
            self.printer_combo.blockSignals(True)
            self.printer_combo.setCurrentText(name)
            self.printer_combo.blockSignals(False)
            self._printer_name = name   # 今の QPrinter（設定込み）をそのまま使う
            self._sync_printer_caps()
        self.copies.setValue(max(1, pr.copyCount()))
        idx = self.duplex_combo.findData({
            QPrinter.DuplexMode.DuplexLongSide: "long",
            QPrinter.DuplexMode.DuplexShortSide: "short"}.get(pr.duplex(), "none"))
        if idx >= 0 and self.duplex_combo.isEnabled():
            self.duplex_combo.setCurrentIndex(idx)
        size = pr.pageLayout().pageSize()
        i = self.paper_combo.findText(size.name())
        if i >= 0:
            self.paper_combo.setCurrentIndex(i)
        self._refresh()

    def range_error(self) -> str:
        """ページ指定の誤り（空文字なら問題なし）。"""
        if not self.rb_range.isChecked():
            return ""
        text = self.range_edit.text().strip()
        if not text:
            return "ページ番号を入力してください"
        try:
            pages = parse_ranges(text, self._doc.page_count)
        except ValueError as exc:
            return str(exc)
        return "" if pages else "該当するページがありません"

    def _validate_range(self, *_args) -> None:
        err = self.range_error()
        self.print_button.setEnabled(not err)
        self.print_button.setToolTip(err)
        shown = bool(err and self.range_edit.text().strip())
        self.range_edit.setStyleSheet("border: 2px solid #d64545;" if shown else "")
        self.range_error_label.setText(err if shown else "")
        self.range_error_label.setVisible(shown)

    def _on_content_changed(self, _i: int) -> None:
        self._release_stripped()
        self._refresh()

    def _pages_to_print(self) -> list[int]:
        """今の設定で刷るページ（0 始まり・刷る順）。指定が誤っていれば全ページ。"""
        try:
            pages = select_pages(self.options(), self._doc.page_count, self._current_index)
        except ValueError:
            pages = []
        return pages or list(range(self._doc.page_count))

    def _on_pages_changed(self, *_args) -> None:
        pages = self._pages_to_print()
        cur = self._preview_page()
        self._preview_pages = pages
        self._preview_pos = pages.index(cur) if cur in pages else 0
        self._refresh()

    def _preview_page(self) -> int:
        pages = getattr(self, "_preview_pages", None) or [self._preview_index]
        return pages[max(0, min(getattr(self, "_preview_pos", 0), len(pages) - 1))]

    # --- プレビューに使う文書（注釈なしのときは複製から描く）------------
    def _release_stripped(self) -> None:
        doc = getattr(self, "_stripped", None)
        if doc is not None:
            doc.close()
        self._stripped = None

    def _preview_doc(self):
        """プレビュー用の文書。印刷と同じ複製（印刷しない欄を除き、刷る中身を
        絞ったもの）を使う。手を入れずに刷れるときは元の文書。"""
        if getattr(self, "_stripped", None) is None:
            try:
                self._stripped = self._doc.print_copy(self.content_combo.currentData())
            except Exception:  # noqa: BLE001  作れなければ元のまま出す
                self._stripped = None
            if self._stripped is None:
                return self._doc
        return self._stripped

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        self._release_stripped()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        # accept()/reject() では closeEvent が飛ばないので、ここで後始末する
        # （プレビュー用に作った複製を残さない）。
        self._release_stripped()
        super().done(result)

    # --- プリンターの能力 -------------------------------------------------
    def _printer(self) -> QPrinter:
        """選択中のプリンターの QPrinter（用紙と余白を実際の値で得るため）。"""
        name = self.printer_combo.currentText()
        if getattr(self, "_printer_name", None) != name:
            info = QPrinterInfo.printerInfo(name)
            self._printer_obj = (
                QPrinter(info, QPrinter.PrinterMode.HighResolution)
                if not info.isNull()
                else QPrinter(QPrinter.PrinterMode.HighResolution))
            self._printer_name = name
        return self._printer_obj

    def _sync_printer_caps(self) -> None:
        """選んだプリンターに合わせて、用紙の一覧と両面の可否を作り直す。"""
        info = QPrinterInfo.printerInfo(self.printer_combo.currentText())
        prev = self.paper_combo.currentData()
        self.paper_combo.blockSignals(True)
        self.paper_combo.clear()
        self.paper_combo.addItem("プリンターの既定", None)
        self.paper_combo.addItem("PDF のページサイズに合わせる", "auto")
        seen = set()
        for size in ([] if info.isNull() else info.supportedPageSizes()):
            if size.name() in seen:
                continue
            seen.add(size.name())
            self.paper_combo.addItem(size.name(), size)
        saved = (self._settings.value("print/paper", "", str)
                 if self._settings else "")
        target = prev if prev is not None else (saved or None)
        idx = -1
        if isinstance(target, QPageSize):
            idx = self.paper_combo.findText(target.name())
        elif target == "auto" or saved == "auto":
            idx = self.paper_combo.findData("auto")
        elif saved:
            idx = self.paper_combo.findText(saved)
        self.paper_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.paper_combo.blockSignals(False)

        modes = [] if info.isNull() else info.supportedDuplexModes()
        can_duplex = any(m != QPrinter.DuplexMode.DuplexNone for m in modes)
        self.duplex_combo.setEnabled(can_duplex)
        if not can_duplex:
            self.duplex_combo.setCurrentIndex(0)
            self.duplex_combo.setToolTip("このプリンターは両面印刷に対応していません")
        else:
            self.duplex_combo.setToolTip("")

    def _paper_rects(self, nat_w: float, nat_h: float):
        """(用紙, 印刷可能領域) をポイントで返す。

        用紙は「プリンターの既定 / PDF に合わせる / 指定」で決まる。プレビューを
        A4 決め打ちにすると、B5 やはがきを選んだときに実際の刷り上がりと
        食い違うので、必ずプリンター側の値を使う。
        """
        pr = self._printer()
        choice = self.paper_combo.currentData()
        try:
            if isinstance(choice, QPageSize):
                pr.setPageSize(choice)
            elif choice == "auto":
                size = paper_for_page(nat_w, nat_h)
                if size is not None:
                    pr.setPageSize(size)
            landscape = (nat_w > nat_h if choice == "auto"
                         else self.rb_landscape.isChecked())
            pr.setPageOrientation(
                QPageLayout.Orientation.Landscape if landscape
                else QPageLayout.Orientation.Portrait)
        except Exception:  # noqa: BLE001  用紙を変えられないプリンターもある
            pass
        return (pr.paperRect(QPrinter.Unit.Point),
                pr.pageRect(QPrinter.Unit.Point))

    # --- 公開: 選択内容 --------------------------------------------------
    def options(self) -> dict:
        if self.rb_current.isChecked():
            mode = "current"
        elif self.rb_range.isChecked():
            mode = "range"
        else:
            mode = "all"
        if self.rb_portrait.isChecked():
            ori = "portrait"
        elif self.rb_landscape.isChecked():
            ori = "landscape"
        else:
            ori = "auto"
        return {
            "printer": self.printer_combo.currentText(),
            "copies": self.copies.value(),
            "page_mode": mode,
            "range_text": self.range_edit.text(),
            "size_mode": self.size_combo.currentIndex(),
            "custom_scale": self.scale_spin.value() / 100.0,
            "orientation": ori,
            "grayscale": self.gray_check.isChecked(),
            "quality": self.quality_combo.currentData(),
            "paper": self.paper_combo.currentData(),
            "nup": self.nup_combo.currentData(),
            "content": self.content_combo.currentData(),
            "annots": self.content_combo.currentData() == "all",
            "duplex": self.duplex_combo.currentData() if self.duplex_combo.isEnabled() else "none",
            "collate": self.collate_check.isChecked(),
            "subset": self.subset_combo.currentData(),
            "reverse": self.reverse_check.isChecked(),
        }

    def _on_accept(self) -> None:
        # 使った設定を記憶
        self._settings.setValue("print/size_mode", self.size_combo.currentIndex())
        self._settings.setValue("print/custom_scale", self.scale_spin.value())
        self._settings.setValue("print/quality", self.quality_combo.currentData())
        self._settings.setValue("print/printer", self.printer_combo.currentText())
        paper = self.paper_combo.currentData()
        self._settings.setValue(
            "print/paper",
            "auto" if paper == "auto" else (paper.name() if paper else ""))
        self._settings.setValue("print/duplex", self.duplex_combo.currentData())
        self._settings.setValue("print/collate", self.collate_check.isChecked())
        self._settings.setValue("print/nup", self.nup_combo.currentData())
        self._settings.setValue("print/content", self.content_combo.currentData())
        self._settings.setValue("print/orientation", self.options()["orientation"])
        self._settings.setValue("print/grayscale", self.gray_check.isChecked())
        self.accept()

    # --- プレビュー -------------------------------------------------------
    def _step_preview(self, step: int) -> None:
        """刷るページだけを送る（範囲や奇数・偶数を選んでいればそのページだけ）。"""
        nup = self.nup_combo.currentData() or 1
        n = len(self._preview_pages)
        self._preview_pos = max(0, min(self._preview_pos + step * nup, n - 1))
        self._refresh()

    @staticmethod
    def _draw_fitted(painter, pixmap, slot: QRectF,
                     page_w: float, page_h: float) -> None:
        """区画 slot にページを収まるように（必要なら 90 度回して）描く。"""
        pad = 2.0
        box = slot.adjusted(pad, pad, -pad, -pad)
        s_plain = min(box.width() / max(1.0, page_w), box.height() / max(1.0, page_h))
        s_rot = min(box.width() / max(1.0, page_h), box.height() / max(1.0, page_w))
        rotate = s_rot > s_plain
        s = max(s_plain, s_rot)
        tw, th = page_w * s, page_h * s
        cx, cy = box.center().x(), box.center().y()
        src = QRectF(0, 0, pixmap.width(), pixmap.height())
        if rotate:
            painter.save()
            painter.translate(cx, cy)
            painter.rotate(90)
            painter.drawPixmap(QRectF(-tw / 2, -th / 2, tw, th), pixmap, src)
            painter.restore()
        else:
            painter.drawPixmap(QRectF(cx - tw / 2, cy - th / 2, tw, th), pixmap, src)

    def _paper_is_landscape(self, nat_w: float, nat_h: float) -> bool:
        if self.rb_landscape.isChecked():
            return True
        if self.rb_portrait.isChecked():
            return False
        return False  # 自動: 用紙は縦のまま（内容側を回す）

    def _preview_pixmap(self, index: int, zoom: float) -> QPixmap:
        """プレビュー用の描画。実際の印刷と同じくベクターで描く。

        印刷がベクターなのにプレビューだけ画像だと、細い線や小さな文字の見え方が
        食い違う。QSvgRenderer が使えないときは従来どおり画像で描く。
        """
        try:
            from PySide6.QtSvg import QSvgRenderer

            svg = self._preview_doc().page_svg(index)
            # 印刷と同じ判断（重ね方を使うページは画像で描く）
            renderer = QSvgRenderer(svg.encode("utf-8")) if svg_is_safe(svg) else None
            if renderer is not None and renderer.isValid():
                box = renderer.viewBoxF()
                pm = QPixmap(max(1, int(box.width() * zoom)),
                             max(1, int(box.height() * zoom)))
                pm.fill(Qt.GlobalColor.white)
                p = QPainter(pm)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                renderer.render(p, QRectF(0, 0, pm.width(), pm.height()))
                p.end()
                return pm
        except Exception:  # noqa: BLE001
            pass
        return self._preview_doc().render_page(index, zoom=zoom)

    def _refresh(self) -> None:
        pages = getattr(self, "_preview_pages", None)
        if not pages:
            return   # 作りかけ（__init__ の途中）
        pos = max(0, min(self._preview_pos, len(pages) - 1))
        i = pages[pos]
        n = self._doc.page_count
        nup = self.nup_combo.currentData() or 1
        sheets = (len(pages) + nup - 1) // nup
        copies = self.copies.value()
        total = f"／ 用紙 {sheets * copies} 枚" if copies > 1 else ""
        self.page_pos_label.setText(
            f"{pos // nup + 1} / {sheets} 枚目（{i + 1} ページ）{total}")
        try:
            nat_w, nat_h = self._doc.page_pixel_size(i, 1.0)
            pix = self._preview_pixmap(i, 0.5)
        except Exception:  # noqa: BLE001
            return
        # 用紙と印刷可能領域は、選んでいるプリンターの実際の値を使う
        # （A4 決め打ちだと、B5 やはがきを選んだときに刷り上がりと食い違う）。
        paper, printable = self._paper_rects(nat_w, nat_h)
        paper_w_pt = max(1.0, paper.width())
        paper_h_pt = max(1.0, paper.height())
        # プレビュー画布：ラベルの中に収まる最大の大きさ（縦横比は用紙どおり）
        avail_w = max(self.PREVIEW_W, self.preview.width() - 16)
        avail_h = max(int(self.PREVIEW_W * 1.4142), self.preview.height() - 16)
        pw = int(min(avail_w, avail_h * paper_w_pt / paper_h_pt))
        ph = max(1, int(pw * paper_h_pt / paper_w_pt))
        canvas = QPixmap(pw, ph)
        canvas.fill(Qt.GlobalColor.white)
        p = QPainter(canvas)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        px_per_pt = pw / paper_w_pt
        # 余白（ここから外は印刷されない）を点線で示す
        margin_rect = QRectF(printable.x() * px_per_pt, printable.y() * px_per_pt,
                             printable.width() * px_per_pt,
                             printable.height() * px_per_pt)
        nup = self.nup_combo.currentData() or 1
        if nup > 1:
            # 割り付け: 区画ごとに 1 ページずつ、収まるように縮小して並べる
            start = (pos // nup) * nup
            sheet = pages[start:start + nup]
            slots = nup_slots(margin_rect, nup, nat_w, nat_h)
            zoom = max(0.12, 0.5 / (nup ** 0.5))
            for slot, idx in zip(slots, sheet):
                try:
                    pm = self._preview_pixmap(idx, zoom)
                except Exception:  # noqa: BLE001
                    continue
                pw_pt, ph_pt = self._doc.page_pixel_size(idx, 1.0)
                self._draw_fitted(p, pm, slot, pw_pt, ph_pt)
            pen = QPen(Qt.GlobalColor.gray)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(margin_rect.adjusted(0, 0, -1, -1))
            p.end()
            self.preview.setPixmap(canvas)
            first, last = sheet[0] + 1, sheet[-1] + 1
            self.page_pos_label.setText(
                f"{start // nup + 1} / {sheets} 枚目（{first}〜{last} ページ）{total}")
            return

        rotate = (self.rb_auto.isChecked()
                  and (nat_w > nat_h) != (paper_w_pt > paper_h_pt))
        eff_w, eff_h = (nat_h, nat_w) if rotate else (nat_w, nat_h)
        # 拡大率は印刷可能領域に対して決める（実際の印刷と同じ計算にする）
        avail_w = max(1.0, printable.width())
        avail_h = max(1.0, printable.height())
        fit = min(avail_w / eff_w, avail_h / eff_h)
        mode = self.size_combo.currentIndex()
        if mode == SIZE_ACTUAL:
            s = 1.0
        elif mode == SIZE_FIT:
            s = fit
        elif mode == SIZE_SHRINK:
            s = min(1.0, fit)
        else:
            s = self.scale_spin.value() / 100.0
        tw = eff_w * s * px_per_pt
        th = eff_h * s * px_per_pt
        # 印刷と同じ置き方（用紙の中央。印刷できる範囲に収まるならその中へ）
        x, y = place_on_paper(QRectF(0, 0, pw, ph), margin_rect, tw, th)
        img = pix.toImage()
        if self.gray_check.isChecked():
            img = img.convertToFormat(img.Format.Format_Grayscale8)
        draw = QPixmap.fromImage(img)
        if rotate:
            p.save()
            p.translate(x + tw / 2, y + th / 2)
            p.rotate(90)
            p.drawPixmap(QRectF(-th / 2, -tw / 2, th, tw), draw,
                         QRectF(0, 0, draw.width(), draw.height()))
            p.restore()
        else:
            p.drawPixmap(QRectF(x, y, tw, th), draw,
                         QRectF(0, 0, draw.width(), draw.height()))
        pen = QPen(Qt.GlobalColor.gray)
        pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(margin_rect.adjusted(0, 0, -1, -1))
        p.end()
        self.preview.setPixmap(canvas)
