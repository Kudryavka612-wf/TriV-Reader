"""PDF ドキュメントのラッパ。

レンダリングと情報取得を担当する。Step 1 では表示のみを扱い、
編集・保存（回転の永続化など）は後続ステップで pikepdf を併用して実装する。
"""
from __future__ import annotations

import os
import re
from collections import OrderedDict
from contextlib import contextmanager

import fitz  # PyMuPDF
from PySide6.QtGui import QImage, QPixmap

from . import fastwidgets, govform_js, jpfonts

# 埋め込みの無い和文を、中国語の字形でなく Windows の和文フォントで描く
jpfonts.install()


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def friendly_error(exc: Exception, saving: bool = False) -> str:
    """PyMuPDF/OS の英語の例外を、利用者に伝わる日本語にする。

    そのまま出すと "Failed to open stream" や "code=2: cannot open file" と
    いった、原因も対処も読み取れない文字列が画面に出てしまう。
    """
    name = type(exc).__name__
    text = str(exc)
    if isinstance(exc, FileNotFoundError) or "No such file" in text:
        return ("保存先のフォルダが見つかりません。" if saving
                else "ファイルが見つかりません。移動または削除された可能性があります。")
    if isinstance(exc, PermissionError) or "denied" in text or "Access is denied" in text:
        return ("保存できませんでした。ほかのアプリ（Acrobat など）でこのファイルを"
                "開いていないか、書き込み権限があるかを確認してください。")
    if getattr(exc, "errno", None) == 28 or "No space" in text:
        return "保存先の空き容量が足りません。"
    if name == "EmptyFileError" or "empty" in text.lower():
        return "ファイルの中身が空です（0 バイト）。"
    if name in ("FileDataError", "FzErrorFormat") or "Failed to open stream" in text:
        return ("PDF として読み込めませんでした。ファイルが壊れているか、"
                "PDF ではない可能性があります。")
    if name == "FzErrorSystem":
        return ("保存できませんでした。保存先のフォルダが無いか、書き込みできません。"
                if saving else "ファイルを開けませんでした。")
    return text


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

# 文字の位置を読むときの指定。既定は画像の中身（スキャン PDF では 1 ページ数 MB）まで
# 取り出すので、文字だけでよい選択・語の判定では外す（スキャンのページが 1/10 の時間になる）
_TEXT_FLAGS = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES


def _span_dist(lo: float, hi: float, v: float) -> float:
    """区間 [lo, hi] と点 v の距離（中にあれば 0）。"""
    if v < lo:
        return lo - v
    if v > hi:
        return v - hi
    return 0.0


def _char_class(ch: str) -> str:
    """ダブルクリックの語選択で使う文字種。同じ種類が続く範囲を 1 語とみなす。"""
    if not ch or ch.isspace():
        return "space"
    if ch.isascii():
        return "word" if (ch.isalnum() or ch == "_") else "symbol"
    code = ord(ch)
    if 0x3040 <= code <= 0x309F:
        return "hira"
    if 0x30A0 <= code <= 0x30FF or ch in "ー－":
        return "kata"
    if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
        return "kanji"
    if 0xFF10 <= code <= 0xFF5A:      # 全角英数
        return "word"
    return "symbol"


# --- テキスト注釈（FreeText）の折り返し ----------------------------------
# MuPDF は FreeText の外観を作るとき、空白でしか折り返さない（句読点を含むと
# 別の経路に入って折り返すこともあるが、当てにならない）。句読点の無い日本語は
# 1 行のまま枠の右で切られて消えるので、改行はこちらで入れてから渡す。
FT_LINE = 1.2   # 行送り（文字サイズの倍率）。MuPDF の FreeText と同じ

# 行頭に来てはいけない文字（前の行にぶら下げる）
_KINSOKU_HEAD = set("、。，．,.)）]］}｝」』】〕〉》’”ゝゞー々！？!?ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ・：；:;")


def ft_line_width(line: str, fontsize: float, fontname: str = "helv") -> float:
    """1 行を FreeText で描いたときの幅（pt）。

    欧文は MuPDF と同じ基本 14 フォントで測る。それ以外（かな・漢字・記号）は
    1 文字 1 em とみなす（MuPDF の簡易経路は半角カナも 1 em で並べるので、
    狭く見積もってはみ出すよりは広めに取る）。
    """
    fn = fontname if fontname in ("helv", "tiro", "cour") else "helv"
    width = 0.0
    run: list[str] = []
    wide = False   # 直前の文字が和文か（空白は直前の文字のフォントで描かれる）
    for ch in line:
        if ch == " " and wide:
            width += fontsize   # 和文の後ろの空白は全角幅になる
            continue
        wide = not (ord(ch) < 0x100 and ch != "\t")
        if not wide:
            run.append(ch)
            continue
        if run:
            width += fitz.get_text_length("".join(run), fontname=fn, fontsize=fontsize)
            run.clear()
        width += fontsize
    if run:
        width += fitz.get_text_length("".join(run), fontname=fn, fontsize=fontsize)
    return width


def _ft_tokens(para: str) -> list[str]:
    """折り返しの単位に切る。欧文は語（後ろの空白込み）、それ以外は 1 文字ずつ。"""
    tokens: list[str] = []
    for m in re.finditer(r"[\x21-\xff]+[ ]*|[ ]+|.", para):
        tokens.append(m.group(0))
    return tokens


def wrap_freetext(text: str, width: float, fontsize: float,
                  fontname: str = "helv") -> str:
    """text を幅 width（pt）に収まるよう改行を入れて返す（元の改行は保つ）。"""
    out: list[str] = []
    for para in text.split("\n"):
        line = ""
        for tok in _ft_tokens(para):
            cand = line + tok
            if not line or ft_line_width(cand.rstrip(" "), fontsize, fontname) <= width:
                line = cand
                continue
            if tok[0] in _KINSOKU_HEAD:
                line = cand          # 句読点は前の行にぶら下げる
                continue
            if ft_line_width(tok.rstrip(" "), fontsize, fontname) > width:
                # 1 語で幅を超える（長い URL など）は文字単位で割る
                for ch in tok:
                    if line and ft_line_width((line + ch).rstrip(" "), fontsize,
                                              fontname) > width:
                        out.append(line.rstrip(" "))
                        line = ""
                    line += ch
                continue
            out.append(line.rstrip(" "))
            line = tok.lstrip(" ") if tok.strip() else ""
        out.append(line.rstrip(" ") if line.strip() else line)
    return "\n".join(out)


def ft_needed_size(wrapped: str, fontsize: float, fontname: str = "helv") -> tuple:
    """改行済みの本文を切らずに描くのに要る (幅, 高さ) pt。"""
    lines = wrapped.split("\n")
    w = max((ft_line_width(ln, fontsize, fontname) for ln in lines), default=0.0)
    # 最終行のディセンダ（g や y の下）まで入る高さ
    h = len(lines) * FT_LINE * fontsize + 0.3 * fontsize
    return w + 1.0, h


# --- 注釈の当たり判定 -----------------------------------------------------
def _seg_dist(p, a, b) -> float:
    """点 p と線分 a-b の距離。"""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((p.x - ax) ** 2 + (p.y - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((p.x - ax) * dx + (p.y - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx, ay + t * dy
    return ((p.x - cx) ** 2 + (p.y - cy) ** 2) ** 0.5


def _rect_dist(p, r) -> float:
    """点 p と矩形 r の距離（中にあれば 0）。"""
    dx = _span_dist(r.x0, r.x1, p.x)
    dy = _span_dist(r.y0, r.y1, p.y)
    return (dx * dx + dy * dy) ** 0.5


def _pt(v) -> tuple:
    return (float(v[0]), float(v[1])) if not hasattr(v, "x") else (float(v.x), float(v.y))


class PdfDocument:
    """PyMuPDF の Document を薄くラップしたクラス。"""

    _UNDO_MAX = 60
    # ページ操作の Undo は文書まるごと控えるので、深さとメモリの両方で抑える。
    _UNDO_DOC_MAX = 5
    _UNDO_DOC_BYTES = 192 * 1024 * 1024
    _LAYOUT_CACHE_MAX = 8   # 文字レイアウトを憶えておくページ数
    _WHOLE_CACHE_MAX = 2000   # 「ページ全体を選んだ結果」を憶えておくページ数（小さい）
    # 注釈に記録する作成者名。設定ダイアログから
    # PdfDocument.annot_author = "..." で全ドキュメントに効かせる。
    annot_author: str = ""

    # ページ描画キャッシュの上限（バイト）。A4 を等倍表示で 20 ページ強。
    RENDER_CACHE_BYTES: int = 128 * 1024 * 1024

    def __init__(self) -> None:
        self._doc: fitz.Document | None = None
        self.path: str | None = None
        self._modified: bool = False
        # 表示内容の版。中身が変わるたびに増え、描画キャッシュの鍵に使う。
        self._epoch: int = 0
        # オブジェクト番号（xref）の版。保存の整理などで番号が付け直されたら
        # 増える。入力欄・選択中の注釈・注釈一覧は xref を覚えているので、
        # これが変わったら作り直す（MainWindow._after_renumber）。
        self.xref_epoch: int = 0
        self._renumber_listeners: list = []
        # ページの構成・向きを変える操作の直前に呼ぶ関数（画面側が入力中の文字を
        # 書き込み、古いページ番号を持つ検索結果・文字選択を捨てる）
        self._pre_change_listeners: list = []
        self._in_pre_change = False
        self.undo_notice: str = ""   # 直前の取り消し・やり直しで、利用者に知らせたいこと
        # ページ描画のキャッシュ（LRU）。スクロールで画面外に出たページを
        # 戻ったときに描き直さずに済ませる。合計バイト数で上限を決める。
        self._render_cache: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        self._render_bytes: int = 0
        # フォーム欄の一覧。ズームのたびに overlay を作り直すので、
        # 中身が変わらない限り widgets() の再走査を避ける。
        self._fields_cache: dict[int, list] = {}
        self._authenticated: bool = True
        # パスワード付きで開いた文書の保護（開いた時のパスワード・権限）。None=保護なし。
        # 開いている文書は復号済みの作業用複製で、保存のたびにこれで付け直す
        self._protect: dict | None = None
        self._doc_scripts_cache: dict[str, str] | None = None
        # 直前のコンボ選択でフォームの JS が変えた欄（画面の入力欄に反映する）
        self.last_form_effects = None
        # 欄の名前 → フォームの入力チェックの誤りの印（画面だけ。PDF には書かない）
        self.field_marks: dict[str, bool] = {}
        # 文字選択用のレイアウト。ドラッグ中はマウスを動かすたびに参照される
        # ので、毎回 rawdict を解析すると重い。ページをまたぐ選択でも効くよう
        # 数ページ分を憶えておく。
        self._layout_cache: dict[int, tuple] = {}
        self._whole_cache: dict[int, tuple] = {}   # ページ → (全文, 行ごとの矩形)
        # ページ内リンク（目次のジャンプ・URL）。ホバーのたびに get_links() を
        # 呼ぶと重いので、ページごとに憶えておく。中身が変われば捨てる。
        self._links_cache: dict[int, list] = {}
        # Undo/Redo。注釈（ページ単位のスナップショット）とページ操作
        # （文書まるごとのスナップショット）を 1 本の履歴にまとめて持つ。
        # 種類を混ぜても、時系列に戻すぶんには前後の状態が食い違わない。
        # 要素: (kind, index, payload, label)
        #   kind "annot" → payload は注釈の記述リスト
        #   kind "doc"   → payload は PDF のバイト列
        self._undo: list = []
        self._redo: list = []
        self._undo_bytes = 0    # doc スナップショットの合計サイズ
        self._undo_group = 0    # >0 の間は 1 操作としてまとめる
        self._group_pushed = False

    # --- 変更フラグと表示キャッシュ -------------------------------------
    # 「中身が変わった＝描いてあるものも変わった」ので、modified を立てる箇所
    # すべてでキャッシュを捨てられるよう、代入をプロパティで受ける。
    @property
    def modified(self) -> bool:
        return self._modified

    @modified.setter
    def modified(self, value: bool) -> None:
        if value:
            self._invalidate_view_caches()
        self._modified = bool(value)

    def _invalidate_view_caches(self) -> None:
        """描画・フォーム欄のキャッシュを捨てて版を進める。"""
        self._epoch += 1
        self._render_cache.clear()
        self._render_bytes = 0
        self._fields_cache.clear()
        self._layout_cache.clear()
        self._whole_cache.clear()
        self._links_cache.clear()

    def _cache_render(self, key: tuple, pixmap: "QPixmap") -> None:
        """描画結果を LRU に入れ、上限を超えた分を古い順に捨てる。"""
        size = pixmap.width() * pixmap.height() * max(1, pixmap.depth()) // 8
        if size > self.RENDER_CACHE_BYTES:
            return  # 1 枚で上限を超える巨大ページはキャッシュしない
        self._render_cache[key] = pixmap
        self._render_bytes += size
        while self._render_bytes > self.RENDER_CACHE_BYTES and self._render_cache:
            _k, old = self._render_cache.popitem(last=False)
            self._render_bytes -= (old.width() * old.height()
                                   * max(1, old.depth()) // 8)

    def _protection_kwargs(self) -> dict:
        """保存先にパスワードを付け直す指定（保護なしなら空）。

        保存は AES-256 で、開いたときのパスワードを開くパスワード・所有者パスワード
        の両方にし、権限（印刷・コピー等の許可）は元のままにする。元の所有者
        パスワードは MuPDF が教えてくれないので引き継げない（開くパスワードと同じになる）。
        """
        if not self._protect:
            return {}
        return {"encryption": fitz.PDF_ENCRYPT_AES_256,
                "user_pw": self._protect["user_pw"],
                "owner_pw": self._protect["owner_pw"],
                "permissions": self._protect["permissions"]}

    def _adopt_decrypted(self, password: str) -> None:
        """認証できた暗号化文書を、復号済みの作業用複製に差し替える。

        MuPDF は、認証済みの暗号化文書で needs_pass を読んだり tobytes() を呼んだり
        すると復号の鍵を壊し、その後の保存が「全ページ白紙・題名が文字化け」のファイル
        になる（保存・印刷・ページ操作の取り消し用の控えが、どれもこの道を通る）。
        認証した直後に 1 度だけ tobytes() で復号したバイト列を取り、以降はその複製
        を作業用の文書にする。保存のたびに _protection_kwargs() で保護を付け直す。
        """
        old = self._doc
        permissions = old.permissions   # 読むだけなら安全（先に控える）
        data = old.tobytes()
        self._doc = fitz.open("pdf", data)
        old.close()
        self._protect = {"user_pw": password, "owner_pw": password,
                         "permissions": permissions}
        self._invalidate_view_caches()

    def _save_doc(self, path: str, **kwargs) -> None:
        """開いている文書を path に保存する。開いている文書の番号は変えない。

        MuPDF の不要オブジェクトの整理（garbage=2 以上）は、保存のついでに
        開いている文書のオブジェクト番号まで詰め直す。番号は入力欄・選択中の
        注釈・注釈一覧が覚えているので、以前は保存した後に入れた値が PDF に
        書き込まれず、黙って消えていた。整理は複製の方でだけ行う。
        """
        for key, value in self._protection_kwargs().items():
            # パスワード付きの文書は、指定が無ければパスワードを付けたまま保存する
            # （PyMuPDF の既定は「外す」で、上書き保存しただけで保護が外れていた）
            kwargs.setdefault(key, value)
        if self._doc.is_encrypted and not self._protect:
            # 開くパスワードの無い暗号化（編集・印刷の制限だけ）はそのまま引き継ぐ。
            # needs_pass は読まない（認証済みの文書では鍵を壊す）
            kwargs.setdefault("encryption", fitz.PDF_ENCRYPT_KEEP)
        if (int(kwargs.get("garbage", 0) or 0) < 2
                or kwargs.get("encryption") == fitz.PDF_ENCRYPT_KEEP):
            # 暗号化を引き継ぐ文書は複製すると外れてしまう。直接保存し、番号が変わった
            # ことを知らせる
            self._doc.save(path, **kwargs)
            if int(kwargs.get("garbage", 0) or 0) >= 2:
                self._renumbered()
            return
        work = fitz.open("pdf", self._doc.tobytes())
        try:
            work.save(path, **kwargs)
        finally:
            work.close()

    def add_pre_change_listener(self, fn) -> None:
        """ページの構成・向きを変える操作の直前に呼ぶ関数を登録する。"""
        if fn not in self._pre_change_listeners:
            self._pre_change_listeners.append(fn)

    def _notify_pre_change(self) -> None:
        if self._in_pre_change:
            return
        self._in_pre_change = True
        try:
            for fn in list(self._pre_change_listeners):
                try:
                    fn()
                except Exception:  # noqa: BLE001  画面側の都合で操作を失敗させない
                    pass
        finally:
            self._in_pre_change = False

    def add_renumber_listener(self, fn) -> None:
        """オブジェクト番号が付け直されたときに呼ぶ関数を登録する。"""
        if fn not in self._renumber_listeners:
            self._renumber_listeners.append(fn)

    def _renumbered(self) -> None:
        self.xref_epoch += 1
        self._invalidate_view_caches()
        for fn in list(self._renumber_listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001  画面側の都合で保存を失敗させない
                pass

    @staticmethod
    def _replace_atomically(path: str, write) -> None:
        """write(一時ファイルのパス) で書いてから、path に置き換える。

        保存先へ直接書くと、書き込みの途中で失敗した（容量不足・USB を抜いた・
        ネットワークが切れた）とき、そこにあった既存のファイルが切り詰められて壊れ、
        開けなくなる。一時ファイルに書き切ってから置き換えれば、失敗しても元のファイルは
        そのまま残る。失敗したら一時ファイルも消す（OneDrive に残さない）。
        """
        tmp = path + ".tmp_save"
        try:
            write(tmp)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _save_to(self, path: str, **kwargs) -> None:
        """開いている文書を path に保存する（元のファイルを壊さない書き方）。"""
        self._replace_atomically(path, lambda tmp: self._save_doc(tmp, **kwargs))

    def _save_over_self(self, out_path: str, **kwargs) -> None:
        """開いているファイル自身へ上書きする（安全な順序で置き換える）。

        先に自分を閉じてから os.replace すると、他アプリがファイルを掴んで
        いた場合（Acrobat で開いている・OneDrive が同期中・ウイルス対策が
        走っている、いずれも Windows では珍しくない）に置き換えが失敗し、
        閉じたきりの文書だけが残って未保存の編集ごと巻き添えになる。
        置き換えが成功してから開き直す。
        """
        self._save_to(out_path, **kwargs)
        # 開き直さない。開いている文書は保存した中身と同じで、ファイルも
        # 掴んでいない（メモリに読んで開いている）。開き直すとオブジェクト
        # 番号が付け直され、入力欄などが古い番号を指したままになる。

    def _finish_annot(self, annot) -> None:
        """注釈の仕上げ。作成者名を入れてから外観を確定する。

        既に作成者が入っている注釈（他のアプリで作られたもの）は書き換えない。
        """
        if self.annot_author:
            try:
                if not (annot.info or {}).get("title"):
                    annot.set_info(title=self.annot_author)
            except Exception:  # noqa: BLE001
                pass  # 作成者名は付けられなくても注釈自体は残す
        self._update_annot(annot)

    def _update_annot(self, annot) -> None:
        """注釈の外観を作り直す。テキスト注釈は和文を日本語として書き込む。

        MuPDF は言語の指定が無いと簡体中国語として扱い、漢字を中国語フォント
        （/Song）で書き込む（Acrobat でも中国語の字形になる）。"""
        freetext = annot.type[1] == "FreeText"
        if freetext:
            try:
                annot.set_language("ja")
            except Exception:  # noqa: BLE001
                pass
        annot.update()
        if freetext:
            _fs, _tc, fn = self._parse_freetext_da(annot)
            jpfonts.japanize_ap(self._doc, annot.xref, serif=(fn == "tiro"))

    def _japanize_before_saving(self) -> None:
        try:
            self.japanize_all()
        except Exception:  # noqa: BLE001  直せなくても保存そのものは止めない
            pass

    def japanize_all(self) -> int:
        """入力欄・テキスト注釈の外観にある中国語扱いの和文フォントを、日本語の
        ものに差し替える（以前の版で書き込んだ分も、保存の前に直す）。"""
        if not self._doc:
            return 0
        n = 0
        kinds = (fitz.PDF_WIDGET_TYPE_TEXT, fitz.PDF_WIDGET_TYPE_COMBOBOX,
                 fitz.PDF_WIDGET_TYPE_LISTBOX)
        for page in self._doc:
            for w in fastwidgets.widgets(page):
                if w.field_type in kinds and jpfonts.japanize_ap(
                        self._doc, w.xref, serif=jpfonts.is_mincho(w.text_font or "")):
                    n += 1
            for a in page.annots(types=(fitz.PDF_ANNOT_FREE_TEXT,)):
                _fs, _tc, fn = self._parse_freetext_da(a)
                if jpfonts.japanize_ap(self._doc, a.xref, serif=(fn == "tiro")):
                    n += 1
        return n

    # --- ライフサイクル -------------------------------------------------
    def open(self, path: str, password: str | None = None) -> None:
        self.close()
        # ファイルをメモリに読み込んでから開く（ディスク上のファイルをロックしない）。
        # これにより、開いたままでもエクスプローラでのリネーム/移動が可能になる。
        try:
            with open(path, "rb") as f:
                data = f.read()
            self._doc = fitz.open(stream=data, filetype="pdf")
        except Exception as exc:  # noqa: BLE001  英語の内部エラーを出さない
            raise RuntimeError(friendly_error(exc)) from exc
        if self._doc.page_count == 0 and not self._doc.needs_pass:
            # ページが 1 枚も無い（ページの木が壊れた）PDF は、開けたように見えて、
            # 以後のどの操作も「ドキュメントが開かれていません」になる
            self._doc.close()
            self._doc = None
            raise RuntimeError(
                "このファイルにはページが 1 枚もありません（壊れている可能性があります）")
        self._invalidate_view_caches()  # 別の中身に差し替えた
        self.path = path
        self.modified = False
        self._authenticated = not self._doc.needs_pass
        self._doc_scripts_cache = None
        self.field_marks = {}
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
        # 認証した文書は復号済みの作業用複製に差し替えるので、_protect でも見る
        return self._protect is not None or (bool(self._doc) and self._doc.is_encrypted)

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
        # 認証の前に読む（認証済みの文書で needs_pass を読むと、復号の鍵を壊す）
        needs = bool(self._doc.needs_pass)
        ok = self._doc.authenticate(password) > 0
        if ok:
            self._authenticated = True
            if needs:
                self._adopt_decrypted(password)
        return ok

    def close(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None
            self._protect = None
            self.path = None
            self.modified = False
            self._doc_scripts_cache = None
            self._invalidate_view_caches()  # 前のファイルの絵を残さない

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
    #: 1 枚の描画に使うデバイス画素数の上限。高 DPI で DPR をそのままかけると、巨大なページ
    #: ×高倍率で数百 MB になる。超えるときは DPR を下げる（Qt が拡大して表示する）
    MAX_RENDER_PIXELS: int = 48_000_000

    def _effective_dpr(self, index: int, zoom: float, dpr: float) -> float:
        """実際に使う DPR（1.0 以上。描画が大きすぎるときは下げる）。"""
        dpr = max(1.0, float(dpr or 1.0))
        if dpr <= 1.0:
            return 1.0
        rect = self._doc.load_page(index).rect
        base = max(1.0, rect.width * zoom * rect.height * zoom)
        if base * dpr * dpr > self.MAX_RENDER_PIXELS:
            dpr = max(1.0, (self.MAX_RENDER_PIXELS / base) ** 0.5)
        return round(dpr, 3)

    def render_page(self, index: int, zoom: float = 1.0, dpr: float = 1.0) -> QPixmap:
        """指定ページを zoom 倍で描画して QPixmap を返す。

        dpr はディスプレイの表示倍率（devicePixelRatio）。Windows の表示拡大 125〜200% で
        論理 px のまま描くと、Qt が拡大して表示するので文字がにじむ。dpr 倍の細かさで描き、
        QPixmap に devicePixelRatio を付ける（論理サイズ＝dpr 1 のときと同じ）。

        同じページを同じ倍率で描く要求はキャッシュから返す（スクロールで
        往復したときや、開いた直後にレイアウトが数回決め直されるときに効く）。
        中身が変わればキャッシュは丸ごと捨てられるので、古い絵は出ない。
        """
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        dpr = self._effective_dpr(index, zoom, dpr)
        key = (index, round(zoom, 4), dpr)
        cached = self._render_cache.get(key)
        if cached is not None:
            self._render_cache.move_to_end(key)
            return cached
        page = self._doc.load_page(index)
        matrix = fitz.Matrix(zoom * dpr, zoom * dpr)
        # alpha=False で不透明背景にする（白紙が透過にならない）
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        pixmap = self._pixmap_from_fitz(pix)
        pixmap.setDevicePixelRatio(dpr)
        self._cache_render(key, pixmap)
        return pixmap

    def render_page_without_annots(self, index: int, zoom: float = 1.0,
                                   dpr: float = 1.0) -> QPixmap:
        """注釈を描かずにページを描く（テキスト注釈の編集中の下地に使う）。

        編集中の注釈そのものが下に透けて二重に見えないようにしつつ、罫線や
        記入欄などの紙面は見せて、どこに書いているかを分かるようにする。
        """
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        dpr = self._effective_dpr(index, zoom, dpr)
        page = self._doc.load_page(index)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom * dpr, zoom * dpr), alpha=False,
                              annots=False)
        pixmap = self._pixmap_from_fitz(pix)
        pixmap.setDevicePixelRatio(dpr)
        return pixmap

    def page_svg(self, index: int, text_as_path: bool = True) -> str:
        """ページを SVG（ベクター）で書き出す。

        印刷でそのまま拡大してもぼやけない。text_as_path=True だと文字も図形
        として出すので、描く側にそのフォントが無くても字形が変わらない。
        """
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        page = self._doc.load_page(index)
        return page.get_svg_image(text_as_path=text_as_path)

    def render_page_image(self, index: int, zoom: float = 1.0,
                          gray: bool = False) -> "QImage":
        """印刷用に QImage で描画する。

        画面表示と違って QPixmap（描画デバイス側の資源）にする必要がなく、
        変換のぶんだけ遅くなるので QImage のまま返す。グレースケール指定なら
        1画素1バイトで描くので、データ量は 1/3・転送も速い。
        """
        from PySide6.QtGui import QImage

        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        page = self._doc.load_page(index)
        cs = fitz.csGRAY if gray else fitz.csRGB
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False,
                              colorspace=cs)
        fmt = (QImage.Format.Format_Grayscale8 if gray
               else QImage.Format.Format_RGB888)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt)
        return img.copy()  # PyMuPDF 側のバッファは後で解放されるので複製する

    def render_thumbnail(self, index: int, max_width: int = 160, dpr: float = 1.0) -> QPixmap:
        """サムネイル用の小さな描画。横幅 max_width（論理 px）に合わせて縮小する。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        page = self._doc.load_page(index)
        rect = page.rect
        zoom = max_width / rect.width if rect.width else 1.0
        dpr = self._effective_dpr(index, zoom, dpr)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom * dpr, zoom * dpr), alpha=False)
        pixmap = self._pixmap_from_fitz(pix)
        pixmap.setDevicePixelRatio(dpr)
        return pixmap

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
        self._push_doc_undo("ページの回転", index)
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
        self._finish_annot(annot)
        self.modified = True

    def add_text_highlight(self, index, p0, p1, color=(1.0, 0.86, 0.18),
                           opacity=1.0) -> bool:
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
        if opacity < 1.0:
            annot.set_opacity(opacity)
        self._finish_annot(annot)
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
        self._finish_annot(annot)
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
        self._finish_annot(annot)
        self.modified = True

    def add_freetext(self, index, rect, text, color=(0.0, 0.0, 0.0),
                     fontsize=12, fontname="helv", wrapped: str | None = None) -> int:
        """テキスト注釈を置く。新しい xref を返す。

        wrapped は画面の入力欄で見えていた折り返し（改行入り）。省略すると
        rect の幅で折り返す。どちらの場合も、文字が切れないよう枠を広げる。
        """
        self._push_undo(index)
        page = self._doc.load_page(index)
        rot = page.rotation
        # 回転したページ（横向きのスキャンなど）では、rect は回転前の座標。画面で見える
        # 向き（幅・高さ）で折り返しと枠を決め、回転前の座標に戻して、向きも渡す。
        # 渡さないと、確定した文字が横倒し・上下逆になる
        disp = self._to_display_rect(page, rect)
        if wrapped is None:
            wrapped = wrap_freetext(text, disp.width, fontsize, fontname)
        disp = self._fit_freetext_rect(disp, wrapped, fontsize, fontname)
        annot = page.add_freetext_annot(
            self._from_display_rect(page, disp), wrapped, fontsize=fontsize,
            fontname=fontname, text_color=color, rotate=rot
        )
        self._finish_annot(annot)
        self._set_soft_text(annot.xref, text, wrapped)
        self.modified = True
        return annot.xref

    @staticmethod
    def _to_display_rect(page, rect):
        """回転前の座標の rect を、画面に見える向きの rect にする（回転なしならそのまま）。"""
        r = fitz.Rect(rect)
        if not page.rotation:
            return r
        out = r * page.rotation_matrix
        out.normalize()
        return out

    @staticmethod
    def _from_display_rect(page, rect):
        r = fitz.Rect(rect)
        if not page.rotation:
            return r
        out = r * page.derotation_matrix
        out.normalize()
        return out

    @staticmethod
    def _fit_freetext_rect(rect, wrapped, fontsize, fontname):
        """改行済みの本文が切れずに入るよう、枠を右と下へ広げる（縮めはしない）。"""
        need_w, need_h = ft_needed_size(wrapped, fontsize, fontname)
        r = fitz.Rect(rect)
        return fitz.Rect(r.x0, r.y0, r.x0 + max(r.width, need_w),
                         r.y0 + max(r.height, need_h))

    def add_text_note(self, index, point, text, icon="Note") -> None:
        """付箋（テキスト注釈）。point は PDF 座標。"""
        self._push_undo(index)
        page = self._doc.load_page(index)
        annot = page.add_text_annot(point, text, icon=icon)
        self._finish_annot(annot)
        self.modified = True

    @staticmethod
    def stamp_size(text: str, fontsize: float = 20) -> tuple[float, float]:
        """スタンプの箱の大きさ (幅, 高さ) pt。置く前の見本（ゴースト）も同じ式で出す。"""
        tw = fitz.get_text_length(text or " ", fontname="japan", fontsize=fontsize)
        return max(28.0, tw + fontsize * 0.9), fontsize + fontsize * 0.7

    def add_stamp(self, index, point, text, color=(0.85, 0.1, 0.1),
                  fontsize=20, center: bool = True) -> int:
        """枠付きテキストスタンプ（承認・済・日付など）を point に置く。

        文字幅からボックスサイズを見積もり、中央寄せの枠付き FreeText にする。
        枠の色は文字色に一致する（PyMuPDF の border_width 指定の挙動）。
        """
        self._push_undo(index)
        page = self._doc.load_page(index)
        w, h = self.stamp_size(text, fontsize)
        # 回転したページでは、画面で見える向きに箱を置いて回転前の座標に戻し、向きも渡す。
        # center=True は、クリックした点が箱の中心（印鑑を押すのと同じ。左上を点に合わせると、
        # 押したつもりの所から箱の分だけ右下にずれて、置き直しが要った）
        dp = fitz.Point(point) * page.rotation_matrix if page.rotation else fitz.Point(point)
        if center:
            dp = fitz.Point(dp.x - w / 2, dp.y - h / 2)
        rect = self._from_display_rect(
            page, fitz.Rect(dp.x, dp.y, dp.x + w, dp.y + h))
        annot = page.add_freetext_annot(
            rect, text, fontsize=fontsize, text_color=color,
            border_width=2, align=1, rotate=page.rotation)  # align=1: 中央寄せ
        self._finish_annot(annot)
        self.modified = True
        return annot.xref

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
        self._finish_annot(annot)
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
        self._finish_annot(annot)
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
        self._finish_annot(annot)
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
        self._push_doc_undo("墨消しの適用", None)
        n = 0
        for i in range(self.page_count):
            page = self._doc.load_page(i)
            marks = [fitz.Rect(a.rect) for a in page.annots(types=(fitz.PDF_ANNOT_REDACT,))]
            if marks:
                self._blank_fields_under(page, marks)
                # 範囲にかかる、本文を持つ注釈（付箋・コメント付きの注釈）も消す。
                # apply_redactions は範囲に全部収まる注釈しか消さない
                for a in list(page.annots()):
                    if (a.type[0] != fitz.PDF_ANNOT_REDACT
                            and (a.info.get("content") or "").strip()
                            and any(fitz.Rect(a.rect).intersects(m) for m in marks)):
                        page.delete_annot(a)
            applied = page.apply_redactions()
            # apply_redactions の戻りは版により bool/None。件数は annots から数える。
            n += 1 if applied else 0
        self.modified = True
        return n

    def _blank_fields_under(self, page, marks: list) -> int:
        """墨消しの範囲に（少しでも）かかる入力欄の値を空にする。空にした数を返す。

        page.apply_redactions() はページの文字と絵だけを消し、入力欄（Widget）の値は
        そのまま残す。黒い帯の下に値が残るので、コピーや文字の抽出で取り出せてしまう
        （マイナンバーなどを伏せたつもりで配る使い方で、いちばん困る）。外観も作り直す。
        """
        blanked = 0
        for w in page.widgets():
            if not any(fitz.Rect(w.rect).intersects(m) for m in marks):
                continue
            try:
                if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                    if not self._set_checkbox(w, False):
                        continue
                elif w.field_type in (fitz.PDF_WIDGET_TYPE_TEXT,
                                      fitz.PDF_WIDGET_TYPE_COMBOBOX,
                                      fitz.PDF_WIDGET_TYPE_LISTBOX):
                    # PyMuPDF は空の値を書かないので、set_field_value と同じ
                    # jpfonts.update_widget で空にする
                    w.field_value = ""
                    jpfonts.update_widget(w)
                else:
                    continue
                blanked += 1
            except Exception:  # noqa: BLE001  1 欄の失敗で墨消し全体を止めない
                continue
        return blanked

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
        self._push_doc_undo("透かしの追加", None)
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
        # 書式は、変更を始める前に確かめる。途中で「{会社名}」のような未知の項目に
        # 当たると、1 ページ目だけ文字が入り、「未変更」のまま中途半端に残っていた
        probe = {"date": "", "filename": "", "page": "1", "total": "1"}
        for text in (left, center, right):
            try:
                (text or "").format(**probe)
            except (KeyError, IndexError, ValueError) as exc:
                raise ValueError(
                    f"書式に使えない項目、またはかっこがあります（{exc}）。"
                    "使えるのは {date} {filename} {page} {total} です") from exc
        self._push_doc_undo("ヘッダー/フッターの追加", None)
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
        self._push_doc_undo("白紙ページの挿入", at)
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
        if not 0 <= index < self.page_count:
            raise ValueError("複製するページが範囲外です")
        self._push_doc_undo("ページの複製", index)
        # 末尾のページは to=-1（最後へ）で複製する。to=ページ数は無効で、
        # 1 ページだけの PDF や最終ページで ValueError になっていた
        self._doc.fullcopy_page(index, index + 1 if index + 1 < self.page_count else -1)
        self.modified = True

    def set_crop(self, index: int, rect) -> None:
        """表示/出力範囲(CropBox)を設定する。rect は PDF 座標。"""
        page = self._doc.load_page(index)
        # 画面で選んだ範囲は「今のページ」（今の CropBox の左上が原点）の座標。set_cropbox は
        # MediaBox の左上が原点なので、今の CropBox の位置を足す。足さないと、トリミング済みの
        # ページをもう一度切るとき、ドラッグした所とずれた範囲が切り取られていた
        pos = page.cropbox_position
        r = fitz.Rect(rect) + (pos.x, pos.y, pos.x, pos.y)
        # ページの外へはみ出したぶんは切り詰める（ドラッグがページの端を越えただけで
        # 「CropBox not in MediaBox」の例外になり、何も切れていなかった）
        r &= fitz.Rect(page.mediabox)
        if r.is_empty or r.width < 2 or r.height < 2:
            raise ValueError("トリミングの範囲がページの外です")
        self._push_doc_undo("余白のトリミング", index)
        page.set_cropbox(r)
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
        # 文字の座標は回転前の向き。page.rect は回転後（横縦が入れ替わる）なので、
        # /Rotate 90・270 のページでは、下側の文字が切れていた。回転前のページの
        # 大きさ（今の CropBox の大きさ）で切り詰める
        cb = page.cropbox
        bbox = fitz.Rect(bbox.x0 - margin, bbox.y0 - margin,
                         bbox.x1 + margin, bbox.y1 + margin) & fitz.Rect(
                             0, 0, cb.width, cb.height)
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

    #: 文字に沿って付ける注釈（動かしたり枠で伸縮したりするものではない）
    MARKUP_KINDS = ("Highlight", "Underline", "StrikeOut", "Squiggly")

    def annot_hit(self, index, point, tol: float = 4.0, interior: bool = False):
        """point（PDF 座標）で掴める注釈の xref（無ければ None）。

        annot_at は外接矩形に入れば当たりにするため、斜めの線のまわりの空白や
        塗りの無い四角の内側をクリックしただけで掴んでしまい、その下の文字を
        選べなかった。ここでは形どおりに測る：線・ペンは線から tol 以内、塗りの
        無い四角/円は枠線の近く、ハイライト等は文字の帯の上。いくつも当たれば
        近いもの、同じ近さなら前面のものを返す。interior=True なら塗りの無い
        四角/円の内側も「いちばん弱い当たり」として数える。
        """
        page = self._doc.load_page(index)
        best = None
        for order, annot in enumerate(page.annots()):
            if _rect_dist(point, annot.rect) > tol:
                continue
            try:
                d = self._annot_distance(annot, point)
            except Exception:  # noqa: BLE001  形が読めなければ矩形で判定
                d = _rect_dist(point, annot.rect)
            if (d > tol and interior and annot.type[1] in ("Square", "Circle")
                    and fitz.Rect(annot.rect).contains(point)):
                d = tol
            if d > tol:
                continue
            key = (d, -order)
            if best is None or key < best[0]:
                best = (key, annot.xref)
        return best[1] if best else None

    @staticmethod
    def _annot_distance(annot, p) -> float:
        """注釈の「見えている形」と点 p の距離（pt）。"""
        kind = annot.type[1]
        rect = fitz.Rect(annot.rect)
        half = max(0.5, float((annot.border or {}).get("width") or 1.0) / 2)
        verts = annot.vertices
        if kind == "Ink" and verts:
            return max(0.0, min(_seg_dist(p, _pt(s[i]), _pt(s[min(i + 1, len(s) - 1)]))
                                for s in verts if s for i in range(len(s))) - half)
        if kind in ("Line", "PolyLine", "Polygon") and verts and len(verts) >= 2:
            pts = [_pt(v) for v in verts]
            if kind == "Polygon":
                pts.append(pts[0])
            return max(0.0, min(_seg_dist(p, pts[i], pts[i + 1])
                                for i in range(len(pts) - 1)) - half)
        if kind in PdfDocument.MARKUP_KINDS and verts and len(verts) >= 4:
            quads = []
            for i in range(0, len(verts) - 3, 4):
                xs = [_pt(v)[0] for v in verts[i:i + 4]]
                ys = [_pt(v)[1] for v in verts[i:i + 4]]
                quads.append(fitz.Rect(min(xs), min(ys), max(xs), max(ys)))
            return min(_rect_dist(p, q) for q in quads)
        if kind in ("Square", "Circle") and not (annot.colors or {}).get("fill"):
            # 塗りの無い図形は枠線の上だけ（内側の文字を選べるように）
            inner = rect + (half, half, -half, -half)
            if kind == "Square":
                if inner.contains(p):
                    return max(0.0, min(p.x - inner.x0, inner.x1 - p.x,
                                        p.y - inner.y0, inner.y1 - p.y) - half)
                return max(0.0, _rect_dist(p, inner) - half)
            a, b = max(inner.width / 2, 0.5), max(inner.height / 2, 0.5)
            cx, cy = (inner.x0 + inner.x1) / 2, (inner.y0 + inner.y1) / 2
            norm = (((p.x - cx) / a) ** 2 + ((p.y - cy) / b) ** 2) ** 0.5
            return max(0.0, abs(norm - 1.0) * min(a, b) - half)
        return _rect_dist(p, rect)

    def annot_kind(self, index, xref) -> str | None:
        page = self._doc.load_page(index)
        annot = self._find_on(page, xref)
        return annot.type[1] if annot else None

    def annot_rect(self, index, xref):
        """注釈の矩形（PDF 座標）。"""
        page = self._doc.load_page(index)  # page はこの関数内で生かし続ける
        annot = self._find_on(page, xref)
        return fitz.Rect(annot.rect) if annot else None

    def annot_text(self, index, xref) -> str:
        """注釈の本文。テキスト注釈は、こちらで入れた折り返しを除いた元の文。"""
        page = self._doc.load_page(index)
        annot = self._find_on(page, xref)
        return self._soft_text(annot) if annot else ""

    #: 折り返す前の本文を FreeText に控えておく独自キー。/Contents には表示
    #: どおりの改行入りを入れる（どのビューワーで外観を作り直しても同じ見た目に
    #: なる）ので、編集し直すときや幅を変えたときはこちらから組み直す。
    _SOFT_KEY = "TrivText"

    def _soft_text(self, annot) -> str:
        content = annot.info.get("content", "") or ""
        if annot.type[1] != "FreeText":
            return content
        try:
            kind, val = self._doc.xref_get_key(annot.xref, self._SOFT_KEY)
        except Exception:  # noqa: BLE001
            return content
        if kind == "string" and "".join(val.split()) == "".join(content.split()):
            return val   # 他のアプリで本文が書き換えられていたら /Contents を信じる
        return content

    def _set_soft_text(self, xref, soft: str, wrapped: str) -> None:
        if soft != wrapped:
            self._doc.xref_set_key(xref, self._SOFT_KEY, fitz.get_pdf_str(soft))
        else:
            self._doc.xref_set_key(xref, self._SOFT_KEY, "null")

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

    # --- リンク（目次ジャンプ・URL）-------------------------------------
    # PyMuPDF の get_links() が返す "from" 矩形は「回転後のページ座標」なので、
    # 表示座標 ÷ 倍率でそのまま当たり判定できる（注釈の unrotated 座標とは違う）。
    def links(self, index: int) -> list:
        """ページ内のリンク一覧。要素は dict:
        {"rect": fitz.Rect, "kind": "goto"|"uri"|"named"|"file"|"other",
         "page": int|None, "uri": str|None, "label": str}
        """
        if not self._doc:
            return []
        cached = self._links_cache.get(index)
        if cached is not None:
            return cached
        out: list = []
        try:
            raw = self._doc.load_page(index).get_links()
        except Exception:  # noqa: BLE001  壊れたリンク辞書は無視して続ける
            raw = []
        for lk in raw:
            kind = lk.get("kind")
            rect = lk.get("from")
            if rect is None or rect.is_empty:
                continue
            item = {"rect": fitz.Rect(rect), "kind": "other", "page": None,
                    "uri": None, "label": ""}
            if kind == fitz.LINK_GOTO:
                pno = lk.get("page", -1)
                if pno is None or pno < 0:
                    continue
                item.update(kind="goto", page=int(pno),
                            label=f"{int(pno) + 1} ページへ")
            elif kind == fitz.LINK_URI:
                uri = (lk.get("uri") or "").strip()
                if not uri:
                    continue
                item.update(kind="uri", uri=uri, label=uri)
            elif kind == fitz.LINK_NAMED:
                name = lk.get("name") or lk.get("nameddest") or ""
                pno = lk.get("page")
                if pno is not None and pno >= 0:
                    item.update(kind="goto", page=int(pno),
                                label=f"{int(pno) + 1} ページへ")
                else:
                    item.update(kind="named", uri=str(name), label=str(name))
            elif kind in (fitz.LINK_GOTOR, fitz.LINK_LAUNCH):
                f = lk.get("file") or ""
                item.update(kind="file", uri=str(f), label=f"別のファイル: {f}")
            else:
                continue
            out.append(item)
        self._links_cache[index] = out
        return out

    def link_at(self, index: int, x: float, y: float, zoom: float):
        """表示(ラベル)座標にあるリンクを返す。無ければ None。"""
        if not self._doc:
            return None
        pt = fitz.Point(x / zoom, y / zoom)
        for lk in self.links(index):
            if lk["rect"].contains(pt):
                return lk
        return None

    def link_boxes_label(self, index: int, zoom: float) -> list:
        """ページ内リンクの表示座標 (x0,y0,x1,y1) 一覧（ホバー表示用）。"""
        return [(r.x0 * zoom, r.y0 * zoom, r.x1 * zoom, r.y1 * zoom)
                for r in (lk["rect"] for lk in self.links(index))]

    # --- スナップショット（範囲を画像に）--------------------------------
    def snapshot_image(self, index: int, x0: float, y0: float, x1: float, y1: float,
                       zoom: float, dpi: int = 200) -> "QImage":
        """表示座標の矩形を dpi 相当の解像度で描いて QImage で返す。

        矩形を渡さなければページ全体。画面の倍率に関係なく、貼り付け先で
        粗くならない解像度で描く（get_pixmap の clip は回転後の座標）。
        """
        from PySide6.QtGui import QImage

        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        page = self._doc.load_page(index)
        clip = fitz.Rect(x0 / zoom, y0 / zoom, x1 / zoom, y1 / zoom)
        clip.normalize()
        clip &= page.rect
        if clip.is_empty:
            raise ValueError("範囲がページの外です")
        scale = max(dpi, 72) / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip,
                              alpha=False, colorspace=fitz.csRGB)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride,
                     QImage.Format.Format_RGB888)
        return img.copy()

    def page_image(self, index: int, dpi: int = 200) -> "QImage":
        """ページ全体を dpi 相当で描いた QImage（「ページを画像としてコピー」用）。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        r = self._doc.load_page(index).rect
        return self.snapshot_image(index, r.x0, r.y0, r.x1, r.y1, 1.0, dpi)

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
        """指定ページのフォーム欄一覧。各要素は dict（rect は fitz.Rect）。

        ズームのたびに入力欄の overlay を作り直すため、同じ内容に対しては
        widgets() の再走査を避けてキャッシュを返す（欄が数百あるフォームでは
        1 回 0.1 秒以上かかる）。中身が変われば modified 経由で捨てられる。
        戻り値は共有される想定なので、呼び出し側は書き換えないこと。
        """
        if not self._doc:
            return []
        cached = self._fields_cache.get(index)
        if cached is not None:
            return cached
        COMB = 1 << 24       # 1文字ずつマス目に入れる欄
        MULTILINE = 1 << 12  # 複数行欄
        READONLY = 1         # 書き換えられない欄
        from .field_ap import _align
        rules = self.field_rules()
        page = self._doc.load_page(index)
        out = []
        rows = []   # 〇欄のクリックで使う表（govform_js.page_widgets）も一緒に作る
        for w in fastwidgets.widgets(page):
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
            PASSWORD = 1 << 13    # 伏せ字の欄
            out.append({
                "xref": w.xref,
                "name": w.field_name or "",
                "kind": self._widget_kind(w.field_type),
                "ftype": w.field_type,   # 生の種類（ラジオと押しボタンは、kind では同じ "radio"）
                "on_state": (self._on_state(w.xref)
                             if w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON else ""),
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
                "align": _align(w),               # 0=左 / 1=中央 / 2=右
                "readonly": bool(flags & READONLY),
                "password": bool(is_text and (flags & PASSWORD)),
                "label": getattr(w, "field_label", None) or "",   # /TU（欄の説明）
                # フォームの入力補助（数字だけ・小さいカナを大きく・入力の注意）
                "rules": rules.get(w.field_name or ""),
            })
            rows.append((w.xref, w.field_name or "", fitz.Rect(w.rect),
                         (getattr(w, "script", None) or "").strip()))
        govform_js.remember_page_widgets(self._doc, index, rows)
        self._fields_cache[index] = out
        return out

    def field_rules(self) -> dict:
        """欄の名前 → 入力補助（govform_js.field_input_rules）。文書ごとに 1 回だけ読む。"""
        key = id(self._doc)
        if getattr(self, "_field_rules_key", None) != key:
            try:
                self._field_rules = govform_js.field_input_rules(
                    self._doc, self.doc_scripts())
            except Exception:  # noqa: BLE001  読めないフォームは補助なし
                self._field_rules = {}
            self._field_rules_key = key
        return self._field_rules

    def _widget_xref(self, index: int, xref: int, name: str | None) -> int | None:
        """欄の xref。番号で見つからなければ名前で探し直す（保存などで番号が
        付け直された後も、入れた値を確実に書き込むため）。"""
        page = self._doc.load_page(index)
        if any(x == xref and t == fitz.PDF_ANNOT_WIDGET
               for x, t, _id in page.annot_xrefs()):
            return xref
        if not name:
            return None
        for x, n, _rect, _script in govform_js.page_widgets(self._doc, index):
            if n == name:
                return x
        return None

    def set_field_value(self, index: int, xref: int, value,
                        name: str | None = None) -> bool:
        """xref（無ければ name）で欄を特定し値を設定して外観を更新する。成功で True。"""
        if not self._doc:
            return False
        xref = self._widget_xref(index, xref, name)
        if xref is None:
            return False
        page = self._doc.load_page(index)
        w = fastwidgets.widget(page, xref)
        if w is None:
            return False
        if w.field_type == fitz.PDF_WIDGET_TYPE_BUTTON:
            return False   # 押しボタンは値を持たない（文字を書かない）
        if w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON:
            # ラジオは、同じグループの 1 つだけが選ばれる。value が真ならこのボタンを選ぶ
            if not value:
                return False
            ok = self.select_radio(index, xref)
            if ok:
                self.modified = True
            return ok
        if (w.field_type != fitz.PDF_WIDGET_TYPE_CHECKBOX
                and str(w.field_value or "") == ("" if value is None else str(value))):
            # 変わっていない。保存・タブを閉じる前にカーソルのある欄を書き込むので、
            # ここで「変更あり」にすると、欄を押しただけで閉じるときに「保存
            # しますか」と聞かれていた
            return True
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
                # 外観（印刷・提出先で見える文字）はこちらで作る。欄に
                # 収まらなければ外観の中だけ縮める（/DA は書き換えない）
                jpfonts.update_widget(w)
            self.modified = True
            return True
        except Exception:  # noqa: BLE001
            return False

    def _on_state(self, xref: int) -> str:
        """チェック・ラジオの ON 状態の名前（/AP/N にある "Off" 以外の名前。無ければ ""）。"""
        apn = self._doc.xref_get_key(xref, "AP/N")
        if not apn or apn[0] != "dict":
            return ""
        keys = re.findall(r"/([^ /<>]+)\s+\d+\s+0\s+R", apn[1])
        states = [k for k in keys if k != "Off"]
        return states[0] if states else ""

    def select_radio(self, index: int, xref: int) -> bool:
        """ラジオボタン xref を選び、同じグループの他のボタンは外す。成功で True。

        値（/V）は欄（名前 /T のある辞書）に、見た目（/AS）は各ボタンに書く。
        以前は、ラジオに文字入力欄が重なり、何か打つと /V に文字が入った。
        """
        on = self._on_state(xref)
        if not on:
            return False
        field = jpfonts.field_xref(self._doc, xref)
        kids = [xref]
        kd = self._doc.xref_get_key(field, "Kids")
        if kd and kd[0] == "array":
            kids = [int(m) for m in re.findall(r"(\d+)\s+0\s+R", kd[1])] or [xref]
        for k in kids:
            self._doc.xref_set_key(k, "AS", "/" + on if k == xref else "/Off")
        self._doc.xref_set_key(field, "V", "/" + on)
        return True

    def _set_checkbox(self, w, on: bool) -> bool:
        """チェックボックスの ON/OFF を /AS・/V を直接書いて反映する。

        ON にするときは外観辞書(/AP/N)にある "Off" 以外の状態名を使う。
        /V は欄（名前 /T のある辞書。jpfonts.field_xref）に、/AS は見た目を持つ
        ウィジェットに書く。
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
        # /V は欄（名前 /T のある辞書）に書く。「親があれば親」と決め打ちすると、欄と
        # 一体のウィジェットの上にある /T 付きの途中ノードに書いてしまい、本人の値は
        # Off のまま、兄弟のチェックボックスが全部 ON と読める
        self._doc.xref_set_key(jpfonts.field_xref(self._doc, xref), "V", name)
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
            for xref, _name, wr, script in govform_js.page_widgets(self._doc, index):
                if not (script and govform_js.SIMPLE_CALL_RE.match(script)):
                    continue
                if not wr.intersects(target):
                    continue
                if govform_js.widget_display(self._doc, xref) in (1, 3):
                    continue  # 非表示（Hidden/印刷のみ）は対象外
                if (abs(wr.x0 - target.x0) < 1.0 and abs(wr.y0 - target.y0) < 1.0
                        and abs(wr.x1 - target.x1) < 1.0
                        and abs(wr.y1 - target.y1) < 1.0):
                    return xref                 # 矩形一致（本命）
                if fallback is None:
                    fallback = xref             # 念のための保険
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
            page = self._doc.load_page(index)   # Widget はページを弱参照で持つ
            w = fastwidgets.widget(page, xref)
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

    def commit_govform_combo(self, index: int, xref: int, display_text: str,
                             name: str | None = None) -> bool:
        """コンボ選択時の JS（_PRINT 転写・枝ラベル表示切替）を再現する。

        値/表示が実際に変化すれば True を返し modified を立てる。
        """
        if not self._doc:
            return False
        xref = self._widget_xref(index, xref, name)
        if xref is None:
            return False
        export = self.govform_combo_export_value(index, xref, display_text)
        effects = govform_js.FieldEffects()
        self.last_form_effects = effects
        scripts = self.doc_scripts()
        changed = govform_js.run_keystroke(
            self._doc, index, xref, scripts, export, effects=effects,
            marks=self.field_marks)
        # 選んだら次の欄へ移る＝フォーカスを外したときの処理（例: 口座情報が
        # あるのに空欄を選んだら「2」に戻す）もここで走らせる
        own = name or govform_js._field_name(self._doc, xref)
        if govform_js.run_blur(self._doc, index, xref, scripts, effects=effects,
                               checks=True, marks=self.field_marks):
            changed = True
            if own in effects.values:
                # 欄自身の値が戻されたら、印刷用の欄・有効/無効もその値に合わせる
                govform_js.run_keystroke(
                    self._doc, index, xref, scripts,
                    self.govform_combo_export_value(index, xref, effects.values[own]),
                    effects=effects, marks=self.field_marks)
        if changed:
            self.modified = True
        return changed

    def clear_form(self) -> int:
        """入力した内容をすべて消して、配られたときの状態に戻す。戻した欄の数。

        値は欄の既定値（/DV。無ければ空・チェックなし）に戻す。官公庁フォーム
        は、〇の選択や入力できる欄の切り替えの初期状態を JS の初期化関数が
        持っているので、その表示・書き換え禁止も当て直す。Ctrl+Z で戻せる。
        """
        if not self._doc:
            return 0
        self._push_doc_undo("フォームのクリア", None)
        self.field_marks.clear()
        init = govform_js.initial_field_states(self.doc_scripts())
        n = 0
        for page in self._doc:
            for w in fastwidgets.widgets(page):
                changed = self._reset_widget_value(w)
                state = init.get(w.field_name or "")
                if state:
                    if state[0] is not None and govform_js.apply_display_flags(
                            self._doc, w.xref, state[0]):
                        changed = True
                    if state[1] is not None and govform_js.apply_readonly(
                            self._doc, w.xref, state[1]):
                        changed = True
                    if govform_js.apply_required(self._doc, w.xref, state[2]):
                        changed = True   # 入力チェックの誤りの印も消す
                n += bool(changed)
        if n:
            self.modified = True
        return n

    def _reset_widget_value(self, w) -> bool:
        """欄の値を既定値に戻す。変わったら True。"""
        kind = w.field_type
        dv = self._doc.xref_get_key(w.xref, "DV")
        if dv[0] == "null":
            parent = self._doc.xref_get_key(w.xref, "Parent")
            if parent[0] == "xref":
                dv = self._doc.xref_get_key(int(parent[1].split()[0]), "DV")
        default = dv[1] if dv[0] in ("string", "name") else ""
        if dv[0] == "name":
            default = default.lstrip("/")
        try:
            if kind in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON):
                on = default not in ("", "Off")
                if (str(w.field_value or "") not in ("", "Off", "False", "false")) == on:
                    return False
                if kind == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                    return self._set_checkbox(w, on)
                w.field_value = on
                w.update()
                return True
            if kind in (fitz.PDF_WIDGET_TYPE_TEXT, fitz.PDF_WIDGET_TYPE_COMBOBOX,
                        fitz.PDF_WIDGET_TYPE_LISTBOX):
                default = govform_js.combo_display(w, default)   # 表示の文字で持つ
                if str(w.field_value or "") == default:
                    return False
                w.field_value = default
                jpfonts.update_widget(w)
                return True
        except Exception:  # noqa: BLE001  戻せない欄は飛ばす
            return False
        return False   # 押しボタン（値を持たない）

    def blur_govform_field(self, index: int, xref: int, name: str | None = None,
                           checks: bool = True) -> bool:
        """文字欄を抜けたときのフォームの処理（入力チェック・年の 2 桁化）。

        誤りの知らせと印は last_form_effects に入る。値が変わったら True。
        checks=False は欄にいるまま書き込むとき（保存など）で、年の 2 桁化だけ。
        """
        self.last_form_effects = None
        if not self._doc or not self.is_govform_js_eligible():
            return False
        xref = self._widget_xref(index, xref, name)
        if xref is None:
            return False
        effects = govform_js.FieldEffects()
        self.last_form_effects = effects
        scripts = self.doc_scripts()
        changed = checks and govform_js.run_blur(self._doc, index, xref, scripts,
                                                 effects=effects, checks=True,
                                                 marks=self.field_marks)
        # 年は「05」のように 2 桁にそろえる。フォームの JS は元号を選ぶ前だけ
        # 0 を足す作りで、選んだ後に「5」と入れると左のマスに「5」だけが刷られ
        # た（欄の注意書きは「数値2桁です」）。ここでは元号に関係なくそろえる
        if govform_js.pads_two_digits(scripts, govform_js.widget_action_js(
                self._doc, xref, "Bl")):
            page = self._doc.load_page(index)   # Widget はページを弱参照で持つ
            w = fastwidgets.widget(page, xref)
            value = str(w.field_value or "") if w is not None else ""
            if len(value) == 1:
                w.field_value = "0" + value
                jpfonts.update_widget(w)
                effects.values[w.field_name or name or ""] = "0" + value
                changed = True
        if changed:
            self.modified = True
        return changed

    def field_is_readonly(self, index: int, xref: int) -> bool:
        """欄が今、書き換え禁止か（フォームの JS が切り替えることがある）。"""
        if not self._doc:
            return False
        return govform_js.widget_readonly(self._doc, xref)

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
            "title": annot.info.get("title", ""),   # 作成者名
        }
        if desc["kind"] == "Text":
            desc["icon"] = annot.info.get("name") or "Note"   # 付箋のアイコン
        if desc["kind"] == "FreeText":
            # テキスト注釈の背景の塗りは /C に入る（annot.colors では "stroke" に出る）。
            # 無いものは None（既定の "stroke" は黒で埋めてあるので、区別できない）
            desc["ft_fill"] = colors.get("stroke") or None
            # 文字サイズ・フォント・文字色は /DA に入る（annot.colors の stroke は
            # 枠線色であって本文色ではない）。ここで拾わないと移動/リサイズ/Undo で
            # サイズも色も失われる。
            (desc["fontsize"], desc["text_color"],
             desc["fontname"]) = self._parse_freetext_da(annot)
            desc["soft"] = self._soft_text(annot)
            # スタンプの枠と中央寄せ。拾わないと動かしただけで枠が消えていた
            desc["ft_border"] = float(border.get("width") or 0)
            try:
                q = self._doc.xref_get_key(annot.xref, "Q")
                desc["align"] = int(q[1]) if q[0] == "int" else 0
            except Exception:  # noqa: BLE001
                desc["align"] = 0
        return desc

    # /DA のフォント資源名 → add_freetext_annot に渡す短縮名
    _DA_FONT_NAMES = {"helv": "helv", "tiro": "tiro", "cour": "cour",
                      "times": "tiro"}

    def _parse_freetext_da(self, annot):
        """FreeText の /DA 文字列からフォントサイズ・本文色・フォント名を取り出す。"""
        fs, tc, fn = 12.0, (0.0, 0.0, 0.0), "helv"
        try:
            da = self._doc.xref_get_key(annot.xref, "DA")
            s = da[1] if da and da[0] == "string" else ""
            m = re.search(r"/(\w+)\s+([\d.]+)\s+Tf", s)
            if m:
                fs = float(m.group(2))
                fn = self._DA_FONT_NAMES.get(m.group(1).lower(), "helv")
            else:
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
        return fs, tc, fn

    def _recreate(self, page, desc, dx=0.0, dy=0.0, color=None, width=None,
                  text=None, new_rect=None, dashes=_UNSET, opacity=_UNSET,
                  fill=_UNSET, line_ends=_UNSET, fontsize=None, fontname=None,
                  wrapped=None):
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
        fill_given = fill is not _UNSET
        fill_arg = fill
        fill = desc.get("fill") if fill is _UNSET else fill
        dashes = desc.get("dashes") if dashes is _UNSET else dashes
        opacity = desc.get("opacity") if opacity is _UNSET else opacity
        if new_rect is not None:
            target = fitz.Rect(new_rect)
        else:
            target = fitz.Rect(old.x0 + dx, old.y0 + dy, old.x1 + dx, old.y1 + dy)

        if kind == "Highlight":
            # 何行にもわたるハイライトは quad の列。外接矩形 1 枚で作り直すと、
            # 塗られていなかった語（行間・行末の余白）まで塗れてしまう
            quads = self._map_quads(desc.get("vertices"), old, target)
            annot = page.add_highlight_annot(quads or target)
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
            fs = fontsize if fontsize else (desc.get("fontsize", 12) or 12)
            fn = fontname if fontname else (desc.get("fontname") or "helv")
            tc = color if color is not None else desc.get("text_color", stroke)
            soft = text if text is not None else desc.get("soft", content)
            disp = self._to_display_rect(page, target)   # 画面で見える向きの枠
            if wrapped is None:
                if (text is not None or new_rect is not None
                        or fontsize or fontname):
                    # 本文・幅・文字の大きさが変わったら折り返し直す
                    wrapped = wrap_freetext(soft, disp.width, fs, fn)
                else:
                    wrapped = content   # 移動・色替えなどは見た目の行をそのまま
            if desc.get("ft_border", 0) <= 0:
                disp = self._fit_freetext_rect(disp, wrapped, fs, fn)
            ft_kwargs = {}
            ft_fill = fill_arg if fill_given else desc.get("ft_fill")
            if ft_fill:   # 背景の塗り。渡さないと、動かす・書式を変える・取り消すたびに消えた
                ft_kwargs["fill_color"] = tuple(ft_fill)
            annot = page.add_freetext_annot(
                self._from_display_rect(page, disp), wrapped, fontsize=fs,
                fontname=fn, text_color=tc,
                border_width=desc.get("ft_border", 0) or 0,
                align=desc.get("align", 0) or 0, rotate=page.rotation, **ft_kwargs)
            self._set_soft_text(annot.xref, soft, wrapped)
        elif kind == "Text":
            annot = page.add_text_annot(fitz.Point(target.x0, target.y0), content,
                                        icon=desc.get("icon") or "Note")
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
            ends = desc.get("line_ends") if line_ends is _UNSET else line_ends
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
        if desc.get("title"):
            try:
                annot.set_info(title=desc["title"])   # 作成者名を引き継ぐ
            except Exception:  # noqa: BLE001
                pass
        self._update_annot(annot)
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
        if annot.type[1] not in self._RECREATABLE:
            # 他のアプリで付けたスタンプ・波線・多角形などは、作り直せない。消してから
            # 気づくと、動かしただけで注釈が無くなる（取り消しでも戻らない）。何もしない
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
                      opacity=_UNSET, fill=_UNSET, line_ends=_UNSET,
                      fontsize=None, fontname=None):
        """選択済み注釈の太さ・線種(dashes)・不透明度・塗り・線端・文字サイズ/
        フォント（FreeText のみ）を後から変更する。

        未指定（_UNSET / None）の項目は元の値を引き継ぐ。fill=None は「塗り無し」。
        line_ends は (始点, 終点) の PDF 線端コード(int)のタプル（直線/矢印のみ有効）。
        変更後の新しい xref を返す（作り直しのため xref は変わる）。
        """
        return self._edit_annot(index, xref, width=width, dashes=dashes,
                                opacity=opacity, fill=fill, line_ends=line_ends,
                                fontsize=fontsize, fontname=fontname)

    def set_annot_text(self, index, xref, text, *, wrapped=None, rect=None,
                       color=None, fontsize=None, fontname=None):
        """注釈の本文を書き換える。新しい xref を返す。

        テキスト注釈では、画面の入力欄の折り返し（wrapped）と枠（rect）・
        文字色・大きさ・フォントもまとめて受け取れる（入力欄で見えていた
        とおりに確定させるため）。省略した分は元の注釈から引き継ぐ。
        """
        return self._edit_annot(index, xref, text=text, wrapped=wrapped,
                                new_rect=rect, color=color, fontsize=fontsize,
                                fontname=fontname)

    def annot_textstyle(self, index, xref):
        """注釈の種別と文字スタイルを返す（UI 同期用）。無ければ None。

        {"kind": 種別, "fontsize": float, "fontname": str, "text_color": rgb}
        fontsize 等は FreeText 以外では既定値のまま。
        """
        page = self._doc.load_page(index)
        annot = self._find_on(page, xref)
        if annot is None:
            return None
        kind = annot.type[1]
        fs, tc, fn = 12.0, (0.0, 0.0, 0.0), "helv"
        if kind == "FreeText":
            fs, tc, fn = self._parse_freetext_da(annot)
        return {"kind": kind, "fontsize": fs, "fontname": fn, "text_color": tc}

    # 注釈種別 → 一覧に出す日本語ラベル
    _ANNOT_LABELS = {
        "Highlight": "ハイライト", "Square": "四角", "Circle": "円",
        "Line": "直線/矢印", "Ink": "ペン", "FreeText": "テキスト",
        "Text": "付箋", "Underline": "下線", "StrikeOut": "取消線",
        "Polygon": "多角形", "PolyLine": "折れ線", "Redact": "墨消し",
    }

    def list_annots(self) -> list:
        """全ページの注釈を一覧用の dict リストで返す。

        各要素: {page, xref, kind, label, content, color(rgb 0-1 tuple|None)}。
        content はテキスト系は本文、それ以外は種別ラベル。
        """
        out = []
        if not self._doc:
            return out
        for pno in range(self._doc.page_count):
            try:
                page = self._doc.load_page(pno)
                for a in page.annots():
                    kind = a.type[1]
                    colors = a.colors or {}
                    col = colors.get("stroke") or colors.get("fill")
                    content = self._soft_text(a)
                    label = self._ANNOT_LABELS.get(kind, kind)
                    text = content.strip().replace("\n", " ") if content.strip() else label
                    out.append({
                        "page": pno, "xref": a.xref, "kind": kind,
                        "label": label, "content": text[:40],
                        "color": tuple(col) if col else None,
                    })
            except Exception:  # noqa: BLE001
                continue
        return out

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
        """注釈を変更する直前に、そのページの現状を履歴へ積む。"""
        if not self._doc:
            return
        try:
            snap = self._annot_snapshot(index)
        except Exception:  # noqa: BLE001
            return
        self._append_history(("annot", index, snap, "注釈"))

    # _recreate で作り直せる注釈の種類。これ以外（Acrobat などで付けたスタンプ・波線・
    # 多角形・墨消しの印など）は、アプリが作らず、編集もしないので、取り消しでも触らない
    _RECREATABLE = frozenset({"Highlight", "Square", "Circle", "FreeText", "Text",
                              "Ink", "Line", "Underline", "StrikeOut"})

    @staticmethod
    def _desc_key(desc: dict) -> tuple:
        """注釈の記述を、同じ注釈かどうかの比較用に丸めた形にする（xref は見ない）。"""
        def norm(v):
            if isinstance(v, (fitz.Rect, fitz.Point)):
                return tuple(round(float(x), 1) for x in v)
            if isinstance(v, float):
                return round(v, 2)
            if isinstance(v, (list, tuple)):
                return tuple(norm(x) for x in v)
            return v
        return tuple(sorted((k, norm(v)) for k, v in desc.items()))

    def _restore_snapshot(self, index: int, snap: list) -> None:
        """そのページの注釈を snap の状態に戻す。

        ページの注釈を全部消して作り直すと、作り直せない種類（他のアプリで付けた
        スタンプ・波線・多角形）や、作り直しで失われる属性（作成者・ハイライトの
        複数行）まで、関係のない注釈を壊す。そのため、snap と同じ注釈には触らず、
        違うもの（後から足した／編集した）だけを消して、無くなったものだけを作り直す。
        """
        page = self._doc.load_page(index)
        want: dict[tuple, list] = {}
        for desc in snap:
            want.setdefault(self._desc_key(desc), []).append(desc)
        for annot in list(page.annots()):
            if annot.type[1] not in self._RECREATABLE:
                continue   # アプリが編集しない種類は、いつでもそのまま
            bucket = want.get(self._desc_key(self._describe(annot)))
            if bucket:
                bucket.pop()          # 戻したい状態にも同じ注釈がある: 触らない
            else:
                page.delete_annot(annot)   # 戻したい状態には無い
        for bucket in want.values():
            for desc in bucket:
                if desc["kind"] in self._RECREATABLE:
                    self._recreate(page, desc)
        self._modified = True   # setter だと履歴を巻き込むので直接立てる
        self._invalidate_view_caches()

    def _push_doc_undo(self, label: str, index: int | None = None) -> None:
        """ページ構成を変える直前に、文書まるごとを履歴へ積む。

        削除・並べ替え・回転などは注釈のような部分スナップショットでは戻せない
        ので、その時点の PDF をバイト列で控える。大きな文書では効かないことが
        あるが（下の上限）、黙って戻せない方がマシなので履歴自体を空にする。
        undo_group で囲まれている間は、最初の 1 回だけ積む。
        """
        if not self._doc:
            return
        # 画面側の入力中の文字を、構成が変わる前に書き込む（ページ操作のボタンは
        # フォーカスを取らないので、何もしないと打ちかけの文字が確認なしで消える）
        self._notify_pre_change()
        if self._undo_group > 0 and self._group_pushed:
            return  # 同じ操作の 2 回目以降（1 回分にまとめる）
        try:
            data = self._doc.tobytes()
        except Exception:  # noqa: BLE001
            return
        if len(data) > self._UNDO_DOC_BYTES:
            # 1 手も入らない大きさ。中途半端に古い履歴だけ残さない。
            self._clear_history()
            return
        if self._undo_group > 0:
            self._group_pushed = True
        self._append_history(("doc", index, data, label))

    @contextmanager
    def undo_group(self, label: str):
        """複数ページにまたがる 1 操作を、Undo 1 回分にまとめる。"""
        self._undo_group += 1
        if self._undo_group == 1:
            self._group_pushed = False
        try:
            yield
        finally:
            self._undo_group -= 1

    def _append_history(self, entry: tuple) -> None:
        self._undo.append(entry)
        if entry[0] == "doc":
            self._undo_bytes += len(entry[2])
        self._redo.clear()      # 新しい変更が入ったら Redo は無効化
        self._trim_history()

    def _trim_history(self) -> None:
        """古いものから捨てて、件数とメモリを上限内に収める。"""
        while len(self._undo) > self._UNDO_MAX:
            self._drop_oldest()
        docs = [e for e in self._undo if e[0] == "doc"]
        while (len(docs) > self._UNDO_DOC_MAX
               or self._undo_bytes > self._UNDO_DOC_BYTES):
            if not self._drop_oldest():
                break
            docs = [e for e in self._undo if e[0] == "doc"]

    def _drop_oldest(self) -> bool:
        if not self._undo:
            return False
        kind, _idx, payload, _label = self._undo.pop(0)
        if kind == "doc":
            self._undo_bytes -= len(payload)
        return True

    def _clear_history(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._undo_bytes = 0

    def _restore_doc_bytes(self, data: bytes) -> None:
        self._doc.close()
        self._doc = fitz.open(stream=data, filetype="pdf")
        self._invalidate_view_caches()
        self._modified = True   # setter を通すと履歴まで巻き込むので直接立てる

    #: 入力値・しおり・文書情報を変えない（変えても、取り消しで戻すべきでない）ページ操作。
    #: これらを取り消しても、そのあとに入れた入力値・しおり・文書情報は残す。「フォームの
    #: クリア」と「墨消しの適用」は値そのものを変える操作なので、取り消せば値が戻るのが正しい
    _VALUE_KEEPING_OPS = frozenset({
        "ページの回転", "透かしの追加", "ヘッダー/フッターの追加", "白紙ページの挿入",
        "ページの複製", "余白のトリミング", "ページの移動", "ページの並べ替え",
        "ページの削除", "PDF の結合", "画像の取り込み", "傾き補正", "ページ番号の追加"})

    @staticmethod
    def _field_values(doc) -> dict:
        """文書の入力欄の値 {欄の名前: (種類, 値)}。ボタン・署名は含めない。"""
        out: dict = {}
        skip = (fitz.PDF_WIDGET_TYPE_BUTTON, fitz.PDF_WIDGET_TYPE_SIGNATURE)
        for page in doc:
            for w in fastwidgets.widgets(page):
                if w.field_name and w.field_type not in skip:
                    out[w.field_name] = (w.field_type, str(w.field_value or ""))
        return out

    def _later_changes(self, base_bytes: bytes) -> dict:
        """取り消して base_bytes の状態に戻すとき、そのあとに入れた変更（履歴に入らないもの）。

        ページ操作の履歴は文書全体のバイト列。入力欄の値・しおり・文書情報の変更は履歴に
        入らないので、そのまま戻すと、ページを回したあとに入力した欄の値やしおりが、確認なしに
        消えていた（さらに何か編集すると、やり直しも消える）。
        """
        base = fitz.open("pdf", base_bytes)
        try:
            base_vals = self._field_values(base)
            base_meta = dict(base.metadata or {})
            base_toc = [(t[0], t[1]) for t in base.get_toc(simple=True)]
            base_pages = base.page_count
        finally:
            base.close()
        cur_vals = self._field_values(self._doc)
        fields = {n: v for n, v in cur_vals.items()
                  if n in base_vals and base_vals[n] != v}
        meta = dict(self._doc.metadata or {})
        toc_full = self._doc.get_toc(simple=False)
        toc_changed = [(t[0], t[1]) for t in toc_full] != base_toc
        out = {"fields": fields, "meta": meta if meta != base_meta else None,
               "toc": None, "toc_lost": False}
        if toc_changed:
            if self._doc.page_count == base_pages:
                out["toc"] = toc_full     # ページ数が同じなら、ページ番号もそのまま使える
            else:
                out["toc_lost"] = True    # ページの数が違うと、番号を対応づけられない
        return out

    def _reapply_changes(self, carried: dict) -> None:
        """_later_changes で控えた変更を、戻した文書へ書き直す。"""
        fields = carried.get("fields") or {}
        if fields:
            done = set()
            for page in self._doc:
                for w in fastwidgets.widgets(page):
                    want = fields.get(w.field_name)
                    if want is None or w.field_name in done:
                        continue
                    typ, val = want
                    try:
                        if typ == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                            self._set_checkbox(w, val not in ("", "Off"))
                        elif typ in (fitz.PDF_WIDGET_TYPE_TEXT, fitz.PDF_WIDGET_TYPE_COMBOBOX,
                                     fitz.PDF_WIDGET_TYPE_LISTBOX):
                            w.field_value = val
                            jpfonts.update_widget(w)
                        else:
                            continue
                        done.add(w.field_name)
                    except Exception:  # noqa: BLE001  1 欄の失敗で取り消し全体を止めない
                        continue
        if carried.get("meta") is not None:
            try:
                self._doc.set_metadata(carried["meta"])
            except Exception:  # noqa: BLE001
                pass
        if carried.get("toc") is not None:
            try:
                self._doc.set_toc(carried["toc"])
            except Exception:  # noqa: BLE001
                pass

    def _apply_entry(self, entry: tuple, opposite: list) -> tuple:
        """entry の状態へ戻し、戻す前の状態を opposite に積む。"""
        kind, index, payload, label = entry
        self.undo_notice = ""
        if kind == "doc":
            current = self._doc.tobytes()
            carried = None
            if label in self._VALUE_KEEPING_OPS:
                try:
                    carried = self._later_changes(payload)
                except Exception:  # noqa: BLE001  控えられなくても、取り消し自体はする
                    carried = None
            self._restore_doc_bytes(payload)
            if carried:
                self._reapply_changes(carried)
                if carried.get("toc_lost"):
                    self.undo_notice = ("ページの数が変わる操作を戻したので、そのあとに追加・変更した"
                                        "しおりは戻りませんでした")
            opposite.append(("doc", index, current, label))
        else:
            current = self._annot_snapshot(index)
            self._restore_snapshot(index, payload)
            opposite.append(("annot", index, current, label))
        return (kind, index, label)

    def undo(self):
        """直前の操作を取り消す。(kind, page_index, label) を返す（無ければ None）。"""
        if not self._undo or not self._doc:
            return None
        entry = self._undo.pop()
        if entry[0] == "doc":
            self._undo_bytes -= len(entry[2])
        try:
            return self._apply_entry(entry, self._redo)
        except Exception:  # noqa: BLE001  戻せないものは履歴ごと諦める
            self._clear_history()
            return None

    def redo(self):
        """取り消した操作をやり直す。(kind, page_index, label) を返す。"""
        if not self._redo or not self._doc:
            return None
        entry = self._redo.pop()
        try:
            result = self._apply_entry(entry, self._undo)
        except Exception:  # noqa: BLE001
            self._clear_history()
            return None
        if entry[0] == "doc":
            self._undo_bytes += len(self._undo[-1][2])
        return result

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo_label(self) -> str:
        return self._undo[-1][3] if self._undo else ""

    def redo_label(self) -> str:
        return self._redo[-1][3] if self._redo else ""

    # 旧名（注釈だけだった頃の呼び名）。呼び出し側が残っているので残す。
    def undo_annot(self):
        result = self.undo()
        return None if result is None else result[1]

    def redo_annot(self):
        result = self.redo()
        return None if result is None else result[1]

    def can_undo_annot(self) -> bool:
        return self.can_undo()

    def can_redo_annot(self) -> bool:
        return self.can_redo()

    # --- ページ編集（並べ替え・削除・統合） ---------------------------
    def move_page(self, src: int, dst: int) -> None:
        """src ページを dst の位置へ移動（並べ替え）。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        if src == dst:
            return
        self._push_doc_undo("ページの移動", dst)
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
        self._push_doc_undo("ページの並べ替え", None)
        self._doc.select(new_order)
        self.modified = True

    def delete_page(self, index: int) -> None:
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        if self.page_count <= 1:
            raise ValueError("最後の1ページは削除できません")
        self._push_doc_undo("ページの削除", index)
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
        self._push_doc_undo("ページの削除", None)   # 検証のあと（失敗で空の履歴を残さない）
        self._doc.select(keep)
        self.modified = True

    def insert_pdf(self, other_path: str, at: int | None = None) -> int:
        """別の PDF を取り込む。at=None なら末尾に追加。挿入したページ数を返す。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        with fitz.open(other_path) as other:
            # 挿入する前に確かめる。パスワード付きの PDF は、ページが 1 枚増えたあとで
            # 英語の例外（document closed or encrypted）になり、空のページと無意味な
            # 取り消しが残っていた
            if other.needs_pass:
                raise ValueError(
                    "パスワード付きの PDF は結合・挿入できません。先にパスワードを外して"
                    "（保護 → 解除）から、もう一度お試しください。")
            n = other.page_count
            if n == 0:
                raise ValueError("挿入する PDF にページがありません")
            start_at = self.page_count if at is None else at
            self._push_doc_undo("PDF の結合", None)
            self._doc.insert_pdf(other, start_at=start_at)
        self.modified = True
        return n

    def insert_images_as_pages(
        self, image_paths: list[str], at: int | None = None
    ) -> int:
        """画像ファイルをページとして取り込む。挿入したページ数を返す。"""
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        # 全部の画像を先に PDF にしておく。途中の 1 枚が壊れていたとき、先の分だけ入って
        # 画面に反映されず、取り消しにも無意味な項目が残っていた
        converted = []
        for p in image_paths:
            try:
                with fitz.open(p) as img_doc:
                    converted.append(img_doc.convert_to_pdf())
            except Exception as exc:  # noqa: BLE001
                raise ValueError(
                    f"画像「{os.path.basename(p)}」を読み込めません: {friendly_error(exc)}") from exc
        self._push_doc_undo("画像の取り込み", None)
        start = self.page_count if at is None else at
        added = 0
        for pdf_bytes in converted:
            with fitz.open("pdf", pdf_bytes) as img_pdf:
                self._doc.insert_pdf(img_pdf, start_at=start + added)
                added += img_pdf.page_count
        self.modified = True
        return added

    # --- 画像変換 -------------------------------------------------------
    @staticmethod
    def image_output_name(out_dir: str, stem: str, index: int, fmt: str) -> str:
        """ページ画像の書き出し先（0 始まりの index）。確認と書き出しで同じ名前を使う。"""
        ext = fmt.lower().lstrip(".")
        ext = "jpg" if ext in ("jpg", "jpeg") else ext
        return os.path.join(out_dir, f"{stem}_{index + 1:03d}.{ext}")

    def split_output_names(self, n: int, out_dir: str, stem: str) -> list[str]:
        """split_every が書き出すファイル名の一覧。"""
        total = self.page_count
        return [os.path.join(out_dir, f"{stem}_part{part:02d}.pdf")
                for part in range(1, (total + max(1, n) - 1) // max(1, n) + 1)]

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
            out_path = self.image_output_name(out_dir, stem, i, ext)
            pix.save(out_path)
            outputs.append(out_path)
        return outputs

    def deskew_all(self, dpi: int = 200, limit: float = 8.0,
                   skip_text_pages: bool = True, progress=None) -> int:
        """スキャンページの傾きを補正する。補正したページ数を返す。

        - テキストを持つページ（born-digital）は触らず原本を保持
        - 傾きが小さいスキャンページも触らず原本を保持（画像の作り直しで劣化・肥大させない）
        - 傾いているスキャンページだけ、補正した画像を、元のページと同じ大きさのページに
          貼って作り直す
        しおり・文書情報は引き継ぐ。progress(done, total) が False を返したら中断（変更は破棄）。
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
                text_page = (skip_text_pages and page.get_text().strip()
                             and not self._is_scanned(page))
                fixed = None
                if not text_page:
                    pix = page.get_pixmap(dpi=dpi, alpha=False)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    angle = deskew_mod.estimate_skew(img, limit=limit)
                    if abs(angle) >= 0.2:
                        img = img.rotate(angle, resample=Image.Resampling.BICUBIC,
                                         fillcolor=(255, 255, 255), expand=False)
                        fixed = img
                if fixed is None:
                    new.insert_pdf(self._doc, from_page=i, to_page=i)   # 原本のまま
                else:
                    # 小さいほうを使う（文字のスキャンは PNG、写真は JPEG のほうが小さい）
                    png, jpg = io.BytesIO(), io.BytesIO()
                    fixed.save(png, format="png", optimize=True)
                    fixed.save(jpg, format="jpeg", quality=88)
                    data = png.getvalue() if png.tell() <= jpg.tell() else jpg.getvalue()
                    # 元のページと同じ大きさ（画面に見える向きの大きさ）に貼る。画像だけの
                    # ページを作ると解像度の指定が無く、96dpi 扱いで約 2 倍の大きさになった
                    rect = page.rect
                    out = new.new_page(width=rect.width, height=rect.height)
                    out.insert_image(out.rect, stream=data)
                    corrected += 1
                if progress and not progress(i + 1, total):
                    new.close()
                    return -1  # 中断
            self._carry_over_outline(new)
        except Exception:
            new.close()
            raise
        # 元ドキュメントを差し替える
        self._push_doc_undo("傾き補正")
        self._doc.close()
        self._doc = new
        self._invalidate_view_caches()  # 別の中身に差し替えた
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
    def _is_scanned(self, page) -> bool:
        """ページ全体を 1 枚の画像が覆っている（スキャン）か。

        ページ番号やスタンプなど、あとから少し文字を足したスキャンページは、文字が
        あるというだけで「文字のあるページ」とされ、OCR も傾き補正もされなかった。
        ロゴなど一部だけの画像は、スキャンとは見なさない（元の文書のまま残す）。
        """
        area = page.rect.width * page.rect.height
        if area <= 0:
            return False
        try:
            for info in page.get_image_info():
                r = fitz.Rect(info["bbox"]) & page.rect
                if r.width * r.height >= 0.8 * area:
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False

    def _carry_over_outline(self, new) -> None:
        """作り直した文書 new に、しおり・文書情報（題名など）を移す。

        ページ数が同じ作り直し（OCR・傾き補正）では、ページ番号のまま移せる。
        """
        try:
            new.set_metadata(self._doc.metadata or {})
        except Exception:  # noqa: BLE001
            pass
        try:
            toc = self._doc.get_toc(simple=False)
            if toc and new.page_count == self._doc.page_count:
                new.set_toc(toc)
        except Exception:  # noqa: BLE001
            try:
                new.set_toc(self._doc.get_toc(simple=True))
            except Exception:  # noqa: BLE001
                pass

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
                if skip_text_pages and page.get_text().strip() and not self._is_scanned(page):
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
            self._carry_over_outline(new)   # 新しい文書に作り直すので、しおり・文書情報を移す
            self._replace_atomically(
                out_path, lambda t: new.save(t, garbage=4, deflate=True))
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
            self._replace_atomically(out_path, lambda t: new.save(
                t, garbage=4, deflate=True, **self._protection_kwargs()))
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
                self._replace_atomically(out_path, lambda t: new.save(
                    t, garbage=4, deflate=True, **self._protection_kwargs()))
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
        self._japanize_before_saving()
        try:
            if self.path and _same_path(self.path, out_path):
                self._save_over_self(out_path, garbage=4, deflate=True)
            else:
                self._save_to(out_path, garbage=4, deflate=True)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(friendly_error(exc, saving=True)) from exc
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
        self._japanize_before_saving()
        self._save_to(
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
        self._save_to(
            out_path, encryption=fitz.PDF_ENCRYPT_NONE, garbage=3, deflate=True
        )

    # --- 固定化（注釈・入力値を本文に焼き込む）---------------------------
    def flatten_to(self, out_path: str, annots: bool = True,
                   widgets: bool = True) -> None:
        """注釈やフォームの入力値をページの絵の一部にして別名保存する。

        相手に渡したあとで注釈を動かされたり入力欄を書き換えられたりしない
        ようにする用途（届出書の控え・確定版）。開いている文書はそのまま
        （編集可能なまま）で、複製に対して行う。
        """
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        if self.path and _same_path(self.path, out_path):
            raise ValueError("開いているファイルとは別名で保存してください")
        self._japanize_before_saving()
        work = fitz.open("pdf", self._doc.tobytes())
        try:
            work.bake(annots=annots, widgets=widgets)
            self._replace_atomically(out_path, lambda t: work.save(
                t, garbage=4, deflate=True, **self._protection_kwargs()))
        finally:
            work.close()

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
        return self._page_layout(index)[0]

    def _page_layout(self, index):
        """(chars, lines) を返す。

        chars は読み取り順の [(rect, 文字, 行ID), ...]、lines は行ごとの
        [(行ID, [chars の添字...], 行のbbox), ...]。ドラッグ中に何度も使うので
        直近1ページ分を憶えておき、選択を始めるときに捨てる
        （invalidate_text_layout）。
        """
        cached = self._layout_cache.get(index)
        if cached is not None:
            return cached
        chars = []
        if self._doc:
            page = self._doc.load_page(index)
            raw = page.get_text("rawdict", flags=_TEXT_FLAGS)
            line_id = 0
            for block in raw.get("blocks", []):
                if block.get("type", 0) != 0:  # 0 = テキストブロックのみ
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        for ch in span.get("chars", []):
                            chars.append((fitz.Rect(ch["bbox"]), ch["c"], line_id))
                    line_id += 1
        lines = []
        for i, (r, _c, lid) in enumerate(chars):
            if lines and lines[-1][0] == lid:
                lines[-1][1].append(i)
                lines[-1][2] |= r
            else:
                lines.append([lid, [i], fitz.Rect(r)])
        if len(self._layout_cache) >= self._LAYOUT_CACHE_MAX:
            self._layout_cache.clear()   # 溜まりすぎたらまとめて捨てる
        self._layout_cache[index] = (chars, lines)
        return self._layout_cache[index]

    def text_at(self, index, point) -> bool:
        """point（PDF 座標）がページの文字の行の上か。"""
        _chars, lines = self._page_layout(index)
        return any(fitz.Rect(bbox).contains(point) for _lid, _ix, bbox in lines)

    def invalidate_text_layout(self) -> None:
        """憶えている文字レイアウトを捨てる（選択を始めるときに呼ぶ）。"""
        self._layout_cache.clear()
        self._whole_cache.clear()

    @staticmethod
    def _caret_at(chars, lines, pt) -> int:
        """クリック位置に対応する「文字と文字の間」の位置を返す。

        先に行を決めてから行内を探す。全文字から最寄りを選ぶと、行末より右を
        クリックしたときに隣の行の文字を拾ってしまう。
        """
        if not lines:
            return 0
        # 1) 行を選ぶ。高さが重なる行を優先し、無ければ縦横の距離が近い行。
        covering = [ln for ln in lines if ln[2].y0 <= pt.y <= ln[2].y1]
        if covering:  # 段組みで同じ高さに複数行あるときは横位置で選ぶ
            line = min(covering, key=lambda ln: _span_dist(ln[2].x0, ln[2].x1, pt.x))
        else:
            line = min(lines, key=lambda ln: (_span_dist(ln[2].y0, ln[2].y1, pt.y),
                                              _span_dist(ln[2].x0, ln[2].x1, pt.x)))
        _lid, idxs, box = line
        # 2) 行の中で位置を決める。縦書きの行は上下で見る。
        vertical = len(idxs) > 1 and box.height > box.width * 1.5
        pos = pt.y if vertical else pt.x
        lo, hi = (box.y0, box.y1) if vertical else (box.x0, box.x1)
        if pos <= lo:
            return idxs[0]          # 行頭より手前 → 行の先頭
        if pos >= hi:
            return idxs[-1] + 1     # 行末より先 → 行の末尾
        for i in idxs:
            r = chars[i][0]
            mid = (r.y0 + r.y1) / 2 if vertical else (r.x0 + r.x1) / 2
            if pos <= mid:
                return i            # 文字の手前寄り
        return idxs[-1] + 1

    @staticmethod
    def _line_of(lines, char_index):
        """文字の添字からその行を返す。"""
        for ln in lines:
            if ln[1][0] <= char_index <= ln[1][-1]:
                return ln
        return lines[-1] if lines else None

    @staticmethod
    def _selection_from(chars, c0: int, c1: int):
        """chars[c0:c1] を (文字列, 行ごとの矩形) にまとめる。"""
        sel = chars[max(0, c0):max(0, c1)]
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

    def _select_whole_page(self, index):
        """そのページの文字を全部選ぶ。(文字列, 行ごとの矩形)。

        ページ全体なら 1 文字ずつの位置は要らないので、行単位で読む（文字ごとの
        解析より 4 倍ほど速い）。結果は憶えておく。ページをまたぐドラッグは
        マウスを動かすたびに途中のページを全部選び直すので、憶えていないと
        文字レイアウトの上限（数ページ）を超える長さのドラッグが毎回全ページを
        読み直していた。
        """
        cached = self._whole_cache.get(index)
        if cached is not None:
            return cached
        layout = self._layout_cache.get(index)
        if layout is not None:
            result = self._selection_from(layout[0], 0, len(layout[0]))
        else:
            result = self._whole_page_by_lines(index)
        if len(self._whole_cache) >= self._WHOLE_CACHE_MAX:
            self._whole_cache.clear()
        self._whole_cache[index] = result
        return result

    def _whole_page_by_lines(self, index):
        """_selection_from(全文字) と同じ結果を、行単位の読み取りで作る。"""
        if not self._doc:
            return ("", [])
        page = self._doc.load_page(index)
        raw = page.get_text("dict", flags=_TEXT_FLAGS)
        texts, rects = [], []
        for block in raw.get("blocks", []):
            if block.get("type", 0) != 0:  # 0 = テキストブロックのみ
                continue
            for line in block.get("lines", []):
                text = "".join(span.get("text", "") for span in line.get("spans", []))
                if text:
                    texts.append(text)
                    rects.append(fitz.Rect(line["bbox"]))
        return ("\n".join(texts), rects)

    def _select_from_point(self, index, pt):
        """クリック位置からページの終わりまで。"""
        chars, lines = self._page_layout(index)
        return self._selection_from(chars, self._caret_at(chars, lines, pt),
                                    len(chars))

    def _select_to_point(self, index, pt):
        """ページの先頭からクリック位置まで。"""
        chars, lines = self._page_layout(index)
        return self._selection_from(chars, 0, self._caret_at(chars, lines, pt))

    def select_across(self, i0: int, p0, i1: int, p1):
        """ページをまたいだ選択。(文字列, {ページ番号: 矩形リスト}) を返す。

        始点より終点が前なら入れ替える。途中のページは丸ごと選ぶ。
        """
        if not self._doc:
            return ("", {})
        if i1 < i0:
            i0, p0, i1, p1 = i1, p1, i0, p0
        if i0 == i1:
            text, rects = self.select_text(i0, p0, p1)
            return (text, {i0: rects} if rects else {})

        parts, out = [], {}
        for i in range(i0, min(i1, self.page_count - 1) + 1):
            if i == i0:
                text, rects = self._select_from_point(i, p0)
            elif i == i1:
                text, rects = self._select_to_point(i, p1)
            else:
                text, rects = self._select_whole_page(i)
            if rects:
                out[i] = rects
                parts.append(text)
        return ("\n".join(parts), out)

    def select_all_text(self):
        """文書全体のテキストを選ぶ。(文字列, {ページ番号: 矩形リスト})。"""
        if not self._doc:
            return ("", {})
        parts, out = [], {}
        for i in range(self.page_count):
            text, rects = self._select_whole_page(i)
            if rects:
                out[i] = rects
                parts.append(text)
        return ("\n".join(parts), out)

    def markup_rects(self, index: int, rects, kind: str, color,
                     opacity: float = 1.0) -> bool:
        """すでに決まっている矩形に、ハイライト/下線/取消線を付ける。

        右クリックメニューのように「選択済みの範囲」へ注釈を付ける経路と、
        ハイライトツールのドラッグ（文字単位で選んだ範囲）で使う。
        """
        if not self._doc or not rects:
            return False
        self._push_undo(index)
        page = self._doc.load_page(index)
        quads = [fitz.Rect(r).quad for r in rects]
        if kind == "underline":
            annot = page.add_underline_annot(quads)
        elif kind == "strikeout":
            annot = page.add_strikeout_annot(quads)
        else:
            annot = page.add_highlight_annot(quads)
        annot.set_colors(stroke=color)
        if opacity < 1.0:
            annot.set_opacity(opacity)
        self._finish_annot(annot)
        self.modified = True
        return True

    def select_word(self, index, pt):
        """クリック位置の語を選ぶ（ダブルクリック用）。

        日本語には語の区切りが無いので、文字種（英数字/ひらがな/カタカナ/漢字）
        が続くまとまりを 1 語として扱う。行はまたがない。
        """
        chars, lines = self._page_layout(index)
        if not chars:
            return ("", [])
        i = min(self._caret_at(chars, lines, pt), len(chars) - 1)
        line = self._line_of(lines, i)
        if line is None:
            return ("", [])
        first, last = line[1][0], line[1][-1]
        cls = _char_class(chars[i][1])
        start = end = i
        while start > first and _char_class(chars[start - 1][1]) == cls:
            start -= 1
        while end < last and _char_class(chars[end + 1][1]) == cls:
            end += 1
        return self._selection_from(chars, start, end + 1)

    def select_line(self, index, pt):
        """クリック位置の行を丸ごと選ぶ（トリプルクリック用）。"""
        chars, lines = self._page_layout(index)
        if not chars:
            return ("", [])
        i = min(self._caret_at(chars, lines, pt), len(chars) - 1)
        line = self._line_of(lines, i)
        if line is None:
            return ("", [])
        return self._selection_from(chars, line[1][0], line[1][-1] + 1)

    def select_text(self, index, p0, p1):
        """p0→p1（PDF座標）の間を「文字単位」で選択する。

        返り値 (text, rects)。text は選択文字列（行が変わると改行）、
        rects は行ごとにまとめた矩形（PDF座標、描画用）。テキストが無ければ
        ("", [])。どの行かを先に決めてから行内の文字境界を探すので、行末より
        右や段組みの余白をクリックしても隣の行へ飛ばない。
        """
        if not self._doc:
            return ("", [])
        chars, lines = self._page_layout(index)
        if not chars:
            return ("", [])
        c0 = self._caret_at(chars, lines, p0)
        c1 = self._caret_at(chars, lines, p1)
        if c0 > c1:
            c0, c1 = c1, c0
        return self._selection_from(chars, c0, c1)

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
        try:
            fmt.format(n=start, total=1)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                f"書式に使えない項目、またはかっこがあります（{exc}）。"
                "使えるのは {n} {total} です") from exc
        self._push_doc_undo("ページ番号の追加", None)
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
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        # 既存のしおりは、飛び先の位置・倍率・色・太字などを持つ辞書（4 つ目）ごと取り直す。
        # simple=True だと、ページ番号しか残らず、1 件足しただけで既存のしおり全部が
        # ページの左上へ飛ぶ・書式なしになった
        toc = self._doc.get_toc(simple=False)
        toc.append([max(1, level), title, page1])
        self._doc.set_toc(toc)
        self.modified = True

    # --- 最適化（無劣化圧縮） -----------------------------------------
    def compress_to(self, out_path: str) -> None:
        """不要オブジェクト整理＋ストリーム/フォント/画像の deflate で軽量化保存。

        既存の JPEG 等は再圧縮しない（無劣化）。構造の冗長を除いてサイズを縮める。
        """
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        # use_objstms: オブジェクトを圧縮ストリームにまとめる（PDF 1.5 以降の
        # 標準機能）。これを付けないと、元がオブジェクトストリーム形式だった
        # PDF は書き直しでかえって太る。
        kwargs = dict(garbage=4, deflate=True, deflate_images=True,
                      deflate_fonts=True, clean=True, use_objstms=1)
        try:
            if self.path and _same_path(self.path, out_path):
                self._save_over_self(out_path, **kwargs)
                self.path = out_path
                self.modified = False
            else:
                self._save_to(out_path, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(friendly_error(exc, saving=True)) from exc

    # 印刷する中身（印刷ダイアログの選択肢）
    PRINT_ALL = "all"        # 文書と注釈
    PRINT_FILLED = "filled"  # 文書と、書き込んだ文字・スタンプ（ハイライトやペン等は除く）
    PRINT_DOC = "doc"        # 文書のみ（入力欄に入れた値は残す）
    PRINT_FIELDS = "fields"  # 入れた値と書き込んだ文字だけ（印刷済みの用紙に刷る）

    _F_HIDDEN, _F_PRINT, _F_NOVIEW = 2, 4, 32
    # 書き込み（記入）として扱う注釈。ハイライト・ペン・付箋などの「赤入れ」とは分ける
    _FILLED_TYPES = (fitz.PDF_ANNOT_FREE_TEXT, fitz.PDF_ANNOT_STAMP, fitz.PDF_ANNOT_WIDGET)

    def print_copy(self, content: str = "all") -> "PdfDocument | None":
        """印刷用の複製を返す。手を入れずにそのまま刷れるなら None。

        - PDF の印刷の指定どおりにする。「印刷しない」欄・注釈（画面用の
          ボタンや、官公庁フォームで画面だけに出すコンボ）は隠し、「印刷だけ
          する」もの（_PRINT 欄など。画面には出ない）は出す。以前は画面の
          見た目のまま刷っていたため、月日が 2 マスの 1 マス目に詰まった。
        - content で刷る中身を絞る（PRINT_*）。
        使い終わったら close() すること。
        """
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        plan: dict[int, int] = {}   # xref → 印刷用に書き換える /F
        blank = content == self.PRINT_FIELDS
        for page in self._doc:
            for xref, atype, _id in page.annot_xrefs():
                if atype in (fitz.PDF_ANNOT_LINK, fitz.PDF_ANNOT_POPUP):
                    continue
                f = self._doc.xref_get_key(xref, "F")
                flags = int(f[1]) if f[0] == "int" else 0
                new = flags
                if not flags & self._F_PRINT or flags & self._F_HIDDEN:
                    new |= self._F_HIDDEN          # 印刷しないもの
                elif flags & self._F_NOVIEW:
                    new &= ~self._F_NOVIEW         # 印刷だけするもの
                keep = {
                    self.PRINT_ALL: True,
                    self.PRINT_DOC: atype == fitz.PDF_ANNOT_WIDGET,
                }.get(content, atype in self._FILLED_TYPES)
                if not keep:
                    new |= self._F_HIDDEN
                if new != flags:
                    plan[xref] = new
        if not plan and not blank:
            return None
        copy = PdfDocument()
        copy._doc = fitz.open("pdf", self._doc.tobytes())
        copy.path = self.path
        for xref, new in plan.items():
            copy._doc.xref_set_key(xref, "F", str(new))
        if blank:
            # 紙面（印刷済みの用紙に既にある部分）は描かず、入れた値と文字だけ
            for page in copy._doc:
                for x in page.get_contents():
                    copy._doc.update_stream(x, b"")
        return copy

    def stripped_copy(self) -> "PdfDocument":
        """注釈（コメント類）を外した使い捨ての複製を返す。

        印刷の「文書のみ」用。入力欄（Widget）は annots() に出てこないので
        そのまま残る＝フォームに入力した値は消えない。使い終わったら close()。
        """
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")
        copy = PdfDocument()
        copy._doc = fitz.open("pdf", self._doc.tobytes())
        copy.path = self.path
        for page in copy._doc:
            for annot in list(page.annots()):
                page.delete_annot(annot)
        return copy

    # --- 画像を再圧縮する「圧縮」 -------------------------------------
    #: 圧縮プリセット（キー → (表示名, 画像の最大DPI, JPEG品質, 説明)）
    COMPRESS_PRESETS = {
        "high": ("高画質（印刷向け）", 200, 85,
                 "印刷しても粗さが分かりにくい。縮小率は控えめ。"),
        "standard": ("標準（画面表示向け）", 150, 72,
                     "画面で見る/メールで送る用途の既定。バランス重視。"),
        "small": ("最小サイズ（メール添付向け）", 96, 45,
                  "とにかく軽く。拡大すると画像の粗さが目立つ。"),
        "lossless": ("無劣化（構造の整理のみ）", 0, 0,
                     "画像はそのまま。不要オブジェクトの削除と圧縮だけ行う。"),
    }

    @staticmethod
    def _recompress_image_bytes(
        data: bytes, ext: str, disp_pt: float,
        max_dpi: int, quality: int, grayscale: bool, orig_bytes: int = 0,
    ) -> bytes | None:
        """埋め込み画像 1 枚を縮小＋再エンコードする。縮まないなら None。

        disp_pt はページ上での表示幅（ポイント）。ピクセル幅と突き合わせて実効
        解像度を求め、max_dpi を超える分だけダウンサンプルする（元から低解像度
        の画像を無駄に作り直さない）。JPEG と PNG の両方を試して小さい方を採る
        ため、線画やスクリーンショットが JPEG 化で逆に太ることがない。

        orig_bytes は PDF に格納されている実際のストリーム長。data（取り出した
        画像ファイル）とは容器が違って数 % ずれるので、得か損かの判定はこちらで
        行う（そうしないと縮んだつもりでファイルが太る）。
        """
        import io

        from PIL import Image

        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception:  # noqa: BLE001  JPEG2000 等 Pillow が開けない形式
            return None
        # 透明度つきは JPEG で失われ、PNG 化しても得が少ないので触らない
        if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
            return None
        if grayscale:
            im = im.convert("L")
        elif im.mode not in ("RGB", "L"):
            im = im.convert("RGB")

        if max_dpi > 0 and disp_pt > 0:
            eff_dpi = im.width / (disp_pt / 72.0)
            if eff_dpi > max_dpi:
                f = max_dpi / eff_dpi
                im = im.resize(
                    (max(1, round(im.width * f)), max(1, round(im.height * f))),
                    Image.Resampling.LANCZOS,
                )

        cands: list[bytes] = []
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=max(1, quality), optimize=True)
        cands.append(buf.getvalue())
        # 元が可逆形式で色数が少ない画像（線画・図表・スクリーンショット）は
        # PNG のままの方がずっと小さい。写真やスキャンは色数で弾いて JPEG だけ
        # 試す（PNG 化は遅いうえ、そもそも数倍に膨らむ）。
        if ext not in ("jpeg", "jpg", "jpx") and im.getcolors(maxcolors=4096):
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=True)
            cands.append(buf.getvalue())
        best = min(cands, key=len)
        # 5% も縮まないなら劣化させる意味がないので元を残す
        return best if len(best) < (orig_bytes or len(data)) * 0.95 else None

    @staticmethod
    def _image_targets(doc: "fitz.Document") -> list[tuple[int, int, float]]:
        """(xref, 代表ページ番号, ページ上の最大表示幅pt) の一覧を返す。

        同じ画像が複数ページで使い回されている場合、xref は 1 つなので 1 回だけ
        処理する（replace_image は全ページに反映される）。表示幅は最も大きく
        置かれている箇所を採り、そこに合わせて解像度を決める。
        """
        found: dict[int, tuple[int, float]] = {}
        for pno in range(doc.page_count):
            page = doc.load_page(pno)
            for info in page.get_images(full=True):
                xref = info[0]
                try:
                    width = max((r.width for r in page.get_image_rects(xref)),
                                default=0.0)
                except Exception:  # noqa: BLE001  壊れた配置情報は無視
                    width = 0.0
                prev = found.get(xref)
                if prev is None or width > prev[1]:
                    found[xref] = (pno, width)
        return [(x, p, w) for x, (p, w) in found.items()]

    def compress_to_advanced(
        self,
        out_path: str,
        *,
        max_dpi: int = 150,
        jpeg_quality: int = 72,
        grayscale: bool = False,
        min_bytes: int = 8 * 1024,
        progress=None,
    ) -> dict:
        """埋め込み画像を縮小・再圧縮して out_path に保存する。

        テキストやベクター図形はそのまま残す（ページを画像化しない）ので、
        文字の検索・コピーや注釈は保たれる。max_dpi=0 なら画像には触らず
        compress_to と同じ無劣化の整理だけを行う。

        progress(done, total) が False を返したら中断し、何も保存せずに
        {"canceled": True} を含む結果を返す。
        戻り値: images/recompressed/skipped/canceled を持つ dict。
        """
        if not self._doc:
            raise RuntimeError("ドキュメントが開かれていません")

        stats = {"images": 0, "recompressed": 0, "skipped": 0, "canceled": False}
        # 開いている文書自体は劣化させたくないので作業用コピーで処理する
        work = fitz.open("pdf", self._doc.tobytes(deflate=True))
        try:
            if max_dpi > 0 or grayscale:
                try:
                    from PIL import Image  # noqa: F401  同梱確認だけ
                except ImportError as exc:  # ライト版は Pillow を含まない
                    raise RuntimeError(
                        "この構成では画像の再圧縮を行えません（軽量版）。"
                        "「最適化して保存（無劣化）」をお使いください。") from exc
                targets = self._image_targets(work)
                stats["images"] = len(targets)
                for n, (xref, pno, disp_pt) in enumerate(targets):
                    if progress and not progress(n, len(targets)):
                        stats["canceled"] = True
                        return stats
                    try:
                        info = work.extract_image(xref)
                    except Exception:  # noqa: BLE001
                        stats["skipped"] += 1
                        continue
                    data = info.get("image") or b""
                    try:  # PDF に入っている実際のストリーム長（圧縮後）
                        orig_bytes = len(work.xref_stream_raw(xref))
                    except Exception:  # noqa: BLE001
                        orig_bytes = len(data)
                    # 小さすぎる画像・白黒2値（CCITT/JBIG2）・透明マスク付きは対象外。
                    # 2値はもともと極小で、JPEG 化するとむしろ太る。
                    if (orig_bytes < min_bytes or info.get("bpc") == 1
                            or info.get("smask")):
                        stats["skipped"] += 1
                        continue
                    new = self._recompress_image_bytes(
                        data, (info.get("ext") or "").lower(), disp_pt,
                        max_dpi, jpeg_quality, grayscale, orig_bytes,
                    )
                    if not new:
                        stats["skipped"] += 1
                        continue
                    try:
                        work.load_page(pno).replace_image(xref, stream=new)
                    except Exception:  # noqa: BLE001  差し替え不可な画像は諦める
                        stats["skipped"] += 1
                        continue
                    stats["recompressed"] += 1
                if progress:
                    progress(len(targets), len(targets))

            kwargs = dict(garbage=4, deflate=True, deflate_images=True,
                          deflate_fonts=True, clean=True, use_objstms=1,
                          **self._protection_kwargs())
            try:
                if self.path and _same_path(self.path, out_path):
                    # 自分自身への上書き。置き換えが成功してから開き直す
                    # （失敗しても、開いている文書は無事なままにする）。
                    tmp = out_path + ".tmp_save"
                    work.save(tmp, **kwargs)
                    work.close()
                    work = None
                    try:
                        os.replace(tmp, out_path)
                    except OSError:
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                        raise
                    with open(out_path, "rb") as f:
                        data = f.read()
                    self._doc.close()
                    self._doc = fitz.open(stream=data, filetype="pdf")
                    if self._protect:   # 保護を付けて書いたので、開き直して復号する
                        self._doc.authenticate(self._protect["user_pw"])
                        self._adopt_decrypted(self._protect["user_pw"])
                    self._renumbered()   # 別の中身に差し替えた
                    self.path = out_path
                    self.modified = False
                else:
                    self._replace_atomically(out_path, lambda t: work.save(t, **kwargs))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(friendly_error(exc, saving=True)) from exc
        finally:
            if work is not None:
                work.close()
        return stats

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
        self._save_doc(tmp, garbage=4, deflate=True, encryption=fitz.PDF_ENCRYPT_NONE)
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
