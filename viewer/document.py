"""PDF ドキュメントのラッパ。

レンダリングと情報取得を担当する。Step 1 では表示のみを扱い、
編集・保存（回転の永続化など）は後続ステップで pikepdf を併用して実装する。
"""
from __future__ import annotations

import os
import re

import fitz  # PyMuPDF
from PySide6.QtGui import QImage, QPixmap

from . import govform_js


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# 直線・矢印の線端スタイル（キー名 → PyMuPDF の PDF_ANNOT_LE_* 定数）。
# UI側（main_window.py）はこのキー名でコンボボックスの選択肢を組み立てる。
LINE_END_STYLES = {
    "none": fitz.PDF_ANNOT_LE_NONE,
    "open_arrow": fitz.PDF_ANNOT_LE_OPEN_ARROW,
    "closed_arrow": fitz.PDF_ANNOT_LE_CLOSED_ARROW,
    "r_open_arrow": fitz.PDF_ANNOT_LE_R_OPEN_ARROW,
    "r_closed_arrow": fitz.PDF_ANNOT_LE_R_CLOSED_ARROW,
    "square": fitz.PDF_ANNOT_LE_SQUARE,
    "circle": fitz.PDF_ANNOT_LE_CIRCLE,
    "diamond": fitz.PDF_ANNOT_LE_DIAMOND,
    "butt": fitz.PDF_ANNOT_LE_BUTT,
    "slash": fitz.PDF_ANNOT_LE_SLASH,
}
# 線種（キー名 → PDF の dash 配列）。None（実線）は set_border に渡さない。
LINE_DASH_PATTERNS = {
    "solid": None,
    "dashed": [6, 3],
    "dotted": [1, 3],
}

# 「指定なし（元の値を引き継ぐ）」を None（＝塗り無しに設定）と区別するための番兵。
_UNSET = object()


class PdfDocument:
    """PyMuPDF の Document を薄くラップしたクラス。"""

    _UNDO_MAX = 60

    def __init__(self) -> None:
        self._doc: fitz.Document | None = None
        self.path: str | None = None
        self.modified: bool = False
        self._authenticated: bool = True
        self._doc_scripts_cache: dict[str, str] | None = None
        # 注釈の Undo/Redo（ページ単位のスナップショット）
        self._undo: list = []   # [(page_index, [desc,...]), ...] 変更前の状態
        self._redo: list = []

    # --- ライフサイクル -------------------------------------------------
    def open(self, path: str, password: str | None = None) -> None:
        self.close()
        # ファイルをメモリに読み込んでから開く（ディスク上のファイルをロックしない）。
        # これにより、開いたままでもエクスプローラでのリネーム/移動が可能になる。
        with open(path, "rb") as f:
            data = f.read()
        self._doc = fitz.open(stream=data, filetype="pdf")
        self.path = path
        self.modified = False
        self._authenticated = not self._doc.needs_pass
        self._doc_scripts_cache = None
        self._undo.clear()   # 別ファイルを開いたら注釈 Undo 履歴は無効
        self._redo.clear()
        # 保持する doc には「正しいパスワードでのみ」authenticate する。
        # 誤パスワードの照合は check_password（使い捨て）で行うこと。
        if self._doc.needs_pass and password:
            self.authenticate(password)

    @property
    def needs_password(self) -> bool:
        # authenticate 後に needs_pass が下がらない版があるため自前フラグで判定
        return bool(self._doc) and self._doc.needs_pass and not self._authenticated

    @property
    def is_encrypted(self) -> bool:
        return bool(self._doc) and self._doc.is_encrypted

    @staticmethod
    def check_password(path: str, password: str) -> bool:
        """使い捨ての複製でパスワードを照合する。

        MuPDF は誤パスワードで authenticate すると「その時点で開いている」
        暗号化 doc の復号が壊れる。そのため照合は使い捨てインスタンスで行い、
        本体は「正しいパスワードが分かってから」開くこと（open_file 参照）。
        """
        try:
            with open(path, "rb") as f:
                data = f.read()
            tmp = fitz.open(stream=data, filetype="pdf")
            ok = (not tmp.needs_pass) or tmp.authenticate(password) > 0
            tmp.close()
            return ok
        except Exception:  # noqa: BLE001
            return False

    def authenticate(self, password: str) -> bool:
        if not self._doc:
            return False
        ok = self._doc.authenticate(password) > 0
        if ok:
            self._authenticated = True
        return ok

    def close(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None
            self.path = None
            self.modified = False
            self._doc_scripts_cache = None

    @property
    def is_open(self) -> bool:
        return self._doc is not None

    @property
    def page_count(self) -> int:
        return self._doc.page_count if self._doc else 0

    # --- サイズ ---------------------------------------------------------
    def page_pixel_size(self, index: int, zoom: float) -> tuple[int, int]:
        """指定ページを zoom 倍で描画したときの (幅, 高さ) px。

        page.rect は回転を反映するため、そのまま zoom 倍すればよい
        （連続スクロール表示のプレースホルダ確保に使う）。
        """
        if not self._doc:
            return (0, 0)
        rect = self._doc.load_page(index).rect
        return (max(1, round(rect.width * zoom)), max(1, round(rect.height * zoom)))

    # --- レンダリング ---------------------------------------------------
    def render_page(self, index: int, zoom: float = 1.0) -> QPixmap:
        """指定ページを zoom 倍で描画して QPixmap を返す。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        page = self._doc.load_page(index)
        matrix = fitz.Matrix(zoom, zoom)
        # alpha=False で不透明背景にする（白紙が透過にならない）
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        return self._pixmap_from_fitz(pix)

    def render_thumbnail(self, index: int, max_width: int = 160) -> QPixmap:
        """サムネイル用の小さな描画。横幅 max_width に合わせて縮小する。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        page = self._doc.load_page(index)
        rect = page.rect
        zoom = max_width / rect.width if rect.width else 1.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return self._pixmap_from_fitz(pix)

    # --- 回転 -----------------------------------------------------------
    def rotation(self, index: int) -> int:
        """ページの現在の絶対回転角（0/90/180/270）。"""
        if not self._doc:
            return 0
        return self._doc.load_page(index).rotation

    def rotate_page(self, index: int, delta: int) -> int:
        """ページを delta 度（±90 など）相対回転し、新しい絶対角を返す。

        PyMuPDF 上で set_rotation するとメモリ上の表示に反映される。
        実ファイルへの永続化は save / save_as で標準の /Rotate に書き込む。
        """
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        page = self._doc.load_page(index)
        new_angle = (page.rotation + delta) % 360
        page.set_rotation(new_angle)
        self.modified = True
        return new_angle

    # --- 注釈（標準 PDF アノテーション） ------------------------------
    def label_to_pdf_point(self, index: int, x: float, y: float, zoom: float):
        """表示(ラベル)座標を、回転を考慮して unrotated PDF 座標へ変換する。"""
        page = self._doc.load_page(index)
        return fitz.Point(x / zoom, y / zoom) * page.derotation_matrix

    def label_to_pdf_rect(self, index, x0, y0, x1, y1, zoom):
        """表示座標の2点から PDF 座標の正規化済み矩形を作る。"""
        page = self._doc.load_page(index)
        m = page.derotation_matrix
        p0 = fitz.Point(x0 / zoom, y0 / zoom) * m
        p1 = fitz.Point(x1 / zoom, y1 / zoom) * m
        r = fitz.Rect(p0, p1)
        r.normalize()
        return r

    def add_highlight(self, index, rect, color=(1.0, 0.86, 0.18), opacity=1.0) -> None:
        self._push_undo(index)
        page = self._doc.load_page(index)
        annot = page.add_highlight_annot(rect)
        annot.set_colors(stroke=color)
        if opacity < 1.0:
            annot.set_opacity(opacity)
        annot.update()
        self.modified = True

    def add_text_highlight(self, index, p0, p1, color=(1.0, 0.86, 0.18)) -> bool:
        """p0→p1（PDF座標）の間にある文字を、語のquadに沿ってハイライトする。

        テキストが見つからなければ False を返す（呼び出し側で矩形ハイライトに
        フォールバックできる）。読み取り順で開始語〜終了語を選択する。
        """
        page = self._doc.load_page(index)
        words = page.get_text("words")  # (x0,y0,x1,y1, word, block, line, word_no)
        if not words:
            return False
        ordered = sorted(words, key=lambda w: (w[5], w[6], w[7]))

        def nearest_index(pt):
            best, bi = None, 0
            for i, w in enumerate(ordered):
                cx = (w[0] + w[2]) / 2
                cy = (w[1] + w[3]) / 2
                dist = abs(cx - pt.x) + abs(cy - pt.y)
                if best is None or dist < best:
                    best, bi = dist, i
            return bi

        i0, i1 = nearest_index(p0), nearest_index(p1)
        if i0 > i1:
            i0, i1 = i1, i0
        quads = [fitz.Rect(w[:4]).quad for w in ordered[i0:i1 + 1]]
        if not quads:
            return False
        self._push_undo(index)
        annot = page.add_highlight_annot(quads)
        annot.set_colors(stroke=color)
        annot.update()
        self.modified = True
        return True

    def add_rect(self, index, rect, color=(0.9, 0.1, 0.1), width=1.5,
                 fill=None, dashes=None, opacity=1.0) -> None:
        self._push_undo(index)
        page = self._doc.load_page(index)
        annot = page.add_rect_annot(rect)
        annot.set_colors(stroke=color, fill=fill)
        annot.set_border(width=width, dashes=dashes)
        if opacity < 1.0:
            annot.set_opacity(opacity)
        annot.update()
        self.modified = True

    def add_ink(self, index, strokes, color=(0.9, 0.1, 0.1), width=2.0, opacity=1.0) -> None:
        """strokes: PDF 座標の点列のリスト（[[Point or (x,y), ...], ...]）。"""
        self._push_undo(index)
        page = self._doc.load_page(index)
        # add_ink_annot は「float ペアの列の列」を要求するため変換する
        norm = [[(float(p.x), float(p.y)) if hasattr(p, "x") else (float(p[0]), float(p[1]))
                 for p in stroke] for stroke in strokes]
        annot = page.add_ink_annot(norm)
        annot.set_colors(stroke=color)
        annot.set_border(width=width)
        if opacity < 1.0:
            annot.set_opacity(opacity)
        annot.update()
        self.modified = True

    def add_freetext(self, index, rect, text, color=(0.0, 0.0, 0.0),
                     fontsize=12, fontname="helv") -> None:
        self._push_undo(index)
        page = self._doc.load_page(index)
        annot = page.add_freetext_annot(
            rect, text, fontsize=fontsize, fontname=fontname, text_color=color
        )
        annot.update()
        self.modified = True

    def add_text_note(self, index, point, text, icon="Note") -> None:
        """付箋（テキスト注釈）。point は PDF 座標。"""
        self._push_undo(index)
        page = self._doc.load_page(index)
        annot = page.add_text_annot(point, text, icon=icon)
        annot.update()
        self.modified = True

    # --- 追加の注釈種類（下線・取消線・直線・矢印・円） ---------------
    def add_underline(self, index, p0, p1, color=(0.1, 0.3, 0.9)) -> bool:
        """p0→p1 の文字に下線を引く（文字が無ければ False）。"""
        return self._text_markup(index, p0, p1, "underline", color)

    def add_strikeout(self, index, p0, p1, color=(0.9, 0.1, 0.1)) -> bool:
        """p0→p1 の文字に取り消し線を引く（文字が無ければ False）。"""
        return self._text_markup(index, p0, p1, "strikeout", color)

    def _text_markup(self, index, p0, p1, kind, color) -> bool:
        _text, rects = self.select_text(index, p0, p1)
        if not rects:
            return False
        self._push_undo(index)
        page = self._doc.load_page(index)
        quads = [r.quad for r in rects]
        if kind == "underline":
            annot = page.add_underline_annot(quads)
        else:
            annot = page.add_strikeout_annot(quads)
        annot.set_colors(stroke=color)
        annot.update()
        self.modified = True
        return True

    def add_line(self, index, p0, p1, color=(0.9, 0.1, 0.1), width=2.0,
                 arrow=False, start="none", end="none", dashes=None,
                 opacity=1.0) -> None:
        """直線／矢印。p0,p1 は PDF 座標。

        start・end は LINE_END_STYLES のキー名で線端の形状を指定する。
        arrow=True は後方互換のショートカットで、end が未指定（"none"）の
        場合に限り end="closed_arrow" として扱う。
        """
        self._push_undo(index)
        page = self._doc.load_page(index)
        annot = page.add_line_annot(p0, p1)
        annot.set_colors(stroke=color)
        annot.set_border(width=width, dashes=dashes)
        end_key = end if end != "none" else ("closed_arrow" if arrow else "none")
        s = LINE_END_STYLES.get(start, fitz.PDF_ANNOT_LE_NONE)
        e = LINE_END_STYLES.get(end_key, fitz.PDF_ANNOT_LE_NONE)
        if s or e:
            annot.set_line_ends(s, e)
        if opacity < 1.0:
            annot.set_opacity(opacity)
        annot.update()
        self.modified = True

    def add_circle(self, index, rect, color=(0.9, 0.1, 0.1), width=2.0,
                    fill=None, dashes=None, opacity=1.0) -> None:
        """楕円/円の注釈。"""
        self._push_undo(index)
        page = self._doc.load_page(index)
        annot = page.add_circle_annot(rect)
        annot.set_colors(stroke=color, fill=fill)
        annot.set_border(width=width, dashes=dashes)
        if opacity < 1.0:
            annot.set_opacity(opacity)
        annot.update()
        self.modified = True

    # --- 墨消し（Redaction：データごと削除） --------------------------
    def add_redaction(self, index, rect, fill=(0, 0, 0)) -> None:
        """墨消し対象を登録する（まだ削除はしない）。apply_redactions で確定。"""
        page = self._doc.load_page(index)
        page.add_redact_annot(rect, fill=fill)
        self.modified = True

    def apply_redactions(self) -> int:
        """登録済みの墨消しを全ページに適用し、対象の文字/画像を完全削除する。"""
        if not self._doc:
            return 0
        n = 0
        for i in range(self.page_count):
            page = self._doc.load_page(i)
            applied = page.apply_redactions()
            # apply_redactions の戻りは版により bool/None。件数は annots から数える。
            n += 1 if applied else 0
        self.modified = True
        return n

    def pending_redactions(self, index: int) -> int:
        """そのページの未適用の墨消し注釈数。"""
        page = self._doc.load_page(index)
        return sum(1 for a in page.annots(types=(fitz.PDF_ANNOT_REDACT,)))

    @staticmethod
    def _font_for(text: str) -> str:
        """日本語等の非ASCIIを含むなら内蔵CJKフォント、無ければ Helvetica。"""
        return "japan" if any(ord(c) > 0x7F for c in text) else "helv"

    # --- 透かし / ヘッダー・フッター ----------------------------------
    def add_watermark(self, text, fontsize=48, color=(0.5, 0.5, 0.5),
                      opacity=0.3, angle=45) -> int:
        """全ページの中央に半透明の透かし文字を斜めに重ねる。"""
        if not self._doc:
            return 0
        font = self._font_for(text)
        for i in range(self.page_count):
            page = self._doc.load_page(i)
            rect = page.rect
            tw = fitz.get_text_length(text, fontname=font, fontsize=fontsize)
            center = fitz.Point(rect.width / 2, rect.height / 2)
            point = fitz.Point(center.x - tw / 2, center.y)
            matrix = fitz.Matrix(angle)  # 中心まわりに回転
            page.insert_text(point, text, fontsize=fontsize, fontname=font,
                             color=color, fill_opacity=opacity,
                             morph=(center, matrix))
        self.modified = True
        return self.page_count

    def add_header_footer(self, left="", center="", right="", top=False,
                          fontsize=10, color=(0, 0, 0), margin=28) -> int:
        """各ページの上端/下端の左・中央・右にテキストを入れる。

        {date} {filename} {page} {total} を差し込み可能。
        """
        if not self._doc:
            return 0
        import datetime
        today = datetime.date.today().isoformat()
        fname = os.path.basename(self.path) if self.path else ""
        total = self.page_count
        for i in range(total):
            page = self._doc.load_page(i)
            rect = page.rect
            y = (margin + fontsize) if top else (rect.height - margin)
            ctx = {"date": today, "filename": fname,
                   "page": str(i + 1), "total": str(total)}
            for text, where in ((left, "l"), (center, "c"), (right, "r")):
                if not text:
                    continue
                s = text.format(**ctx)
                font = self._font_for(s)
                tw = fitz.get_text_length(s, fontname=font, fontsize=fontsize)
                if where == "l":
                    x = margin
                elif where == "r":
                    x = rect.width - margin - tw
                else:
                    x = (rect.width - tw) / 2
                page.insert_text((x, y), s, fontsize=fontsize,
                                 fontname=font, color=color)
        self.modified = True
        return total

    # --- ページ操作（白紙挿入・複製・トリミング） ---------------------
    def insert_blank_page(self, at: int, width=None, height=None) -> None:
        """at の位置に白紙ページを挿入。サイズ未指定は隣ページ or A4。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        if width is None or height is None:
            ref = min(max(at - 1, 0), self.page_count - 1) if self.page_count else 0
            if self.page_count:
                r = self._doc.load_page(ref).rect
                width, height = r.width, r.height
            else:
                width, height = 595, 842
        self._doc.new_page(pno=at, width=width, height=height)
        self.modified = True

    def duplicate_page(self, index: int) -> None:
        """index ページを複製して直後に挿入する（独立コピー）。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        self._doc.fullcopy_page(index, index + 1)
        self.modified = True

    def set_crop(self, index: int, rect) -> None:
        """表示/出力範囲(CropBox)を設定する。rect は PDF 座標。"""
        page = self._doc.load_page(index)
        page.set_cropbox(rect)
        self.modified = True

    def auto_crop_rect(self, index: int, margin=4.0):
        """ページ内の文字・図形の外接矩形＋余白を返す（自動トリミング候補）。"""
        page = self._doc.load_page(index)
        bbox = None
        for w in page.get_text("words"):
            r = fitz.Rect(w[:4])
            bbox = r if bbox is None else (bbox | r)
        if bbox is None:
            return None
        bbox = fitz.Rect(bbox.x0 - margin, bbox.y0 - margin,
                         bbox.x1 + margin, bbox.y1 + margin) & page.rect
        return bbox

    # --- メタデータ ----------------------------------------------------
    def get_metadata(self) -> dict:
        return dict(self._doc.metadata) if self._doc else {}

    def set_metadata(self, meta: dict) -> None:
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        self._doc.set_metadata(meta)
        self.modified = True

    def clear_metadata(self) -> None:
        """タイトル/著者等のメタデータを一括削除（プライバシー）。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        self._doc.set_metadata({})
        self.modified = True

    # --- 全文抽出 / HTML 書き出し -------------------------------------
    def export_text(self, out_path: str) -> None:
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        parts = []
        for i in range(self.page_count):
            parts.append(self._doc.load_page(i).get_text())
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\f".join(parts))  # ページ区切りは改ページ文字

    def export_html(self, out_path: str) -> None:
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        body = []
        for i in range(self.page_count):
            body.append(self._doc.load_page(i).get_text("html"))
        html = ("<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
                "<body>" + "\n".join(body) + "</body></html>")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)

    def full_text(self) -> str:
        if not self._doc:
            return ""
        return "\n".join(self._doc.load_page(i).get_text()
                         for i in range(self.page_count))

    def annot_count(self, index: int) -> int:
        page = self._doc.load_page(index)
        return sum(1 for _ in page.annots())

    # --- 注釈の選択・移動・編集 ---------------------------------------
    def annot_at(self, index, point):
        """指定 PDF 座標を含む最前面の注釈の xref を返す（無ければ None）。"""
        page = self._doc.load_page(index)
        hit = None
        for annot in page.annots():
            if point in annot.rect:
                hit = annot.xref  # 後勝ち＝最前面
        return hit

    def annot_rect(self, index, xref):
        """注釈の矩形（PDF 座標）。"""
        page = self._doc.load_page(index)  # page はこの関数内で生かし続ける
        annot = self._find_on(page, xref)
        return fitz.Rect(annot.rect) if annot else None

    def annot_text(self, index, xref) -> str:
        page = self._doc.load_page(index)
        annot = self._find_on(page, xref)
        return annot.info.get("content", "") if annot else ""

    def annot_is_textual(self, index, xref) -> bool:
        page = self._doc.load_page(index)
        annot = self._find_on(page, xref)
        return bool(annot) and annot.type[1] in ("FreeText", "Text")

    def pdf_rect_to_label(self, index, rect, zoom):
        """PDF 矩形を表示(ラベル)座標の (x0,y0,x1,y1) に変換する。"""
        page = self._doc.load_page(index)
        m = page.rotation_matrix
        p0 = fitz.Point(rect.x0, rect.y0) * m
        p1 = fitz.Point(rect.x1, rect.y1) * m
        xs = sorted([p0.x * zoom, p1.x * zoom])
        ys = sorted([p0.y * zoom, p1.y * zoom])
        return (xs[0], ys[0], xs[1], ys[1])

    # --- フォーム（AcroForm 入力欄）------------------------------------
    def is_form(self) -> bool:
        """入力欄付き（フォーム）PDF かどうか。"""
        return bool(self._doc) and bool(getattr(self._doc, "is_form_pdf", False))

    @staticmethod
    def _widget_kind(field_type) -> str:
        return {
            fitz.PDF_WIDGET_TYPE_TEXT: "text",
            fitz.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
            fitz.PDF_WIDGET_TYPE_COMBOBOX: "combo",
            fitz.PDF_WIDGET_TYPE_LISTBOX: "list",
            fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
            # 官公庁フォームの「丸で選択」は素の RADIOBUTTON ではなく BUTTON
            # 型のペア（同じ rect・別名の "選択中/非選択" 外観）で作られている
            # ことが多い（実サンプルで確認済み）。UI 側では radio と同様に
            # 「クリックで選択する」ものとして扱いたいので radio 扱いにする。
            fitz.PDF_WIDGET_TYPE_BUTTON: "radio",
        }.get(field_type, "text")

    def page_fields(self, index: int) -> list:
        """指定ページのフォーム欄一覧。各要素は dict（rect は fitz.Rect）。"""
        if not self._doc:
            return []
        COMB = 1 << 24       # 1文字ずつマス目に入れる欄
        MULTILINE = 1 << 12  # 複数行欄
        page = self._doc.load_page(index)
        out = []
        for w in (page.widgets() or []):
            # choice_values は文字列のほか (エクスポート値, 表示名) のタプルのことがある
            choices = []
            for c in (getattr(w, "choice_values", None) or []):
                if isinstance(c, (list, tuple)):
                    choices.append(str(c[1] if len(c) > 1 else c[0]))
                else:
                    choices.append(str(c))
            is_text = w.field_type == fitz.PDF_WIDGET_TYPE_TEXT
            maxlen = int(getattr(w, "text_maxlen", 0) or 0)
            flags = int(getattr(w, "field_flags", 0) or 0)
            out.append({
                "xref": w.xref,
                "name": w.field_name or "",
                "kind": self._widget_kind(w.field_type),
                "value": w.field_value if w.field_value is not None else "",
                "rect": w.rect,
                "choices": choices,
                "maxlen": maxlen,
                "comb": bool(is_text and maxlen and (flags & COMB)),
                "multiline": bool(is_text and (flags & MULTILINE)),
                # 0 は「自動（枠に合わせて縮小）」の意味（Adobe と同じ）
                "fontsize": float(getattr(w, "text_fontsize", 0) or 0),
                # 官公庁フォームの JS トグル判定用（クリックハンドラの有無）。
                "script": getattr(w, "script", None) or "",
                # 表示状態(0=表示,1=非表示,2=画面のみ,3=印刷のみ)。画面に出す
                # かどうかの判定に使う（_PRINT 欄など画面非表示の欄を除外）。
                "display": int(getattr(w, "field_display", 0) or 0),
            })
        return out

    def set_field_value(self, index: int, xref: int, value) -> bool:
        """xref で欄を特定し値を設定して外観を更新する。成功で True。"""
        if not self._doc:
            return False
        page = self._doc.load_page(index)
        for w in (page.widgets() or []):
            if w.xref == xref:
                try:
                    if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                        # field_value=True は、ON 状態名が非ASCII（日本語を
                        # UTF-16 で PDF 名にしたもの。官公庁フォームで実在）だと
                        # PyMuPDF が対応しきれず反映されないことがある。/AS・/V を
                        # 直接書いて確実にチェック/解除する。
                        if not self._set_checkbox(w, bool(value)):
                            return False
                    else:
                        w.field_value = "" if value is None else str(value)
                        self._fit_text_fontsize(w)
                        w.update()
                    self.modified = True
                    return True
                except Exception:  # noqa: BLE001
                    return False
        return False

    def _set_checkbox(self, w, on: bool) -> bool:
        """チェックボックスの ON/OFF を /AS・/V を直接書いて反映する。

        ON にするときは外観辞書(/AP/N)にある "Off" 以外の状態名を使う。
        /V はフィールド（親があれば親、無ければウィジェット自身）に、/AS は
        見た目を持つウィジェットに書く。
        """
        xref = w.xref
        if on:
            apn = self._doc.xref_get_key(xref, "AP/N")
            if not apn or apn[0] != "dict":
                return False
            keys = re.findall(r"/([^ /<>]+)\s+\d+\s+0\s+R", apn[1])
            on_states = [k for k in keys if k != "Off"]
            if not on_states:
                return False
            name = "/" + on_states[0]
        else:
            name = "/Off"
        parent = self._doc.xref_get_key(xref, "Parent")
        v_xref = int(parent[1].split()[0]) if parent and parent[0] == "xref" else xref
        self._doc.xref_set_key(v_xref, "V", name)
        self._doc.xref_set_key(xref, "AS", name)
        return True

    # --- 官公庁フォームの「丸で選択」 JS トグル（限定対応） -------------
    def doc_scripts(self) -> dict[str, str]:
        """文書レベル JavaScript（Names/JavaScript）を遅延取得してキャッシュする。"""
        if self._doc_scripts_cache is None:
            self._doc_scripts_cache = (
                govform_js.extract_doc_scripts(self._doc) if self._doc else {}
            )
        return self._doc_scripts_cache

    def is_govform_js_eligible(self) -> bool:
        """GroupButtonClick/KinmuButtonClick 等、既知の安全なヘルパーを
        この文書自身が定義しているか（= 対応スコープ内のフォームか）。"""
        return govform_js.is_eligible(self.doc_scripts())

    def govform_click_target_at(self, index: int, rect):
        """rect とほぼ一致し、現在表示中(display 0/2)で govform クリック
        スクリプトを持つウィジェットの xref を返す（無ければ None）。

        「〇で選択」の _01(選択) と _OFF(非選択) は同じ矩形を共有し、常に
        どちらか一方だけが表示される。UI 側はクリックのたびにこれを呼び、
        「今クリックすべき可視側」を引き直す。こうすることで選択/解除の
        どちらでも overlay を作り直さずに正しい対象を実行できる。
        """
        if not self._doc:
            return None
        try:
            target = fitz.Rect(rect)
            fallback = None
            for w in (self._doc.load_page(index).widgets() or []):
                if int(getattr(w, "field_display", 0) or 0) in (1, 3):
                    continue  # 非表示（Hidden/印刷のみ）は対象外
                script = (getattr(w, "script", None) or "").strip()
                if not (script and govform_js.SIMPLE_CALL_RE.match(script)):
                    continue
                wr = w.rect
                if (abs(wr.x0 - target.x0) < 1.0 and abs(wr.y0 - target.y0) < 1.0
                        and abs(wr.x1 - target.x1) < 1.0
                        and abs(wr.y1 - target.y1) < 1.0):
                    return w.xref               # 矩形一致（本命）
                if fallback is None and wr.intersects(target):
                    fallback = w.xref           # 念のための保険
            return fallback
        except Exception:  # noqa: BLE001
            return None

    def click_govform_widget(self, index: int, xref: int) -> bool:
        """官公庁フォームの選択ウィジェットをクリックした効果を再現する。

        表示状態（/F の Hidden ビット）が実際に変化すれば True を返し、
        modified フラグを立てる。対象外・失敗時は何も変えず False。
        """
        if not self._doc:
            return False
        changed = govform_js.run_click(self._doc, index, xref, self.doc_scripts())
        if changed:
            self.modified = True
        return changed

    def govform_combo_export_value(self, index: int, xref: int, display_text: str):
        """コンボの表示テキストから、対応するエクスポート値を求める。

        choice_values は文字列か (エクスポート値, 表示名) のタプル。JS の
        event.changeEx にはエクスポート値を渡す必要がある（表示名しか一致
        しなければ、そのまま表示名を使う）。
        """
        if not self._doc:
            return display_text
        try:
            page = self._doc.load_page(index)
            for w in (page.widgets() or []):
                if w.xref != xref:
                    continue
                for c in (getattr(w, "choice_values", None) or []):
                    if isinstance(c, (list, tuple)):
                        export = str(c[0])
                        face = str(c[1] if len(c) > 1 else c[0])
                        if face == display_text or export == display_text:
                            return export
                    elif str(c) == display_text:
                        return str(c)
        except Exception:  # noqa: BLE001
            pass
        return display_text

    def commit_govform_combo(self, index: int, xref: int, display_text: str) -> bool:
        """コンボ選択時の JS（_PRINT 転写・枝ラベル表示切替）を再現する。

        値/表示が実際に変化すれば True を返し modified を立てる。
        """
        if not self._doc:
            return False
        export = self.govform_combo_export_value(index, xref, display_text)
        changed = govform_js.run_keystroke(
            self._doc, index, xref, self.doc_scripts(), export)
        if changed:
            self.modified = True
        return changed

    def govform_unsupported_helpers(self) -> list[str]:
        """このフォームが使う入力補助のうち、まだ未対応のヘルパー名一覧。

        空でなければ「未対応の入力補助あり」＝ユーザーに知らせ、報告用に
        書き出せるようにする対象。
        """
        if not self._doc:
            return []
        return govform_js.unsupported_helpers(self._doc, self.doc_scripts())

    def govform_telemetry_payload(self, app_version: str = ""):
        """未対応フォーム機能の匿名テレメトリ用ペイロード（無ければ None）。

        含めるのはフォーム由来の定義と版・指紋のみ（個人情報・入力値は含まない）。
        """
        if not self._doc:
            return None
        names = self.govform_unsupported_helpers()
        if not names:
            return None
        from . import telemetry
        scripts = self.doc_scripts()
        sources = govform_js._all_function_bodies("\n".join(scripts.values()))
        import fitz as _fitz
        return telemetry.build_payload(
            scripts, names, {n: sources.get(n, "") for n in names},
            app_version, getattr(_fitz, "__version__", ""))

    def write_govform_diagnostic(self, out_path: str,
                                 app_version: str = "") -> list[str]:
        """未対応ヘルパーの報告用テキストを out_path に書き出す。

        書き出した未対応ヘルパー名一覧を返す（空なら何も書かない）。
        含めるのはフォーム側のスクリプト定義のみで、ユーザー入力値は含めない。
        """
        if not self._doc:
            return []
        names = self.govform_unsupported_helpers()
        if not names:
            return []
        import fitz as _fitz
        header = {
            "ファイル名": os.path.basename(self.path) if self.path else "(不明)",
            "ページ数": self.page_count,
            "アプリ版": app_version or "(不明)",
            "PyMuPDF": getattr(_fitz, "__version__", "(不明)"),
        }
        text = govform_js.build_diagnostic(
            self._doc, self.doc_scripts(), names, header)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        return names

    @staticmethod
    def _fit_text_fontsize(w) -> None:
        """1行テキスト欄の文字サイズを枠の幅にも収める（Adobe の自動縮小相当）。

        MuPDF の外観生成は高さ基準でしかサイズを決めないため、長い文字列が
        枠の右へあふれて描画される。自動サイズ（0）の欄と、指定サイズでは
        収まらない欄は、実測幅から収まるサイズに詰める。
        """
        COMB = 1 << 24
        MULTILINE = 1 << 12
        if w.field_type != fitz.PDF_WIDGET_TYPE_TEXT:
            return
        flags = int(getattr(w, "field_flags", 0) or 0)
        if flags & (COMB | MULTILINE):  # コム欄・複数行は MuPDF 側の配置に任せる
            return
        text = str(w.field_value or "")
        if not text:
            return
        rect = w.rect
        avail_w = max(4.0, rect.width - 4)
        avail_h = max(4.0, rect.height)
        # 「japan」フォントで実測（ASCII は半角幅・かな漢字は全角幅で測れる）
        unit = fitz.get_text_length(text, fontname="japan", fontsize=1)
        cur = float(getattr(w, "text_fontsize", 0) or 0)
        base = cur if cur > 0 else avail_h * 0.75
        if unit <= 0:
            return
        fit = min(base, avail_w / unit, avail_h * 0.9)
        if cur == 0 or unit * cur > avail_w:
            w.text_fontsize = max(4.0, fit)

    @staticmethod
    def _find_on(page, xref):
        for annot in page.annots():
            if annot.xref == xref:
                return annot
        return None

    def _describe(self, annot) -> dict:
        colors = annot.colors or {}
        border = annot.border or {}
        desc = {
            "kind": annot.type[1],
            "rect": fitz.Rect(annot.rect),
            "vertices": annot.vertices,
            "stroke": colors.get("stroke") or (0.0, 0.0, 0.0),
            "fill": colors.get("fill"),
            "width": border.get("width", 1.5) or 1.5,
            "dashes": list(border["dashes"]) if border.get("dashes") else None,
            "line_ends": annot.line_ends,
            "opacity": annot.opacity,
            "content": annot.info.get("content", ""),
        }
        if desc["kind"] == "FreeText":
            # 文字サイズと文字色は /DA に入る（annot.colors の stroke は枠線色で
            # あって本文色ではない）。ここで拾わないと移動/リサイズ/Undo で
            # サイズも色も失われる。
            desc["fontsize"], desc["text_color"] = self._parse_freetext_da(annot)
        return desc

    def _parse_freetext_da(self, annot):
        """FreeText の /DA 文字列からフォントサイズと本文色を取り出す。"""
        fs, tc = 12.0, (0.0, 0.0, 0.0)
        try:
            da = self._doc.xref_get_key(annot.xref, "DA")
            s = da[1] if da and da[0] == "string" else ""
            m = re.search(r"([\d.]+)\s+Tf", s)
            if m:
                fs = float(m.group(1))
            m = re.search(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg", s)
            if m:
                tc = tuple(float(m.group(i)) for i in (1, 2, 3))
            else:
                m = re.search(r"([\d.]+)\s+g\b", s)  # グレースケール
                if m:
                    v = float(m.group(1))
                    tc = (v, v, v)
        except Exception:  # noqa: BLE001
            pass
        return fs, tc

    def _recreate(self, page, desc, dx=0.0, dy=0.0, color=None, width=None,
                  text=None, new_rect=None, dashes=_UNSET, opacity=_UNSET,
                  fill=_UNSET):
        """捕捉した注釈情報から、平行移動・リサイズ・色/太さ/線種/不透明度/塗り/
        本文を変更して作り直す。

        page は呼び出し側が delete に使ったのと同一の Page オブジェクトを渡す
        （別インスタンスを混在させると PyMuPDF がクラッシュするため）。
        new_rect 指定時はリサイズ（Ink/Line/下線/取消線は点列を旧矩形→新矩形へ
        スケール）。dashes/opacity/fill を指定しなければ元の注釈から引き継ぐ
        （_UNSET＝未指定。fill=None は「塗り無し」を明示的に設定する意味）。
        """
        kind = desc["kind"]
        stroke = color if color is not None else desc["stroke"]
        w = width if width is not None else desc["width"]
        content = text if text is not None else desc["content"]
        old = desc["rect"]
        fill = desc.get("fill") if fill is _UNSET else fill
        dashes = desc.get("dashes") if dashes is _UNSET else dashes
        opacity = desc.get("opacity") if opacity is _UNSET else opacity
        if new_rect is not None:
            target = fitz.Rect(new_rect)
        else:
            target = fitz.Rect(old.x0 + dx, old.y0 + dy, old.x1 + dx, old.y1 + dy)

        if kind == "Highlight":
            annot = page.add_highlight_annot(target)
            annot.set_colors(stroke=stroke)
        elif kind == "Square":
            annot = page.add_rect_annot(target)
            annot.set_colors(stroke=stroke, fill=fill)
            annot.set_border(width=w, dashes=dashes)
        elif kind == "Circle":
            annot = page.add_circle_annot(target)
            annot.set_colors(stroke=stroke, fill=fill)
            annot.set_border(width=w, dashes=dashes)
        elif kind == "FreeText":
            fs = desc.get("fontsize", 12) or 12
            tc = color if color is not None else desc.get("text_color", stroke)
            annot = page.add_freetext_annot(target, content, fontsize=fs,
                                            text_color=tc)
        elif kind == "Text":
            annot = page.add_text_annot(fitz.Point(target.x0, target.y0), content)
        elif kind == "Ink":
            annot = page.add_ink_annot(self._map_ink(desc["vertices"], old, target))
            annot.set_colors(stroke=stroke)
            annot.set_border(width=w)
        elif kind == "Line":
            v = desc["vertices"] or [(old.x0, old.y0), (old.x1, old.y1)]
            p0, p1 = self._map_ink([v], old, target)[0]
            annot = page.add_line_annot(p0, p1)
            annot.set_colors(stroke=stroke)
            annot.set_border(width=w, dashes=dashes)
            ends = desc.get("line_ends")
            if ends:
                annot.set_line_ends(ends[0], ends[1])
        elif kind in ("Underline", "StrikeOut"):
            quads = self._map_quads(desc["vertices"], old, target)
            if not quads:
                return None
            annot = (page.add_underline_annot(quads) if kind == "Underline"
                     else page.add_strikeout_annot(quads))
            annot.set_colors(stroke=stroke)
        else:
            return None
        if opacity is not None and 0 <= opacity < 1.0:
            annot.set_opacity(opacity)
        annot.update()
        self.modified = True
        return annot.xref

    @staticmethod
    def _map_ink(vertices, old, target):
        """点列を旧矩形 old から新矩形 target へ平行移動＋スケールする。"""
        ow = old.width or 1.0
        oh = old.height or 1.0
        sx = target.width / ow
        sy = target.height / oh
        out = []
        for stroke_pts in (vertices or []):
            out.append([
                (target.x0 + (p[0] - old.x0) * sx, target.y0 + (p[1] - old.y0) * sy)
                for p in stroke_pts
            ])
        return out

    @staticmethod
    def _map_quads(vertices, old, target):
        """下線/取消線の quad 点列（4点区切り）を旧矩形→新矩形へ変換する。"""
        ow = old.width or 1.0
        oh = old.height or 1.0
        sx = target.width / ow
        sy = target.height / oh
        pts = [(target.x0 + (p[0] - old.x0) * sx, target.y0 + (p[1] - old.y0) * sy)
               for p in (vertices or [])]
        quads = []
        for i in range(0, len(pts) - 3, 4):
            grp = pts[i:i + 4]
            xs = [q[0] for q in grp]
            ys = [q[1] for q in grp]
            quads.append(fitz.Rect(min(xs), min(ys), max(xs), max(ys)).quad)
        return quads

    def _edit_annot(self, index, xref, **kwargs):
        """同一 Page 上で注釈を捕捉→削除→再作成する共通処理。"""
        page = self._doc.load_page(index)
        annot = self._find_on(page, xref)
        if not annot:
            return None
        self._push_undo(index)
        desc = self._describe(annot)
        page.delete_annot(annot)
        return self._recreate(page, desc, **kwargs)

    def delete_annot_xref(self, index, xref) -> bool:
        """xref 指定で注釈を削除する。"""
        page = self._doc.load_page(index)
        annot = self._find_on(page, xref)
        if annot is None:
            return False
        self._push_undo(index)
        page.delete_annot(annot)
        self.modified = True
        return True

    def move_annot(self, index, xref, dx, dy):
        """注釈を (dx,dy) PDF 単位で平行移動。新しい xref を返す。"""
        return self._edit_annot(index, xref, dx=dx, dy=dy)

    def resize_annot(self, index, xref, new_rect):
        """注釈を new_rect（PDF座標）にリサイズする。"""
        return self._edit_annot(index, xref, new_rect=new_rect)

    def recolor_annot(self, index, xref, color):
        return self._edit_annot(index, xref, color=color)

    def restyle_annot(self, index, xref, width=None, dashes=_UNSET,
                      opacity=_UNSET, fill=_UNSET):
        """選択済み注釈の太さ・線種(dashes)・不透明度・塗りを後から変更する。

        未指定（_UNSET / None）の項目は元の値を引き継ぐ。fill=None は「塗り無し」。
        変更後の新しい xref を返す（作り直しのため xref は変わる）。
        """
        return self._edit_annot(index, xref, width=width, dashes=dashes,
                                opacity=opacity, fill=fill)

    def set_annot_text(self, index, xref, text):
        return self._edit_annot(index, xref, text=text)

    def describe_annot(self, index, xref):
        """注釈の内容を dict で取り出す（コピー用）。無ければ None。"""
        page = self._doc.load_page(index)
        annot = self._find_on(page, xref)
        return self._describe(annot) if annot else None

    def paste_annot(self, index, desc, dx=12.0, dy=12.0):
        """describe_annot で取った内容を、少しずらして新しい注釈として貼り付ける。
        新しい xref を返す。"""
        if not desc or not self._doc:
            return None
        self._push_undo(index)
        page = self._doc.load_page(index)
        return self._recreate(page, desc, dx=dx, dy=dy)

    def delete_annot_at(self, index, point) -> bool:
        """指定 PDF 座標を含む最前面の注釈を1つ削除する。"""
        page = self._doc.load_page(index)
        target = None
        for annot in page.annots():
            if point in annot.rect:
                target = annot  # 後勝ち＝最前面
        if target is not None:
            self._push_undo(index)
            page.delete_annot(target)
            self.modified = True
            return True
        return False

    def clear_annots(self, index: int) -> int:
        page = self._doc.load_page(index)
        annots = list(page.annots())
        if annots:
            self._push_undo(index)
        for annot in annots:
            page.delete_annot(annot)
        if annots:
            self.modified = True
        return len(annots)

    # --- 注釈の Undo / Redo（ページ単位のスナップショット）--------------
    def _annot_snapshot(self, index: int) -> list:
        """ページ index の全注釈を _describe のリストで捕捉する。"""
        page = self._doc.load_page(index)
        return [self._describe(a) for a in page.annots()]

    def _push_undo(self, index: int) -> None:
        """注釈を変更する直前に、そのページの現状を Undo スタックへ積む。"""
        if not self._doc:
            return
        try:
            self._undo.append((index, self._annot_snapshot(index)))
        except Exception:  # noqa: BLE001
            return
        if len(self._undo) > self._UNDO_MAX:
            self._undo.pop(0)
        self._redo.clear()  # 新しい変更が入ったら Redo は無効化

    def _restore_snapshot(self, index: int, snap: list) -> None:
        page = self._doc.load_page(index)
        for a in list(page.annots()):
            page.delete_annot(a)
        for desc in snap:
            self._recreate(page, desc)
        self.modified = True

    def undo_annot(self):
        """直前の注釈変更を取り消す。対象ページ番号を返す（無ければ None）。"""
        if not self._undo or not self._doc:
            return None
        index, snap = self._undo.pop()
        try:
            self._redo.append((index, self._annot_snapshot(index)))
            self._restore_snapshot(index, snap)
        except Exception:  # noqa: BLE001
            return None
        return index

    def redo_annot(self):
        """取り消した注釈変更をやり直す。対象ページ番号を返す（無ければ None）。"""
        if not self._redo or not self._doc:
            return None
        index, snap = self._redo.pop()
        try:
            self._undo.append((index, self._annot_snapshot(index)))
            self._restore_snapshot(index, snap)
        except Exception:  # noqa: BLE001
            return None
        return index

    def can_undo_annot(self) -> bool:
        return bool(self._undo)

    def can_redo_annot(self) -> bool:
        return bool(self._redo)

    # --- ページ編集（並べ替え・削除・統合） ---------------------------
    def move_page(self, src: int, dst: int) -> None:
        """src ページを dst の位置へ移動（並べ替え）。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        if src == dst:
            return
        # move_page は dst の「前」に挿入する。末尾へ送る場合に対応する。
        self._doc.move_page(src, dst if dst < self.page_count else -1)
        self.modified = True

    def reorder(self, new_order: list[int]) -> None:
        """new_order（旧インデックスの並び）の順にページを並べ替える。

        全ページを過不足なく含むこと。fitz の select はこの配列の順序で
        ページを取り直すため、並べ替えがそのまま反映される。
        """
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        if sorted(new_order) != list(range(self.page_count)):
            raise ValueError("並べ替え指定が不正です")
        self._doc.select(new_order)
        self.modified = True

    def delete_page(self, index: int) -> None:
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        if self.page_count <= 1:
            raise ValueError("最後の1ページは削除できません")
        self._doc.delete_page(index)
        self.modified = True

    def delete_pages(self, indices: list[int]) -> None:
        """複数ページをまとめて削除する（順序は維持）。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        drop = set(indices)
        keep = [i for i in range(self.page_count) if i not in drop]
        if not keep:
            raise ValueError("すべてのページは削除できません")
        self._doc.select(keep)
        self.modified = True

    def insert_pdf(self, other_path: str, at: int | None = None) -> int:
        """別の PDF を取り込む。at=None なら末尾に追加。挿入したページ数を返す。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        with fitz.open(other_path) as other:
            n = other.page_count
            start_at = self.page_count if at is None else at
            self._doc.insert_pdf(other, start_at=start_at)
        self.modified = True
        return n

    def insert_images_as_pages(
        self, image_paths: list[str], at: int | None = None
    ) -> int:
        """画像ファイルをページとして取り込む。挿入したページ数を返す。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        start = self.page_count if at is None else at
        added = 0
        for p in image_paths:
            with fitz.open(p) as img_doc:
                pdf_bytes = img_doc.convert_to_pdf()
            with fitz.open("pdf", pdf_bytes) as img_pdf:
                self._doc.insert_pdf(img_pdf, start_at=start + added)
                added += img_pdf.page_count
        self.modified = True
        return added

    # --- 画像変換 -------------------------------------------------------
    def export_page_images(
        self,
        indices: list[int],
        out_dir: str,
        fmt: str = "png",
        dpi: int = 150,
        stem: str = "page",
    ) -> list[str]:
        """指定ページを画像（PNG/JPEG）として書き出す。生成パス一覧を返す。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        ext = fmt.lower().lstrip(".")
        if ext in ("jpg", "jpeg"):
            ext = "jpg"
        elif ext != "png":
            raise ValueError("対応形式は PNG / JPEG です")
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        outputs: list[str] = []
        for i in indices:
            page = self._doc.load_page(i)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            out_path = os.path.join(out_dir, f"{stem}_{i + 1:03d}.{ext}")
            pix.save(out_path)
            outputs.append(out_path)
        return outputs

    def deskew_all(self, dpi: int = 200, limit: float = 8.0,
                   skip_text_pages: bool = True, progress=None) -> int:
        """スキャンページの傾きを補正する。補正したページ数を返す。

        - テキストを持つページ（born-digital）は触らず原本を保持
        - 画像/スキャンページは傾き角を推定し、補正画像で作り直す
        progress(done, total) が False を返したら中断（変更は破棄）。
        """
        import io

        from PIL import Image

        from . import deskew as deskew_mod

        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        new = fitz.open()
        corrected = 0
        total = self.page_count
        try:
            for i in range(total):
                page = self._doc.load_page(i)
                if skip_text_pages and page.get_text().strip():
                    new.insert_pdf(self._doc, from_page=i, to_page=i)
                else:
                    pix = page.get_pixmap(dpi=dpi, alpha=False)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    angle = deskew_mod.estimate_skew(img, limit=limit)
                    if abs(angle) >= 0.2:
                        img = img.rotate(angle, resample=Image.Resampling.BICUBIC,
                                         fillcolor=(255, 255, 255), expand=False)
                        corrected += 1
                    buf = io.BytesIO()
                    img.save(buf, format="png")
                    with fitz.open("png", buf.getvalue()) as imgdoc:
                        with fitz.open("pdf", imgdoc.convert_to_pdf()) as ip:
                            new.insert_pdf(ip)
                if progress and not progress(i + 1, total):
                    new.close()
                    return -1  # 中断
        except Exception:
            new.close()
            raise
        # 元ドキュメントを差し替える
        self._doc.close()
        self._doc = new
        self.modified = True
        return corrected

    @staticmethod
    def images_to_pdf(image_paths: list[str], out_path: str) -> None:
        """画像ファイル群から 1 つの PDF を作成する（各画像=1ページ）。"""
        if not image_paths:
            raise ValueError("画像が指定されていません")
        new = fitz.open()
        try:
            for p in image_paths:
                with fitz.open(p) as img_doc:
                    pdf_bytes = img_doc.convert_to_pdf()
                with fitz.open("pdf", pdf_bytes) as img_pdf:
                    new.insert_pdf(img_pdf)
            new.save(out_path)
        finally:
            new.close()

    # --- OCR（文字認識：透明テキスト層を付与） -----------------------
    def ocr_to(
        self,
        out_path: str,
        language: str,
        tessdata: str,
        dpi: int = 300,
        skip_text_pages: bool = True,
        progress=None,
    ) -> bool:
        """全ページを OCR し、検索可能 PDF を out_path に保存する。

        - 既にテキストを持つページは（skip_text_pages 時）再OCRせず原本を保持
        - 画像/スキャンページは dpi で描画し、透明テキスト層付き PDF に変換
        - PyMuPDF(MuPDF) 内蔵の Tesseract を使うため、生成物は標準の検索可能PDF
        progress(done, total) が False を返したらキャンセルして False を返す。
        """
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        new = fitz.open()
        try:
            total = self.page_count
            for i in range(total):
                page = self._doc.load_page(i)
                if skip_text_pages and page.get_text().strip():
                    new.insert_pdf(self._doc, from_page=i, to_page=i)
                else:
                    pix = page.get_pixmap(dpi=dpi)
                    pdf_bytes = pix.pdfocr_tobytes(
                        language=language, tessdata=tessdata
                    )
                    with fitz.open("pdf", pdf_bytes) as opdf:
                        new.insert_pdf(opdf)
                if progress and not progress(i + 1, total):
                    return False  # キャンセル
            new.save(out_path, garbage=4, deflate=True)
        finally:
            new.close()
        return True

    # --- 抽出・分割（新しいファイルを書き出す） -----------------------
    def extract_to(self, indices: list[int], out_path: str) -> None:
        """指定ページ（0始まり・任意の順序）だけを新しい PDF として保存。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        new = fitz.open()
        try:
            for i in indices:
                new.insert_pdf(self._doc, from_page=i, to_page=i)
            new.save(out_path, garbage=4, deflate=True)
        finally:
            new.close()

    def split_every(self, n: int, out_dir: str, stem: str) -> list[str]:
        """n ページごとに分割して複数ファイルを書き出す。生成パス一覧を返す。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        if n < 1:
            raise ValueError("分割単位は1以上にしてください")
        outputs: list[str] = []
        total = self.page_count
        part = 1
        for start in range(0, total, n):
            end = min(start + n - 1, total - 1)
            out_path = os.path.join(out_dir, f"{stem}_part{part:02d}.pdf")
            new = fitz.open()
            try:
                new.insert_pdf(self._doc, from_page=start, to_page=end)
                new.save(out_path, garbage=4, deflate=True)
            finally:
                new.close()
            outputs.append(out_path)
            part += 1
        return outputs

    # --- 保存（標準準拠・回転や編集を反映） ---------------------------
    def save_as(self, out_path: str) -> None:
        """現在のドキュメント（回転・並べ替え・削除・統合を反映）を保存する。

        PyMuPDF の save は /Rotate を標準どおり書き出すため、Adobe 以外の
        どのビューワーでも同じ表示になる。garbage=4 で不要オブジェクトを
        整理し、deflate で未圧縮ストリームのみ圧縮する（既存画像は再圧縮しない）。
        開いているファイル自身へ上書きする場合は一時ファイル経由で置き換える。
        """
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        opening_self = self.path and _same_path(self.path, out_path)
        if opening_self:
            tmp = out_path + ".tmp_save"
            self._doc.save(tmp, garbage=4, deflate=True)
            self._doc.close()
            os.replace(tmp, out_path)
            # 置き換え後もロックしないよう stream で開き直す
            with open(out_path, "rb") as f:
                data = f.read()
            self._doc = fitz.open(stream=data, filetype="pdf")
        else:
            self._doc.save(out_path, garbage=4, deflate=True)
        self.path = out_path
        self.modified = False

    def save(self) -> None:
        """元のファイルに上書き保存する。"""
        if not self.path:
            raise RuntimeError("保存先が不明です")
        self.save_as(self.path)

    # --- パスワード保護 / 解除 ----------------------------------------
    def save_encrypted(self, out_path: str, user_pw: str, owner_pw: str | None = None) -> None:
        """AES-256 でパスワード保護して別名保存する。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        if self.path and _same_path(self.path, out_path):
            raise ValueError("開いているファイルとは別名で保存してください")
        self._doc.save(
            out_path,
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw=owner_pw or user_pw,
            user_pw=user_pw,
            permissions=-1,  # すべて許可（印刷/コピー/抽出が制限されないように）
            garbage=3,
            deflate=True,
        )

    def save_decrypted(self, out_path: str) -> None:
        """暗号化を解除して別名保存する（認証済みであること）。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        if self.path and _same_path(self.path, out_path):
            raise ValueError("開いているファイルとは別名で保存してください")
        self._doc.save(
            out_path, encryption=fitz.PDF_ENCRYPT_NONE, garbage=3, deflate=True
        )

    # --- 検索 -----------------------------------------------------------
    def search(self, query: str, max_hits: int = 2000) -> list:
        """全ページから query を検索し [(page_index, fitz.Rect), ...] を返す。"""
        if not self._doc or not query:
            return []
        results = []
        for i in range(self.page_count):
            page = self._doc.load_page(i)
            for rect in page.search_for(query):
                results.append((i, fitz.Rect(rect)))
                if len(results) >= max_hits:
                    return results
        return results

    def _page_chars(self, index):
        """ページの文字を読み取り順で [(rect, char, line_id), ...] にする。"""
        page = self._doc.load_page(index)
        raw = page.get_text("rawdict")
        chars = []
        line_id = 0
        for block in raw.get("blocks", []):
            if block.get("type", 0) != 0:  # 0 = テキストブロックのみ
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    for ch in span.get("chars", []):
                        chars.append((fitz.Rect(ch["bbox"]), ch["c"], line_id))
                line_id += 1
        return chars

    def select_text(self, index, p0, p1):
        """p0→p1（PDF座標）の間を「文字単位」で選択する。

        返り値 (text, rects)。text は選択文字列（行が変わると改行）、
        rects は行ごとにまとめた矩形（PDF座標、描画用）。テキストが無ければ ("", [])。
        クリック位置が文字の左右どちら寄りかでキャレット位置を決め、横方向も正確に。
        """
        if not self._doc:
            return ("", [])
        chars = self._page_chars(index)
        if not chars:
            return ("", [])

        def caret(pt):
            # 行(Y)を重視して最寄りの文字を選び、左右半分でキャレット位置を決める
            best, bi = None, 0
            for i, (r, _c, _lid) in enumerate(chars):
                cy = (r.y0 + r.y1) / 2
                cx = (r.x0 + r.x1) / 2
                score = abs(cy - pt.y) * 3 + abs(cx - pt.x)
                if best is None or score < best:
                    best, bi = score, i
            r = chars[bi][0]
            mid = (r.x0 + r.x1) / 2
            return bi + 1 if pt.x > mid else bi  # 右半分なら文字の後ろ

        c0, c1 = caret(p0), caret(p1)
        if c0 > c1:
            c0, c1 = c1, c0
        sel = chars[c0:c1]
        if not sel:
            return ("", [])

        text_parts, rects = [], []
        cur, prev_line = None, None
        for r, c, lid in sel:
            if prev_line is not None and lid != prev_line:
                text_parts.append("\n")
                if cur is not None:
                    rects.append(cur)
                cur = None
            text_parts.append(c)
            cur = fitz.Rect(r) if cur is None else (cur | r)
            prev_line = lid
        if cur is not None:
            rects.append(cur)
        return ("".join(text_parts), rects)

    def add_search_highlights(self, hits: list, color=(1.0, 0.86, 0.18)) -> int:
        """検索ヒット [(page_index, rect), ...] をまとめてハイライト注釈にする。"""
        if not self._doc:
            return 0
        for index, rect in hits:
            page = self._doc.load_page(index)
            annot = page.add_highlight_annot(rect)
            annot.set_colors(stroke=color)
            annot.update()
        if hits:
            self.modified = True
        return len(hits)

    # --- ページ番号（標準テキストとして焼き込み） ---------------------
    def add_page_numbers(self, position="bottom-center", fmt="{n}",
                         start=1, fontsize=11, margin=36, color=(0, 0, 0)) -> int:
        """各ページにページ番号を挿入する。fmt は {n} と {total} が使える。

        位置: bottom/top の center/right/left の組み合わせ。
        本文テキストとして埋め込むため、どのビューワーでも表示される。
        """
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        total = self.page_count
        for i in range(total):
            page = self._doc.load_page(i)
            text = fmt.format(n=start + i, total=total)
            rect = page.rect
            w, h = rect.width, rect.height
            font = self._font_for(text)
            tw = fitz.get_text_length(text, fontname=font, fontsize=fontsize)
            if "right" in position:
                x = w - margin - tw
            elif "left" in position:
                x = margin
            else:  # center
                x = (w - tw) / 2
            y = (margin + fontsize) if "top" in position else (h - margin)
            page.insert_text((x, y), text, fontsize=fontsize, color=color, fontname=font)
        self.modified = True
        return total

    # --- しおり / 目次（標準アウトライン） ----------------------------
    def get_toc(self) -> list:
        """[[level, title, page(1始まり)], ...] を返す。"""
        if not self._doc:
            return []
        return self._doc.get_toc(simple=True)

    def set_toc(self, toc: list) -> None:
        """目次（アウトライン）を設定する。標準PDFのしおりとして保存される。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        self._doc.set_toc(toc)
        self.modified = True

    def add_bookmark(self, title: str, page1: int, level: int = 1) -> None:
        """末尾にしおりを1件追加（page1 は1始まり）。"""
        toc = self.get_toc()
        toc.append([max(1, level), title, page1])
        self.set_toc(toc)

    # --- 最適化（無劣化圧縮） -----------------------------------------
    def compress_to(self, out_path: str) -> None:
        """不要オブジェクト整理＋ストリーム/フォント/画像の deflate で軽量化保存。

        既存の JPEG 等は再圧縮しない（無劣化）。構造の冗長を除いてサイズを縮める。
        """
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        kwargs = dict(garbage=4, deflate=True, deflate_images=True,
                      deflate_fonts=True, clean=True)
        if self.path and _same_path(self.path, out_path):
            tmp = out_path + ".tmp_save"
            self._doc.save(tmp, **kwargs)
            self._doc.close()
            os.replace(tmp, out_path)
            self._doc = fitz.open(out_path)
            self.path = out_path
            self.modified = False
        else:
            self._doc.save(out_path, **kwargs)

    # --- PDF/A-2b 化（pikepdf で OutputIntent + XMP 付与） -------------
    SRGB_ICC = r"C:\Windows\System32\spool\drivers\color\sRGB Color Space Profile.icm"

    def export_pdfa(self, out_path: str) -> None:
        """PDF/A-2b 化して書き出す。

        Ghostscript を使わず pikepdf で sRGB の OutputIntent と PDF/A 識別用 XMP を
        付与する。完全準拠はフォント埋め込み等にも依存するため、厳密な検証は
        別途バリデータ推奨（基本的な PDF/A 構造は満たす）。
        """
        import pikepdf

        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        if not os.path.exists(self.SRGB_ICC):
            raise RuntimeError("sRGB ICC プロファイルが見つかりません")

        # 現在の内容（注釈・回転等を反映）を一時PDFへ
        tmp = out_path + ".pre_pdfa"
        self._doc.save(tmp, garbage=4, deflate=True)
        try:
            with pikepdf.open(tmp) as pdf:
                with open(self.SRGB_ICC, "rb") as f:
                    icc = f.read()
                icc_stream = pdf.make_stream(icc)
                icc_stream.N = 3  # sRGB = 3 成分
                oi = pdf.make_indirect(pikepdf.Dictionary(
                    Type=pikepdf.Name.OutputIntent,
                    S=pikepdf.Name("/GTS_PDFA1"),
                    OutputConditionIdentifier=pikepdf.String("sRGB IEC61966-2.1"),
                    Info=pikepdf.String("sRGB IEC61966-2.1"),
                    DestOutputProfile=icc_stream,
                ))
                pdf.Root.OutputIntents = pikepdf.Array([oi])
                with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                    meta["pdfaid:part"] = "2"
                    meta["pdfaid:conformance"] = "B"
                pdf.save(out_path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    @staticmethod
    def _pixmap_from_fitz(pix: "fitz.Pixmap") -> QPixmap:
        image = QImage(
            pix.samples,
            pix.width,
            pix.height,
            pix.stride,
            QImage.Format.Format_RGB888,
        )
        # QImage は元バッファを参照するため copy() で実体を確保する
        return QPixmap.fromImage(image.copy())
