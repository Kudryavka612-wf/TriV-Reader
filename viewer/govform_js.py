"""官公庁系フォーム PDF の「丸で囲んで選択」JS トグルへの限定対応。

厚生労働省/ハローワーク/税務署等の申請書 PDF では、日付や区分の選択を
ネイティブのラジオボタンではなく、同じ rect を共有する2つの Button 型
ウィジェット（選択中/非選択の2つの見た目）＋ Acrobat JavaScript の
クリックハンドラで実現していることが多い（本ファイルはこの1パターンに
限定して対応する。フル Acrobat JS 互換は目的としない）。

対応範囲は「ドキュメントが GroupButtonClick / KinmuButtonClick という
既知のヘルパー関数を自前定義している場合に限り、クリックされたウィジェット
自身の /A スクリプトだけを、py_mini_racer 上でこの2関数のみを注入して
実行し、表示状態（/F の Hidden ビット）の変化だけを PDF に反映する」。
Doc-Open 等の自動実行は行わず、ユーザーのクリックに応答してのみ動く。
"""
from __future__ import annotations

import json
import re

import fitz

# --- 実行を許可するヘルパー関数名（ここに書かれた名前だけを対象文書から
#     抽出して V8 に注入する。文書 JS 全体は絶対に注入しない）------------
ALLOWED_HELPERS = ("GroupButtonClick", "KinmuButtonClick")

# ウィジェットの /A スクリプトが「単純な no-arg-besides-event の関数呼び出し」
# であることを確認するための形状チェック（それ以外は何もしない）。
SIMPLE_CALL_RE = re.compile(r"^(\w+)\(event\)$")

# 選択/非選択ペアの命名ヒント（あくまで UI 側の参考用。実際のスコープ判定は
# is_eligible の「ドキュメントが対象関数を定義しているか」のみで行う）。
FRM_SUFFIX_RE = re.compile(r"^(.*)_(01|ON|OFF)$")

_JS_EXEC_TIMEOUT_MS = 2000

# display の意味は PyMuPDF の field_display と同じ整数（Adobe の display.* も同様）。
DISPLAY_VISIBLE = 0
DISPLAY_HIDDEN = 1
DISPLAY_NOVIEW = 2
DISPLAY_HIDDEN_NOVIEW = 3

_HIDDEN_BIT = 2  # PDF 注釈 /F フラグの Hidden ビット


def extract_doc_scripts(doc: "fitz.Document") -> dict[str, str]:
    """カタログの Names/JavaScript 名前ツリーから、文書レベル JS を全部集める。

    高レベル API では辿れないため xref を直接読む。想定外の構造は
    例外を握りつぶして {} を返す（アプリの起動を絶対に壊さないため）。
    """
    out: dict[str, str] = {}
    try:
        catalog_xref = doc.pdf_catalog()
        names_ref = doc.xref_get_key(catalog_xref, "Names")
        if names_ref[0] != "xref":
            return out
        names_xref = int(names_ref[1].split()[0])
        js_tree_ref = doc.xref_get_key(names_xref, "JavaScript")
        if js_tree_ref[0] != "xref":
            return out
        js_xref = int(js_tree_ref[1].split()[0])
        names_arr = doc.xref_get_key(js_xref, "Names")
        if names_arr[0] != "array":
            return out
        # "(name1)N1 0 R(name2)N2 0 R..." 形式をペアで抜き出す
        pairs = re.findall(r"\(([^()]*)\)\s*(\d+)\s+0\s+R", names_arr[1])
        for name, xref_s in pairs:
            try:
                xref = int(xref_s)
                js_ref = doc.xref_get_key(xref, "JS")
                if js_ref[0] != "xref":
                    continue
                stream_xref = int(js_ref[1].split()[0])
                raw = doc.xref_stream(stream_xref)
                if raw is None:
                    continue
                out[name] = _decode_js_stream(raw)
            except Exception:  # noqa: BLE001  - 1件の異常で全体を諦めない
                continue
    except Exception:  # noqa: BLE001
        return {}
    return out


def _decode_js_stream(raw: bytes) -> str:
    """JS ストリームは UTF-16 (BOM付き) のことが多く、utf-8 決め打ちだと文字化けする。"""
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def is_eligible(doc_scripts: dict[str, str]) -> bool:
    """この文書が「安全に実行できると分かっている」ヘルパーを持つか。

    発行元（省庁名など）の推測はしない（実サンプルでメタデータが
    空だったため信頼できない）。「実行してよい関数を実際に定義しているか」
    だけを見るほうが堅牢かつ安全。
    """
    if not doc_scripts:
        return False
    combined = "\n".join(doc_scripts.values())
    return any(
        re.search(r"function\s+" + re.escape(name) + r"\s*\(", combined)
        for name in ALLOWED_HELPERS
    )


def _find_function_source(combined: str, name: str) -> str | None:
    """combined 文字列中の `function <name>(...){ ... }` をブレース対応で1つ抜き出す。"""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", combined)
    if not m:
        return None
    brace_start = combined.find("{", m.start())
    if brace_start < 0:
        return None
    depth = 0
    for i in range(brace_start, len(combined)):
        ch = combined[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return combined[m.start():i + 1]
    return None  # 対応する閉じ括弧が見つからない（壊れた/想定外の構造）


class FieldBridge:
    """JS の getField() が返すオブジェクトの元になる、ウィジェット1個分のスナップショット。

    py_mini_racer は JSON 互換値の受け渡ししかできず、Python オブジェクトを
    ライブなプロキシとして JS 側から読み書きさせることはできない。そのため
    実際の getField() オブジェクトは V8 内の素の JS オブジェクト
    （__registerField が作る）として実装するが、その初期値はこのクラスの
    フィールドから作る。display の変化だけを実行後に読み戻して PDF に反映
    する（value・required・strokeColor 等はこのスコープでは書き戻さない）。
    """

    def __init__(self, name: str, display: int, value, readonly: bool) -> None:
        self.name = name
        self.display = display
        self.value = value
        self.valueAsString = "" if value is None else str(value)
        self.readonly = readonly
        self.required = False
        self.strokeColor = None

    @classmethod
    def from_widget(cls, w) -> "FieldBridge":
        flags = int(getattr(w, "field_flags", 0) or 0)
        readonly_bit = 1 << 0
        return cls(
            name=w.field_name,
            display=int(w.field_display),
            value=w.field_value,
            readonly=bool(flags & readonly_bit),
        )

    def register_js(self, ctx) -> None:
        """このスナップショットを V8 コンテキスト内の __fields[name] として登録する。"""
        ctx.eval(
            "__registerField(%s, %d, %s, %s);"
            % (_js_str(self.name), self.display,
               _js_str(self.valueAsString), "true" if self.readonly else "false"),
            timeout=_JS_EXEC_TIMEOUT_MS,
        )


def run_click(doc: "fitz.Document", page_index: int, widget_xref: int,
              doc_scripts: dict[str, str]) -> bool:
    """widget_xref のウィジェットをクリックしたとして /A スクリプトを実行する。

    実際に表示状態が変わったウィジェットがあれば PDF の /F に書き戻して
    True を返す。何かがおかしければ何もせず False を返す（例外は必ずここで
    握りつぶし、呼び出し元やアプリ全体を絶対に落とさない）。
    """
    try:
        return _run_click_impl(doc, page_index, widget_xref, doc_scripts)
    except Exception:  # noqa: BLE001
        return False


def _run_click_impl(doc: "fitz.Document", page_index: int, widget_xref: int,
                     doc_scripts: dict[str, str]) -> bool:
    from py_mini_racer import MiniRacer  # 使う時だけ import（起動時コストを避ける）

    if not is_eligible(doc_scripts):
        return False

    page = doc.load_page(page_index)
    widgets = list(page.widgets() or [])
    clicked = next((w for w in widgets if w.xref == widget_xref), None)
    if clicked is None:
        return False

    script = clicked.script or ""
    m = SIMPLE_CALL_RE.match(script.strip())
    if not m:
        # 単純な「識別子(event)」形でなければ、何を実行することになるか
        # 保証できないため何もしない。
        return False
    entry_name = m.group(1)

    combined = "\n".join(doc_scripts.values())
    helper_sources = []
    for name in ALLOWED_HELPERS:
        src = _find_function_source(combined, name)
        if src:
            helper_sources.append(src)
    if not helper_sources:
        return False

    # entry_name 自体が GroupButtonClick/KinmuButtonClick そのものの場合と、
    # それらを内部で呼ぶラッパー関数（例: FRM6010_03_004_1_MouseUp）の場合が
    # 実ファイルで両方観測されている。ラッパーなら定義も追加で注入する。
    entry_source = None
    if entry_name not in ALLOWED_HELPERS:
        entry_source = _find_function_source(combined, entry_name)
        if entry_source is None:
            return False

    # --- 各ウィジェットの現在状態を Python 側に集約（getField 用）------
    before_display: dict[str, int] = {}
    bridges: list[FieldBridge] = []
    for w in widgets:
        if not w.field_name:
            continue
        bridge = FieldBridge.from_widget(w)
        before_display[bridge.name] = bridge.display
        bridges.append(bridge)

    ctx = MiniRacer()
    ctx.eval(
        """
        var display = {visible:0, hidden:1, noView:2, hiddenNoView:3};
        var global = {};
        var app = {alert: function(){}, beep: function(){}};
        var __fields = {};
        function __registerField(name, disp, value, readonly){
            __fields[name] = {
                name: name,
                display: disp,
                value: value,
                valueAsString: (value === null || value === undefined) ? "" : String(value),
                readonly: readonly,
                required: false,
                strokeColor: null,
                setFocus: function(){}
            };
        }
        function getField(name){
            if (!__fields[name]) { __registerField(name, 0, "", false); }
            return __fields[name];
        }
        var __this = this;
        __this.getField = getField;
        """,
        timeout=_JS_EXEC_TIMEOUT_MS,
    )
    for bridge in bridges:
        bridge.register_js(ctx)

    # 許可されたヘルパーだけを注入する（文書 JS 全体は絶対に注入しない）
    for src in helper_sources:
        ctx.eval(src, timeout=_JS_EXEC_TIMEOUT_MS)
    if entry_source:
        ctx.eval(entry_source, timeout=_JS_EXEC_TIMEOUT_MS)

    # entry のラッパー本体が CHK6010_* / AlertError2 等の未定義関数を呼んでも
    # トグル自体は既に走った後なので、try/catch で無害に握りつぶす。
    ctx.eval(
        """
        function __run_click(eventTargetName){
            var event = {target: {name: eventTargetName}};
            try { %s(event); } catch (e) { /* validation 等は対象外・無視 */ }
        }
        """ % entry_name,
        timeout=_JS_EXEC_TIMEOUT_MS,
    )
    ctx.eval("__run_click(%s); null;" % _js_str(clicked.field_name),
             timeout=_JS_EXEC_TIMEOUT_MS)

    # --- 変化した display を PDF の /F に反映する -------------------------
    changed = False
    for w in widgets:
        if not w.field_name or w.field_name not in before_display:
            continue
        try:
            new_disp = ctx.eval(
                "__fields[%s] ? __fields[%s].display : -1"
                % (_js_str(w.field_name), _js_str(w.field_name)),
                timeout=_JS_EXEC_TIMEOUT_MS,
            )
        except Exception:  # noqa: BLE001
            continue
        if new_disp is None or new_disp < 0:
            continue
        new_disp = int(new_disp)
        if new_disp == before_display[w.field_name]:
            continue
        if _apply_display(doc, w.xref, new_disp):
            changed = True
    return changed


def _apply_display(doc: "fitz.Document", xref: int, new_display: int) -> bool:
    """display 値（0..3）を /F の Hidden ビットへ変換して書き戻す。

    Print 等、Hidden 以外の既存ビットはそのまま保持する。
    """
    try:
        cur = doc.xref_get_key(xref, "F")
        cur_int = int(cur[1]) if cur and cur[0] not in ("null", None) else 0
        hidden = new_display in (DISPLAY_HIDDEN, DISPLAY_HIDDEN_NOVIEW)
        new_int = (cur_int | _HIDDEN_BIT) if hidden else (cur_int & ~_HIDDEN_BIT)
        if new_int == cur_int:
            return False
        doc.xref_set_key(xref, "F", str(new_int))
        return True
    except Exception:  # noqa: BLE001
        return False


def _js_str(s: str) -> str:
    """Python 文字列を JS の文字列リテラルソースへ（安全にエスケープして）変換する。"""
    return json.dumps(s)
