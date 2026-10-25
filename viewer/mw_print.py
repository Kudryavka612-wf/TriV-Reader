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


def select_pages(opt: dict, total: int, current: int) -> list[int]:
    """ダイアログの選択内容から、刷るページ（0 始まり）を順に返す。

    「すべて / 現在 / 指定」で母集団を決め、奇数・偶数で絞り、逆順にする。
    奇数・偶数は文書のページ番号（1 始まり）で数える。ページ指定が誤って
    いれば ValueError（メッセージは parse_ranges のもの）。
    """
    if opt.get("page_mode") == "current":
        pages = [current]
    elif opt.get("page_mode") == "range":
        pages = parse_ranges(opt.get("range_text", ""), total)
    else:
        pages = list(range(total))
    pages = [i for i in pages if 0 <= i < total]
    subset = opt.get("subset", "all")
    if subset == "odd":
        pages = [i for i in pages if (i + 1) % 2 == 1]
    elif subset == "even":
        pages = [i for i in pages if (i + 1) % 2 == 0]
    if opt.get("reverse"):
        pages.reverse()
    return pages


def print_content(opt: dict) -> str:
    """刷る中身（PdfDocument.PRINT_*）。古い呼び出しの annots=True/False も読む。"""
    content = opt.get("content")
    if content:
        return content
    return "all" if opt.get("annots", True) else "doc"


def svg_is_safe(svg: str) -> bool:
    """QSvgRenderer でそのまま描いてよい SVG か。

    ハイライトは「乗算」の重ね方で描かれる（下の文字が透ける）。QSvgRenderer は
    重ね方（mix-blend-mode）を無視して塗りつぶすので、ハイライトの下の文字が
    消えた。そういうページは画像として描く。
    """
    return "mix-blend-mode" not in svg


def paper_for_page(w_pt: float, h_pt: float):
    """PDF のページに合わせる用紙（縦向きの寸法で、定形に近ければ定形）。"""
    from PySide6.QtCore import QSizeF
    from PySide6.QtGui import QPageSize

    if w_pt <= 0 or h_pt <= 0:
        return None
    short, long_ = sorted((w_pt, h_pt))
    # FuzzyMatch: 1pt ずれた A4（596×843 など）も A4 として扱う
    return QPageSize(QSizeF(short, long_), QPageSize.Unit.Point, "",
                     QPageSize.SizeMatchPolicy.FuzzyMatch)


def place_on_paper(paper, printable, w: float, h: float) -> tuple[float, float]:
    """幅 w・高さ h の内容を置く左上の位置。

    用紙の中央に置く（Acrobat と同じ。実寸で刷れば印刷済みの用紙の枠と合う）。
    印刷できる範囲に収まる大きさなら、その中へ最小限ずらす（余白の左右が
    違うプリンターでも端が欠けない）。
    """
    x = paper.left() + (paper.width() - w) / 2
    y = paper.top() + (paper.height() - h) / 2
    if w <= printable.width():
        x = min(max(x, printable.left()), printable.right() - w)
    if h <= printable.height():
        y = min(max(y, printable.top()), printable.bottom() - h)
    return x, y


class PrintMixin:
    """印刷ダイアログとプリンタへの描画。MainWindow に混ぜて使う。"""

    def print_document(self) -> None:
        """Adobe 風の印刷ダイアログ（プレビュー付き）で印刷する。"""
        if not self.doc.is_open:
            return
        self.page_view.finish_text_edit()   # 打ちかけの文字も刷る
        from .print_dialog import PrintDialog

        dlg = PrintDialog(self.doc, self.page_view.index, self.settings, self)
        if dlg.exec() != PrintDialog.DialogCode.Accepted:
            return
        opt = dlg.options()

        # プリンタを構成。ダイアログの「プロパティ」で変えたドライバーの設定
        # （給紙トレイ・用紙の種類など）を持った QPrinter をそのまま使う
        printer = dlg.printer()
        self._configure_printer(printer, opt)

        # 対象ページ
        try:
            pages = select_pages(opt, self.doc.page_count, self.page_view.index)
        except ValueError as exc:
            QMessageBox.information(self, "入力エラー", str(exc))
            return
        if not pages:
            QMessageBox.information(
                self, "印刷", "該当するページがありません（奇数/偶数の指定を確認してください）")
            return

        self._render_to_printer(printer, pages, opt)

    def _configure_printer(self, printer, opt: dict) -> None:
        """ダイアログの選択内容をプリンターに反映する（部数・両面・用紙・向き）。"""
        from PySide6.QtGui import QPageLayout, QPageSize

        printer.setCopyCount(opt.get("copies", 1))
        # 2 部以上のとき、1 部ずつ揃えて出すか、同じページをまとめて出すか
        printer.setCollateCopies(bool(opt.get("collate", True)))
        printer.setDuplex({
            "long": QPrinter.DuplexMode.DuplexLongSide,
            "short": QPrinter.DuplexMode.DuplexShortSide,
        }.get(opt.get("duplex", "none"), QPrinter.DuplexMode.DuplexNone))
        if opt.get("grayscale"):
            printer.setColorMode(QPrinter.ColorMode.GrayScale)
        # 用紙。"auto"（PDF のページサイズに合わせる）はページごとに指定する。
        paper = opt.get("paper")
        if isinstance(paper, QPageSize):
            printer.setPageSize(paper)
        # 向きは必ず指定する。指定しないとプリンター側の既定（横のまま等）が
        # 残り、「縦」を選んだのに横で出ることがある。
        printer.setPageOrientation(
            QPageLayout.Orientation.Landscape
            if opt.get("orientation") == "landscape"
            else QPageLayout.Orientation.Portrait)

    def _render_to_printer(self, printer, pages: list[int], opt: dict) -> None:
        """選んだページをプリンタへ描画する（ダイアログから切り離してある）。"""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainter

        from .print_dialog import nup_slots

        res = printer.resolution() or 300           # プリンタの dpi
        auto_paper = opt.get("paper") == "auto"
        nup = max(1, int(opt.get("nup", 1) or 1))   # 1 枚に載せるページ数

        # PDF の印刷の指定（印刷しない欄・印刷だけする欄）と、刷る中身の選択を
        # 反映した複製から描く。手を入れずに刷れるときは複製を作らない。
        source = self.doc
        try:
            copy = self.doc.print_copy(print_content(opt))
        except Exception:  # noqa: BLE001  作れなければそのまま出す
            copy = None
        if copy is not None:
            source = copy

        sheets = [pages[i:i + nup] for i in range(0, len(pages), nup)]
        if auto_paper and sheets:
            self._set_paper_from_page(printer, sheets[0][0])

        painter = QPainter(printer)
        if not painter.isActive():
            # プリンターが止まっている・ドライバーが応答しない・出力先に書けない等
            if copy is not None:
                copy.close()
            QMessageBox.warning(
                self, "印刷",
                f"「{opt.get('printer') or 'プリンター'}」で印刷を始められませんでした。\n"
                "プリンターの電源・接続と、Windows の「プリンターとスキャナー」で"
                "エラーが出ていないかを確認してください。")
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        progress = QProgressDialog("印刷を準備中…", "キャンセル", 0, len(sheets), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(400)
        try:
            for n, sheet in enumerate(sheets):
                progress.setValue(n)
                if progress.wasCanceled():
                    # 描いた分も捨てる（end だけだと途中までのジョブが送られる）
                    printer.abort()
                    painter.end()
                    self.statusBar().showMessage(
                        "印刷を中止しました（プリンターには送っていません）", 4000)
                    return
                if n > 0:
                    # 用紙をページごとに変える場合、newPage の前に指定する
                    if auto_paper:
                        self._set_paper_from_page(printer, sheet[0])
                    printer.newPage()
                # QPainter の原点は「印刷できる範囲」の左上。pageRect は用紙の
                # 左上からの位置なので、そのまま使うと余白を 2 回足すことになり、
                # 紙の上で右下へずれて右端・下端が欠けていた。用紙・範囲は
                # ページごとに取り直す（用紙が変わることがある）。
                page_rect = QRectF(printer.pageRect(QPrinter.Unit.DevicePixel))
                paper_rect = QRectF(printer.paperRect(QPrinter.Unit.DevicePixel))
                area = QRectF(0, 0, page_rect.width(), page_rect.height())
                paper = paper_rect.translated(-page_rect.left(), -page_rect.top())
                if nup == 1:
                    self._draw_page_in(painter, source, sheet[0], area, opt, res,
                                       paper=paper)
                    continue
                # 割り付け: 区画に分けて、それぞれに収まるよう縮小して並べる
                w_pt, h_pt = source.page_pixel_size(sheet[0], 1.0)
                for slot, index in zip(nup_slots(area, nup, w_pt, h_pt), sheet):
                    self._draw_page_in(painter, source, index, slot, opt, res,
                                       fit_only=True)
            progress.setValue(len(sheets))
        finally:
            if painter.isActive():
                painter.end()
            if copy is not None:
                copy.close()
        self.statusBar().showMessage(
            f"{len(sheets)} 枚分を「{opt.get('printer') or 'プリンター'}」に送りました", 5000)

    def _set_paper_from_page(self, printer, index: int) -> None:
        """そのページの実寸を用紙サイズにする（B5 の届出書やはがき向け）。"""
        from PySide6.QtGui import QPageLayout

        w_pt, h_pt = self.doc.page_pixel_size(index, 1.0)
        size = paper_for_page(w_pt, h_pt)
        if size is None:
            return
        try:
            printer.setPageSize(size)
            # 横長のページは「A4 の横向き」として渡す。横長の寸法のまま渡すと
            # 定形外（ユーザー定義）用紙になり、複合機が手差しを待って止まる
            printer.setPageOrientation(
                QPageLayout.Orientation.Landscape if w_pt > h_pt
                else QPageLayout.Orientation.Portrait)
        except Exception:  # noqa: BLE001  受け付けないプリンターは既定のまま
            pass

    def _draw_page_in(self, painter, source, index: int, area, opt: dict,
                      res: int, fit_only: bool = False, paper=None) -> None:
        """1 ページを area（印刷できる範囲。描画のピクセル座標）に描く。

        paper は用紙全体（同じ座標）。ページは用紙の中央に置く（実寸で刷ると
        印刷済みの用紙の枠とぴったり合う）。印刷できる範囲に収まる大きさなら
        その中へずらす。fit_only=True（割り付け）のときは、ページサイズ処理を
        無視して区画の中央に収まるよう縮小する。区画に対して縦横が食い違う
        ページは 90 度回す。
        """
        from PySide6.QtCore import QRectF

        from .print_dialog import SIZE_ACTUAL, SIZE_FIT, SIZE_SHRINK

        quality = opt.get("quality", "vector")
        vector = quality == "vector"
        max_dpi = 0 if vector else (int(quality) or 0)
        grayscale = opt["grayscale"]
        auto_rotate = fit_only or opt["orientation"] == "auto"

        # ページの実寸(pt) → 紙面ピクセル数（1pt = 1/72inch）
        w_pt, h_pt = source.page_pixel_size(index, 1.0)
        nat_w = w_pt / 72.0 * res
        nat_h = h_pt / 72.0 * res
        if nat_w <= 0 or nat_h <= 0:
            return
        pad = (min(area.width(), area.height()) * 0.01) if fit_only else 0.0
        box = area.adjusted(pad, pad, -pad, -pad)
        # 向き=自動: 内容と紙（区画）の縦横が食い違うときは内容を 90° 回す
        rotate = auto_rotate and (
            (nat_w > nat_h) != (box.width() > box.height()))
        eff_w, eff_h = (nat_h, nat_w) if rotate else (nat_w, nat_h)
        fit = min(box.width() / eff_w, box.height() / eff_h)
        if fit_only:
            s = fit
        elif opt["size_mode"] == SIZE_ACTUAL:
            s = 1.0
        elif opt["size_mode"] == SIZE_FIT:
            s = fit
        elif opt["size_mode"] == SIZE_SHRINK:
            s = min(1.0, fit)
        else:  # SIZE_CUSTOM
            s = opt["custom_scale"]
        tgt_w = eff_w * s
        tgt_h = eff_h * s
        if fit_only or paper is None:
            x = box.left() + (box.width() - tgt_w) / 2
            y = box.top() + (box.height() - tgt_h) / 2
        else:
            x, y = place_on_paper(paper, box, tgt_w, tgt_h)
        if vector and self._draw_page_vector(
                painter, source, index, x, y, tgt_w, tgt_h, rotate):
            return
        # 紙に出る大きさから必要な解像度を決める。プリンタ解像度と違う細かさで
        # 描くと、その差を補間で埋めることになり、ぼやけるうえに巨大ビット
        # マップの拡大縮小でとても遅くなる。
        out_dpi = res * s
        if max_dpi:
            out_dpi = min(out_dpi, max_dpi)
        zoom = max(72.0, out_dpi) / 72.0
        img = source.render_page_image(index, zoom=zoom, gray=grayscale)
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

    def _draw_page_vector(self, painter, source, index: int, x: float, y: float,
                          w: float, h: float, rotate: bool) -> bool:
        """ページをベクター（SVG 経由）で描く。描けなければ False を返す。

        QSvgRenderer は SVG のすべてを解釈できるわけではないので、少しでも
        怪しければラスタ描画に任せる（印刷が白紙になるより確実）。
        """
        try:
            from PySide6.QtCore import QRectF
            from PySide6.QtSvg import QSvgRenderer

            svg = source.page_svg(index)
            if not svg_is_safe(svg):
                return False
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
