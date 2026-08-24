"""印刷まわり（MainWindow から切り出した Mixin）。

既定はベクター（SVG 経由）で描く。プリンタのネイティブ解像度でそのまま出る
ので文字がくっきりし、データ量も小さい。図版が崩れる PDF 向けに画像化も
選べる（印刷ダイアログの「品質」）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import QMessageBox, QProgressDialog

from .util import parse_ranges


class PrintMixin:
    """印刷ダイアログとプリンタへの描画。MainWindow に混ぜて使う。"""

    def print_document(self) -> None:
        """Adobe 風の印刷ダイアログ（プレビュー付き）で印刷する。"""
        if not self.doc.is_open:
            return
        from PySide6.QtPrintSupport import QPrinterInfo

        from .print_dialog import PrintDialog

        dlg = PrintDialog(self.doc, self.page_view.index, self.settings, self)
        if dlg.exec() != PrintDialog.DialogCode.Accepted:
            return
        opt = dlg.options()

        # プリンタを構成
        info = QPrinterInfo.printerInfo(opt["printer"])
        printer = (QPrinter(info, QPrinter.PrinterMode.HighResolution)
                   if not info.isNull() else QPrinter(QPrinter.PrinterMode.HighResolution))
        printer.setCopyCount(opt["copies"])
        if opt["grayscale"]:
            printer.setColorMode(QPrinter.ColorMode.GrayScale)
        if opt["orientation"] == "landscape":
            from PySide6.QtGui import QPageLayout
            printer.setPageOrientation(QPageLayout.Orientation.Landscape)

        # 対象ページ
        total = self.doc.page_count
        if opt["page_mode"] == "current":
            pages = [self.page_view.index]
        elif opt["page_mode"] == "range":
            try:
                pages = parse_ranges(opt["range_text"], total)
            except ValueError as exc:
                QMessageBox.information(self, "入力エラー", str(exc))
                return
        else:
            pages = list(range(total))
        pages = [i for i in pages if 0 <= i < total]
        if not pages:
            return

        self._render_to_printer(printer, pages, opt)

    def _render_to_printer(self, printer, pages: list[int], opt: dict) -> None:
        """選んだページをプリンタへ描画する（ダイアログから切り離してある）。"""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainter

        from .print_dialog import SIZE_ACTUAL, SIZE_FIT, SIZE_SHRINK

        res = printer.resolution() or 300           # プリンタの dpi
        # 品質: "vector" ならベクターで描く。数値ならラスタライズの上限 dpi
        # （0 は頭打ちなし＝プリンタ解像度まで）。
        quality = opt.get("quality", "vector")
        vector = quality == "vector"
        max_dpi = 0 if vector else (int(quality) or 0)
        grayscale = opt["grayscale"]
        size_mode = opt["size_mode"]
        custom_scale = opt["custom_scale"]
        auto_rotate = opt["orientation"] == "auto"

        painter = QPainter(printer)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        page_rect = printer.pageRect(QPrinter.Unit.DevicePixel)  # 印刷可能領域(px)
        progress = QProgressDialog("印刷を準備中…", "キャンセル", 0, len(pages), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(400)
        try:
            for n, i in enumerate(pages):
                progress.setValue(n)
                if progress.wasCanceled():
                    painter.end()
                    self.statusBar().showMessage("印刷をキャンセルしました", 3000)
                    return
                if n > 0:
                    printer.newPage()
                # ページの実寸(pt) → 紙面ピクセル数（1pt = 1/72inch）
                w_pt, h_pt = self.doc.page_pixel_size(i, 1.0)
                nat_w = w_pt / 72.0 * res
                nat_h = h_pt / 72.0 * res
                if nat_w <= 0 or nat_h <= 0:
                    continue
                # 向き=自動: 内容と用紙の縦横が食い違うときは内容を 90° 回して印刷
                rotate = auto_rotate and (
                    (nat_w > nat_h) != (page_rect.width() > page_rect.height()))
                eff_w, eff_h = (nat_h, nat_w) if rotate else (nat_w, nat_h)
                fit = min(page_rect.width() / eff_w, page_rect.height() / eff_h)
                if size_mode == SIZE_ACTUAL:
                    s = 1.0
                elif size_mode == SIZE_FIT:
                    s = fit
                elif size_mode == SIZE_SHRINK:
                    s = min(1.0, fit)
                else:  # SIZE_CUSTOM
                    s = custom_scale
                tgt_w = eff_w * s
                tgt_h = eff_h * s
                x = page_rect.left() + (page_rect.width() - tgt_w) / 2
                y = page_rect.top() + (page_rect.height() - tgt_h) / 2
                if vector and self._draw_page_vector(
                        painter, i, x, y, tgt_w, tgt_h, rotate):
                    continue
                # 紙に出る大きさから必要な解像度を決める。プリンタ解像度と
                # 違う細かさで描くと、その差を補間で埋めることになり、ぼやける
                # うえに巨大ビットマップの拡大縮小でとても遅くなる。
                out_dpi = res * s
                if max_dpi:
                    out_dpi = min(out_dpi, max_dpi)
                zoom = max(72.0, out_dpi) / 72.0
                img = self.doc.render_page_image(i, zoom=zoom, gray=grayscale)
                src = QRectF(0, 0, img.width(), img.height())
                if rotate:
                    painter.save()
                    painter.translate(x + tgt_w / 2, y + tgt_h / 2)
                    painter.rotate(90)
                    painter.drawImage(
                        QRectF(-tgt_h / 2, -tgt_w / 2, tgt_h, tgt_w), img, src)
                    painter.restore()
                else:
                    painter.drawImage(QRectF(x, y, tgt_w, tgt_h), img, src)
            progress.setValue(len(pages))
        finally:
            if painter.isActive():
                painter.end()
        self.statusBar().showMessage("印刷ジョブを送信しました", 3000)

    def _draw_page_vector(self, painter, index: int, x: float, y: float,
                          w: float, h: float, rotate: bool) -> bool:
        """ページをベクター（SVG 経由）で描く。描けなければ False を返す。

        QSvgRenderer は SVG のすべてを解釈できるわけではないので、少しでも
        怪しければラスタ描画に任せる（印刷が白紙になるより確実）。
        """
        try:
            from PySide6.QtCore import QRectF
            from PySide6.QtSvg import QSvgRenderer

            svg = self.doc.page_svg(index)
            renderer = QSvgRenderer(svg.encode("utf-8"))
            if not renderer.isValid():
                return False
            if rotate:
                painter.save()
                painter.translate(x + w / 2, y + h / 2)
                painter.rotate(90)
                renderer.render(painter, QRectF(-h / 2, -w / 2, h, w))
                painter.restore()
            else:
                renderer.render(painter, QRectF(x, y, w, h))
            return True
        except Exception:  # noqa: BLE001
            return False
