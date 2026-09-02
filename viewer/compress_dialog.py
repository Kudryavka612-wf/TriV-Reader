"""PDF 圧縮ダイアログ（プリセット選択＋試算）。

用途別のプリセットを選ぶだけで済むようにし、細かく詰めたい場合だけ
「カスタム」で解像度と画質を直接指定する。OK を押す前に「試算」で
圧縮後のサイズを確認できる（試算結果は破棄され、ファイルは残さない）。

実際の圧縮は PdfDocument.compress_to_advanced が行い、このダイアログは
設定値（options()）を返すだけ。
"""
from __future__ import annotations

import os
import tempfile

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

from .document import PdfDocument

CUSTOM = "custom"


def human_size(n: int) -> str:
    """1234567 → "1.2 MB" のような読みやすい表記。"""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


class CompressDialog(QDialog):
    """圧縮の強さを選ぶダイアログ。"""

    def __init__(self, doc: PdfDocument, settings=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PDF を圧縮")
        self._doc = doc
        self._settings = settings

        root = QVBoxLayout(self)

        # ── 現在のファイル情報 ──────────────────────────────
        size = 0
        if doc.path and os.path.exists(doc.path):
            size = os.path.getsize(doc.path)
        self._before = size
        info = QLabel(
            f"現在のサイズ: <b>{human_size(size) if size else '未保存'}</b>"
            f"　/　{doc.page_count} ページ"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(info)

        # ── プリセット ──────────────────────────────────────
        gb = QGroupBox("圧縮の強さ")
        v = QVBoxLayout(gb)
        self._group = QButtonGroup(self)
        self._radios: dict[str, QRadioButton] = {}
        for key, (label, dpi, quality, desc) in PdfDocument.COMPRESS_PRESETS.items():
            title = f"{label}　—　画像 {dpi}dpi / 画質 {quality}" if dpi else label
            rb = QRadioButton(title)
            rb.setToolTip(desc)
            self._group.addButton(rb)
            self._radios[key] = rb
            v.addWidget(rb)
            note = QLabel(desc)
            note.setWordWrap(True)
            note.setStyleSheet("color: palette(mid); margin-left: 22px;")
            v.addWidget(note)
        rb = QRadioButton("カスタム")
        self._group.addButton(rb)
        self._radios[CUSTOM] = rb
        v.addWidget(rb)

        form = QFormLayout()
        form.setContentsMargins(22, 0, 0, 0)
        self.dpi = QSpinBox()
        self.dpi.setRange(50, 600)
        self.dpi.setSingleStep(10)
        self.dpi.setSuffix(" dpi")
        self.dpi.setToolTip(
            "これを超える解像度の画像だけ縮小します（元から低解像度の画像はそのまま）")
        self.quality = QSpinBox()
        self.quality.setRange(20, 95)
        self.quality.setToolTip("JPEG の画質。高いほどきれいで大きくなります。")
        form.addRow("画像の最大解像度:", self.dpi)
        form.addRow("JPEG 画質 (20-95):", self.quality)
        v.addLayout(form)
        root.addWidget(gb)

        self.gray = QCheckBox("画像を白黒（グレースケール）にする")
        self.gray.setToolTip("カラーのスキャン原稿は大きく軽くなります。色は失われます。")
        root.addWidget(self.gray)

        hint = QLabel("文字や図形はそのまま残ります（ページを画像化しません）。"
                      "検索・コピー・注釈・しおりは保たれます。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        root.addWidget(hint)

        # ── 試算 ────────────────────────────────────────────
        row = QHBoxLayout()
        self.btn_estimate = QPushButton("試算")
        self.btn_estimate.setToolTip("この設定で圧縮した場合のサイズを調べます（保存はしません）")
        self.btn_estimate.clicked.connect(self._estimate)
        self.result = QLabel("")
        self.result.setTextFormat(Qt.TextFormat.RichText)
        self.result.setWordWrap(True)
        row.addWidget(self.btn_estimate)
        row.addWidget(self.result, 1)
        root.addLayout(row)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
        box.button(QDialogButtonBox.StandardButton.Ok).setText("圧縮して保存…")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        root.addWidget(box)

        self._group.buttonToggled.connect(lambda *_: self._sync())
        self._load()
        self._sync()

    # --- 設定の読み書き -------------------------------------------------
    def _load(self) -> None:
        s = self._settings
        preset = s.value("compress/preset", "standard", str) if s else "standard"
        if preset not in self._radios:
            preset = "standard"
        self.dpi.setValue(int(s.value("compress/dpi", 150, int)) if s else 150)
        self.quality.setValue(int(s.value("compress/quality", 72, int)) if s else 72)
        self.gray.setChecked(
            bool(s.value("compress/grayscale", False, bool)) if s else False)
        self._radios[preset].setChecked(True)

    def save_settings(self) -> None:
        if not self._settings:
            return
        self._settings.setValue("compress/preset", self.preset())
        self._settings.setValue("compress/dpi", self.dpi.value())
        self._settings.setValue("compress/quality", self.quality.value())
        self._settings.setValue("compress/grayscale", self.gray.isChecked())

    # --- 状態 -----------------------------------------------------------
    def preset(self) -> str:
        for key, rb in self._radios.items():
            if rb.isChecked():
                return key
        return "standard"

    def _sync(self) -> None:
        """プリセットに応じてカスタム欄の値と有効/無効を合わせる。"""
        key = self.preset()
        custom = key == CUSTOM
        self.dpi.setEnabled(custom)
        self.quality.setEnabled(custom)
        lossless = not custom and not PdfDocument.COMPRESS_PRESETS[key][1]
        self.gray.setEnabled(not lossless)  # 無劣化では画像に触らない
        if not custom and not lossless:
            _, dpi, quality, _ = PdfDocument.COMPRESS_PRESETS[key]
            self.dpi.setValue(dpi)
            self.quality.setValue(quality)
        self.result.setText("")

    def options(self) -> dict:
        """compress_to_advanced に渡すキーワード引数。"""
        key = self.preset()
        if key == CUSTOM:
            return {"max_dpi": self.dpi.value(),
                    "jpeg_quality": self.quality.value(),
                    "grayscale": self.gray.isChecked()}
        _, dpi, quality, _ = PdfDocument.COMPRESS_PRESETS[key]
        return {"max_dpi": dpi, "jpeg_quality": quality or 72,
                "grayscale": self.gray.isChecked() and bool(dpi)}

    # --- 試算 -----------------------------------------------------------
    def _estimate(self) -> None:
        tmp = os.path.join(tempfile.gettempdir(), "_trivreader_estimate.pdf")
        dlg = QProgressDialog("試算中…", "キャンセル", 0, 100, self)
        dlg.setWindowTitle("試算")
        dlg.setMinimumDuration(0)

        def progress(done, total):
            dlg.setMaximum(max(1, total))
            dlg.setValue(done)
            QApplication.processEvents()
            return not dlg.wasCanceled()

        self.btn_estimate.setEnabled(False)
        try:
            stats = self._doc.compress_to_advanced(
                tmp, progress=progress, **self.options())
        except Exception as exc:  # noqa: BLE001
            self.result.setText(f"試算できませんでした: {exc}")
            return
        finally:
            self.btn_estimate.setEnabled(True)
            dlg.close()
        if stats.get("canceled"):
            self.result.setText("試算を中止しました")
            return
        after = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        try:
            os.remove(tmp)
        except OSError:
            pass
        self.result.setText(self.describe(self._before, after, stats))

    @staticmethod
    def describe(before: int, after: int, stats: dict) -> str:
        """圧縮前後のサイズを 1 行にまとめる（試算・完了メッセージ共通）。"""
        text = f"圧縮後: <b>{human_size(after)}</b>"
        if before:
            pct = round(100 * after / before)
            if after < before:
                text += f"（元の {pct}% ／ {human_size(before - after)} 削減）"
            else:
                text += f"（元の {pct}% ＝ ほとんど縮みません）"
        n = stats.get("recompressed", 0)
        if n:
            text += f"　画像 {n} 点を再圧縮"
        elif stats.get("images"):
            text += "　※ 画像はすでに十分小さく、再圧縮していません"
        return text
