"""変換・書き出しまわり（MainWindow から切り出した Mixin）。

画像化 / 画像から PDF / OCR / 傾き補正 / テキスト書き出し / 圧縮 / 最適化 /
PDF-A 化 / 一括処理 / 2 ファイルの比較。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QMessageBox,
    QProgressDialog,
)

from . import ocr
from . import settings_dialog as prefs
from .document import PdfDocument
from .util import parse_ranges


class ConvertMixin:
    """ファイル変換と書き出しのダイアログ群。MainWindow に混ぜて使う。"""

    def _export_text(self, as_html: bool) -> None:
        if not self.doc.is_open:
            return
        base = os.path.splitext(self.doc.path or "document")[0]
        if as_html:
            out, _ = QFileDialog.getSaveFileName(
                self, "HTMLに書き出し", base + ".html", "HTML (*.html)")
        else:
            out, _ = QFileDialog.getSaveFileName(
                self, "テキストに書き出し", base + ".txt", "テキスト (*.txt)")
        if not out:
            return
        try:
            if as_html:
                self.doc.export_html(out)
            else:
                self.doc.export_text(out)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"書き出せませんでした:\n{exc}")
            return
        self.statusBar().showMessage(f"書き出しました: {out}", 4000)

    def deskew_document(self) -> None:
        if not self.doc.is_open:
            return
        ret = QMessageBox.question(
            self, "傾き補正",
            "スキャン（画像）ページの傾きを補正します。\n"
            "対象ページは画像として作り直されます（テキストのあるページは変更しません）。続行しますか？",
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        dlg = QProgressDialog("傾き補正中…", "キャンセル", 0, self.doc.page_count, self)
        dlg.setWindowTitle("傾き補正")
        dlg.setMinimumDuration(0)

        def progress(done, total):
            dlg.setMaximum(total)
            dlg.setValue(done)
            QApplication.processEvents()
            return not dlg.wasCanceled()

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            n = self.doc.deskew_all(progress=progress)
        except Exception as exc:  # noqa: BLE001
            dlg.close()
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "エラー", f"傾き補正に失敗:\n{exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        dlg.close()
        if n < 0:
            self.statusBar().showMessage("傾き補正をキャンセルしました", 3000)
            return
        self._reload(self.page_view.index)
        self.thumbnails.load(self.doc)
        self._update_title()
        self.statusBar().showMessage(f"{n} ページの傾きを補正しました", 4000)

    # --- バッチ処理 / 比較 ---------------------------------------------
    def batch_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "処理する PDF を選択（複数可）", self._last_dir(), "PDF (*.pdf)"
        )
        if not paths:
            return
        ops = ["圧縮(画像を150dpiに縮小)", "最適化(無劣化)",
               "各ページ画像化(PNG 150dpi)", "OCR(日本語+英語)"]
        if getattr(self, "is_lite", False):
            ops.pop(0)  # ライト版は Pillow 非同梱で画像を再圧縮できない
        op, ok = QInputDialog.getItem(self, "バッチ処理", "操作:", ops, 0, False)
        if not ok:
            return
        out_dir = QFileDialog.getExistingDirectory(self, "出力先フォルダ")
        if not out_dir:
            return
        tessdata = None
        if op.startswith("OCR"):
            try:
                tessdata = ocr.ensure_languages(["jpn", "eng"])
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "エラー", f"OCR準備に失敗:\n{exc}")
                return

        dlg = QProgressDialog("バッチ処理中…", "キャンセル", 0, len(paths), self)
        dlg.setWindowTitle("バッチ処理")
        dlg.setMinimumDuration(0)
        done, errors = 0, []
        for n, p in enumerate(paths):
            dlg.setValue(n)
            dlg.setLabelText(f"{os.path.basename(p)} ({n + 1}/{len(paths)})")
            QApplication.processEvents()
            if dlg.wasCanceled():
                break
            stem = os.path.splitext(os.path.basename(p))[0]
            try:
                d = PdfDocument()
                d.open(p)
                if d.needs_password:
                    d.close()
                    errors.append(f"{stem}: パスワード保護のためスキップ")
                    continue
                if op.startswith("圧縮"):
                    d.compress_to_advanced(
                        os.path.join(out_dir, f"{stem}_compressed.pdf"),
                        max_dpi=150, jpeg_quality=72)
                elif op.startswith("最適化"):
                    d.compress_to(os.path.join(out_dir, f"{stem}_optimized.pdf"))
                elif op.startswith("各ページ"):
                    d.export_page_images(list(range(d.page_count)), out_dir,
                                         "png", 150, stem)
                else:  # OCR
                    d.ocr_to(os.path.join(out_dir, f"{stem}_ocr.pdf"),
                             "jpn+eng", tessdata, dpi=300)
                d.close()
                done += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{stem}: {exc}")
        dlg.setValue(len(paths))
        msg = f"{done} 件を処理しました。\n出力先: {out_dir}"
        if errors:
            msg += "\n\n[スキップ/失敗]\n" + "\n".join(errors[:10])
        QMessageBox.information(self, "バッチ処理 完了", msg)

    def diff_dialog(self) -> None:
        from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QPlainTextEdit,
                                       QVBoxLayout)
        import difflib
        a, _ = QFileDialog.getOpenFileName(self, "比較するPDF①", self._last_dir(), "PDF (*.pdf)")
        if not a:
            return
        b, _ = QFileDialog.getOpenFileName(self, "比較するPDF②", self._last_dir(), "PDF (*.pdf)")
        if not b:
            return

        def text_of(path):
            d = PdfDocument(); d.open(path)
            if d.needs_password:
                d.close()
                raise RuntimeError("パスワード保護されています")
            t = d.full_text(); d.close()
            return t

        try:
            ta, tb = text_of(a), text_of(b)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"比較できませんでした:\n{exc}")
            return
        diff = list(difflib.unified_diff(
            ta.splitlines(), tb.splitlines(),
            fromfile=os.path.basename(a), tofile=os.path.basename(b), lineterm=""
        ))
        dlg = QDialog(self)
        dlg.setWindowTitle("PDF テキスト比較")
        dlg.resize(720, 560)
        lay = QVBoxLayout(dlg)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setStyleSheet("font-family: Consolas, monospace;")
        view.setPlainText("\n".join(diff) if diff else "差分はありません（テキストは一致）。")
        lay.addWidget(view)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(dlg.reject)
        box.accepted.connect(dlg.accept)
        lay.addWidget(box)
        dlg.exec()

    # --- 圧縮 / 最適化 / PDF-A ------------------------------------------
    def compress_document(self) -> None:
        """画像を再圧縮して軽くする（プリセット選択 → 保存先 → 実行）。

        compress_save（無劣化の整理だけ）と違い、埋め込み画像を指定解像度まで
        縮小して再エンコードするため、スキャン PDF が実際に小さくなる。
        """
        if not self.doc.is_open:
            return
        from PySide6.QtWidgets import QDialog

        from .compress_dialog import CompressDialog, human_size

        dlg = CompressDialog(self.doc, self.settings, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dlg.save_settings()
        opts = dlg.options()

        src = self.doc.path if self.doc.path and os.path.exists(self.doc.path) else ""
        before = os.path.getsize(src) if src else 0
        base = os.path.splitext(self.doc.path or "document")[0]
        out, _ = QFileDialog.getSaveFileName(
            self, "圧縮して保存", base + "_compressed.pdf", "PDF ファイル (*.pdf)"
        )
        if not out:
            return

        prog = QProgressDialog("圧縮中…", "キャンセル", 0, 100, self)
        prog.setWindowTitle("圧縮")
        prog.setMinimumDuration(0)

        def progress(done, total):
            prog.setMaximum(max(1, total))
            prog.setValue(done)
            QApplication.processEvents()
            return not prog.wasCanceled()

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            stats = self.doc.compress_to_advanced(out, progress=progress, **opts)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"圧縮できませんでした:\n{exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
            prog.close()

        if stats.get("canceled"):
            self.statusBar().showMessage("圧縮をキャンセルしました", 3000)
            return

        # 開いているファイル自身に上書きした場合は中身が入れ替わっている
        if src and os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(out)):
            self._reload(self.page_view.index)
            self.thumbnails.load(self.doc)
            self._update_title()

        after = os.path.getsize(out)
        msg = (f"圧縮して保存しました:\n{out}\n\n"
               f"{human_size(before) if before else '?'} → {human_size(after)}")
        if before:
            msg += f"（元の {round(100 * after / before)}%）"
        if stats.get("recompressed"):
            msg += f"\n画像 {stats['recompressed']} 点を再圧縮しました。"
        elif stats.get("images"):
            msg += "\n画像はすでに十分小さかったため、再圧縮していません。"
        if before and after >= before:
            msg += ("\n\nこの PDF は画像がほとんど無いか、すでに圧縮済みのようです。"
                    "元のファイルをそのまま使うのがおすすめです。")
        QMessageBox.information(self, "圧縮 完了", msg)

    def compress_save(self) -> None:
        if not self.doc.is_open:
            return
        before = os.path.getsize(self.doc.path) if self.doc.path and os.path.exists(self.doc.path) else 0
        base = os.path.splitext(self.doc.path or "document")[0]
        out, _ = QFileDialog.getSaveFileName(
            self, "最適化して保存", base + "_optimized.pdf", "PDF ファイル (*.pdf)"
        )
        if not out:
            return
        try:
            self.doc.compress_to(out)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"最適化できませんでした:\n{exc}")
            return
        after = os.path.getsize(out)
        msg = f"最適化して保存しました:\n{out}\n\nサイズ: {after:,} バイト"
        if before:
            msg += f"（元 {before:,} バイト / {100 * after // before}%）"
        QMessageBox.information(self, "完了", msg)

    def export_pdfa(self) -> None:
        if not self.doc.is_open:
            return
        base = os.path.splitext(self.doc.path or "document")[0]
        out, _ = QFileDialog.getSaveFileName(
            self, "PDF/A で書き出し", base + "_pdfa.pdf", "PDF ファイル (*.pdf)"
        )
        if not out:
            return
        try:
            self.doc.export_pdfa(out)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"PDF/A 化できませんでした:\n{exc}")
            return
        QMessageBox.information(
            self, "PDF/A 完了",
            f"PDF/A-2b として書き出しました:\n{out}\n\n"
            "※ sRGB の OutputIntent と PDF/A 識別情報を付与しています。"
            "厳密な準拠検証は専用バリデータの利用を推奨します。",
        )

    # --- 画像変換（Step 4） --------------------------------------------
    def export_images_dialog(self) -> None:
        """PDF→画像。ページ範囲を指定して書き出す（ツールバーから）。"""
        if not self.doc.is_open:
            return
        text, ok = QInputDialog.getText(
            self,
            "画像に書き出し",
            "対象ページ（例: 1-3,5 / 空欄で全ページ）:",
            text=f"1-{self.doc.page_count}",
        )
        if not ok:
            return
        try:
            indices = (
                parse_ranges(text, self.doc.page_count)
                if text.strip()
                else list(range(self.doc.page_count))
            )
        except ValueError as exc:
            QMessageBox.information(self, "入力エラー", str(exc))
            return
        self._export_indices_images(indices)

    def _export_indices_images(self, indices: list) -> None:
        """指定ページを画像として書き出す（整理画面の選択からも呼ばれる）。"""
        if not self.doc.is_open or not indices:
            return
        indices = [int(i) for i in indices]
        fmt, ok = QInputDialog.getItem(
            self, "形式", "画像形式:", ["PNG", "JPEG"], 0, False
        )
        if not ok:
            return
        dpi, ok = QInputDialog.getInt(
            self, "解像度", "DPI（大きいほど高精細）:", value=150, minValue=36, maxValue=600
        )
        if not ok:
            return
        out_dir = QFileDialog.getExistingDirectory(self, "出力先フォルダを選択")
        if not out_dir:
            return
        stem = os.path.splitext(os.path.basename(self.doc.path or "page"))[0]
        try:
            outputs = self.doc.export_page_images(indices, out_dir, fmt, dpi, stem)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"書き出せませんでした:\n{exc}")
            return
        QMessageBox.information(
            self, "完了", f"{len(outputs)} 枚の画像を出力しました。\n{out_dir}"
        )

    def images_to_pdf_dialog(self) -> None:
        """画像→PDF。複数画像から新しい PDF を作る（開いていなくても可）。"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "PDF にする画像を選択（複数可）", "",
            "画像 (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff)",
        )
        if not paths:
            return
        out, _ = QFileDialog.getSaveFileName(
            self, "保存先 PDF", "images.pdf", "PDF ファイル (*.pdf)"
        )
        if not out:
            return
        try:
            PdfDocument.images_to_pdf(paths, out)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"作成できませんでした:\n{exc}")
            return
        ret = QMessageBox.question(
            self, "完了", f"PDF を作成しました:\n{out}\n\n今すぐ開きますか？"
        )
        if ret == QMessageBox.StandardButton.Yes:
            self.open_path(out)  # 新しいタブで開く

    def add_images_as_pages(self) -> None:
        """画像を現在の PDF にページとして取り込む。"""
        if not self.doc.is_open:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "ページとして追加する画像を選択（複数可）", "",
            "画像 (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff)",
        )
        if not paths:
            return
        try:
            added = self.doc.insert_images_as_pages(paths)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"追加できませんでした:\n{exc}")
            return
        self._reload(self.doc.page_count - 1)
        self.statusBar().showMessage(f"{added} ページ（画像）を追加しました", 3000)

    # --- OCR（Step 5） --------------------------------------------------
    def run_ocr(self) -> None:
        """全ページを OCR し、検索可能 PDF を別名で書き出す。"""
        if not self.doc.is_open:
            return
        labels = list(ocr.LANGUAGE_CHOICES.keys())
        codes = list(ocr.LANGUAGE_CHOICES.values())
        try:  # 設定の既定言語を最初から選んでおく
            start = codes.index(prefs.value(self.settings, "ocr/lang"))
        except ValueError:
            start = 0
        label, ok = QInputDialog.getItem(
            self, "文字認識(OCR)", "認識する言語:", labels, start, False
        )
        if not ok:
            return
        language = ocr.LANGUAGE_CHOICES[label]

        # 必要な言語データを用意（不足分はダウンロード）
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            tessdata = ocr.ensure_languages(language.split("+"))
        except Exception as exc:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(
                self, "OCR 準備エラー",
                f"言語データを用意できませんでした:\n{exc}\n\n"
                "ネットワーク接続をご確認ください。",
            )
            return
        finally:
            QApplication.restoreOverrideCursor()

        base = os.path.splitext(self.doc.path or "document")[0]
        out, _ = QFileDialog.getSaveFileName(
            self, "検索可能 PDF の保存先", base + "_ocr.pdf", "PDF ファイル (*.pdf)"
        )
        if not out:
            return

        dlg = QProgressDialog("OCR 処理中…", "キャンセル", 0, self.doc.page_count, self)
        dlg.setWindowTitle("文字認識(OCR)")
        dlg.setMinimumDuration(0)
        dlg.setValue(0)

        def progress(done: int, total: int) -> bool:
            dlg.setMaximum(total)
            dlg.setValue(done)
            dlg.setLabelText(f"OCR 処理中… {done}/{total} ページ")
            QApplication.processEvents()
            return not dlg.wasCanceled()

        try:
            done = self.doc.ocr_to(out, language, tessdata, dpi=300, progress=progress)
        except Exception as exc:  # noqa: BLE001
            dlg.close()
            QMessageBox.critical(self, "OCR エラー", f"OCR に失敗しました:\n{exc}")
            return
        dlg.close()
        if not done:
            self.statusBar().showMessage("OCR をキャンセルしました", 3000)
            return
        ret = QMessageBox.question(
            self, "OCR 完了",
            f"検索可能 PDF を作成しました:\n{out}\n\n今すぐ開きますか？",
        )
        if ret == QMessageBox.StandardButton.Yes:
            self.open_path(out)  # 新しいタブで開く
