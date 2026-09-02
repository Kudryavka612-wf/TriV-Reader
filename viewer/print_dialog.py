"""Adobe Reader 風の印刷ダイアログ。

左側に設定（プリンター/部数/対象ページ/サイズ処理/向き/グレースケール）、
右側にプレビュー。OK で選択内容を options() として返し、実際の印刷処理は
呼び出し側（main_window.print_document）が行う。
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSizeF, Qt
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
    QSpinBox,
    QVBoxLayout,
)

# サイズ処理モード
SIZE_FIT, SIZE_ACTUAL, SIZE_SHRINK, SIZE_CUSTOM = range(4)


class PrintDialog(QDialog):
    """印刷設定＋プレビュー。"""

    PREVIEW_W = 300

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
        form.addRow("プリンター:", self.printer_combo)
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
        self.rb_current = QRadioButton("現在のページ")
        row = QHBoxLayout()
        self.rb_range = QRadioButton("ページ指定:")
        self.range_edit = QLineEdit()
        self.range_edit.setPlaceholderText("例: 1-3, 5, 8")
        row.addWidget(self.rb_range)
        row.addWidget(self.range_edit)
        self.rb_all.setChecked(True)
        v.addWidget(self.rb_all)
        v.addWidget(self.rb_current)
        v.addLayout(row)
        self._page_group = QButtonGroup(self)
        for rb in (self.rb_all, self.rb_current, self.rb_range):
            self._page_group.addButton(rb)
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

        self.gray_check = QCheckBox("グレースケール（白黒）で印刷")
        left.addWidget(self.gray_check)

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
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("印刷")
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        left.addWidget(bb)
        root.addLayout(left, 1)

        # ── 右: プレビュー ──────────────────────────────────────
        right = QVBoxLayout()
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(self.PREVIEW_W, int(self.PREVIEW_W * 1.4142))
        self.preview.setStyleSheet("background: #888; border: 1px solid #666;")
        right.addWidget(self.preview, 1)
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

        self._printer_name = None
        self._sync_printer_caps()
        if settings:
            idx = self.duplex_combo.findData(settings.value("print/duplex", "none", str))
            self.duplex_combo.setCurrentIndex(max(0, idx))
            self.collate_check.setChecked(
                settings.value("print/collate", True, bool))
        self._refresh()

    def _on_printer_changed(self, _name: str) -> None:
        self._sync_printer_caps()
        self._refresh()

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
                pr.setPageSize(QPageSize(QSizeF(nat_w, nat_h), QPageSize.Unit.Point,
                                         "PDF", QPageSize.SizeMatchPolicy.ExactMatch))
            pr.setPageOrientation(
                QPageLayout.Orientation.Landscape
                if self.rb_landscape.isChecked()
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
            "duplex": self.duplex_combo.currentData() if self.duplex_combo.isEnabled() else "none",
            "collate": self.collate_check.isChecked(),
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
        self.accept()

    # --- プレビュー -------------------------------------------------------
    def _step_preview(self, step: int) -> None:
        n = self._doc.page_count
        self._preview_index = max(0, min(self._preview_index + step, n - 1))
        self._refresh()

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

            renderer = QSvgRenderer(self._doc.page_svg(index).encode("utf-8"))
            if renderer.isValid():
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
        return self._doc.render_page(index, zoom=zoom)

    def _refresh(self) -> None:
        i = self._preview_index
        n = self._doc.page_count
        self.page_pos_label.setText(f"{i + 1} / {n}")
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
        # プレビュー画布
        pw = self.PREVIEW_W
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
        x = margin_rect.x() + (margin_rect.width() - tw) / 2
        y = margin_rect.y() + (margin_rect.height() - th) / 2
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
