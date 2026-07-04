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
# いずれも「フィールドの表示状態(display)か値(value)だけを操作する」既知の
# ヘルパー。入力チェック(CHK*/AlertError)や日付範囲制約(SetDateComb)等は
# 対象外（未注入なら実行時に ReferenceError となり try/catch で無害に無視）。
ALLOWED_HELPERS = (
    "GroupButtonClick",   # クリックで「丸選択」をトグル（表示切替）
    "KinmuButtonClick",   # 勤務状況の複数状態トグル（表示切替）
    "GroupCheckboxClick",  # チェックボックス群の排他選択（他を「いいえ」に）
    "SetShinryoAll",      # 診療日を31日分まとめて選択（表示切替）
    "SetKinmuAll",        # 勤務日を31日分まとめて選択（表示切替）
    "StateNameSelect",    # 都道府県コンボ選択で枝ラベルを表示/非表示
    "DropDownSelect",     # コンボ選択値を相棒の _PRINT 欄へ転写（値書き込み）
)

# ウィジェットのスクリプトが「単純な no-arg-besides-event の関数呼び出し」
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


# 「入力補助」を担うアクション種別。ここに絞ることで、ツールチップ(Focus)や
# 入力チェック(Blur)は「未対応」として拾わない（それらは方針として対象外で、
# 報告対象にしても実装しないため）。クリックとコンボ選択のみを見る。
_INTERACTION_ACTION_KEYS = ("A", "AA/K")

# フィールドの表示か値を変える＝「操作結果に効く」ヘルパーの目印。
_STATE_CHANGE_RE = re.compile(r"\.(?:display|value)\s*=(?!=)")
_CALL_RE = re.compile(r"([A-Za-z_]\w*)\s*\(")

# 「見たことはあるが、方針として対応しない」共有ヘルパー（装飾＝ツールチップ、
# 検証＝赤枠/必須、書式整形、日付範囲制約）。未対応検知ではこれらを除外し、
# “本当に未知の入力補助”だけをユーザーに知らせる。ここに無い新種ヘルパーが
# クリック/コンボから呼ばれていたら、それが報告対象になる。
KNOWN_UNSUPPORTED_HELPERS = frozenset({
    "ShowToolTip", "HideToolTip",                       # 装飾（ツールチップ）
    "AlertError", "AlertError2", "AlertSuccess", "AlertSuccess2",  # 検証の通知
    "SetRedBorder", "SetEnable",                        # 検証（赤枠・活性制御）
    "SetDateComb",                                      # 日付の範囲制約
    "FormatYearMonthDay", "FormatSingleByteNumber",     # 書式整形
})


def _widget_action_entries(doc: "fitz.Document") -> set[str]:
    """全ウィジェットのクリック/コンボ選択アクションが呼ぶ入口関数名を集める。"""
    entries: set[str] = set()
    try:
        for pno in range(doc.page_count):
            for w in (doc.load_page(pno).widgets() or []):
                for key in _INTERACTION_ACTION_KEYS:
                    try:
                        js = doc.xref_get_key(w.xref, key + "/JS")
                    except Exception:  # noqa: BLE001
                        continue
                    if js and js[0] == "string":
                        m = SIMPLE_CALL_RE.match(js[1].strip())
                        if m:
                            entries.add(m.group(1))
    except Exception:  # noqa: BLE001
        return set()
    return entries


def unsupported_helpers(doc: "fitz.Document",
                        doc_scripts: dict[str, str]) -> list[str]:
    """クリック/コンボ選択から到達する「表示/値を変えるヘルパー」のうち、
    まだ対応（ALLOWED_HELPERS）していないものの名前一覧を返す。

    これが空でなければ「このフォームには未対応の入力補助がある」＝
    ユーザーに知らせ、報告用に書き出せるようにする対象。
    """
    if not doc_scripts:
        return []
    combined = "\n".join(doc_scripts.values())
    fns = _all_function_bodies(combined)
    entries = _widget_action_entries(doc)

    # 入口から推移的に呼ぶ関数名を辿る（3段まで＝実フォームには十分）
    reached = set(entries)
    for _ in range(3):
        new = set()
        for n in list(reached):
            if n in fns:
                new |= set(_CALL_RE.findall(fns[n]))
        reached |= new

    unsupported = set()
    for name in reached:
        if name in ALLOWED_HELPERS:
            continue                 # 対応済み
        if name in KNOWN_UNSUPPORTED_HELPERS:
            continue                 # 方針として対象外（既知）
        if name in entries:
            continue                 # ウィジェット直結の入口ラッパー（実行はする）
        body = fns.get(name)
        if body and _STATE_CHANGE_RE.search(body):
            unsupported.add(name)    # 未知の“操作結果に効く”ヘルパー＝報告対象
    return sorted(unsupported)


def _all_function_bodies(combined: str) -> dict[str, str]:
    """combined 中の全 `function name(...){...}` を名前→本体で返す。"""
    out: dict[str, str] = {}
    for m in re.finditer(r"function\s+([A-Za-z0-9_]+)\s*\([^)]*\)\s*\{", combined):
        name = m.group(1)
        brace = combined.find("{", m.start())
        depth = 0
        for i in range(brace, len(combined)):
            if combined[i] == "{":
                depth += 1
            elif combined[i] == "}":
                depth -= 1
                if depth == 0:
                    out[name] = combined[m.start():i + 1]
                    break
    return out


def build_diagnostic(doc: "fitz.Document", doc_scripts: dict[str, str],
                     helper_names: list[str], header: dict) -> str:
    """未対応ヘルパーの実装に必要な情報をまとめた報告用テキストを作る。

    含めるのはフォーム由来のスクリプト定義（テンプレート側のコード）のみで、
    ユーザーが入力した値は含めない。
    """
    fns = _all_function_bodies("\n".join(doc_scripts.values()))
    lines = ["# TriV-Reader 未対応フォーム機能レポート", ""]
    for k, v in header.items():
        lines.append(f"{k}: {v}")
    lines.append("")
    lines.append(f"未対応ヘルパー ({len(helper_names)}件): " + ", ".join(helper_names))
    lines.append("")
    lines.append("---- 該当関数のソース（フォーム定義。入力値は含みません） ----")
    for name in helper_names:
        lines.append(f"\n// ===== {name} =====")
        lines.append(fns.get(name, "// (ソースが見つかりませんでした)"))
    return "\n".join(lines)


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


def widget_keystroke_js(doc: "fitz.Document", xref: int) -> str:
    """コンボ等の Keystroke アクション(/AA/K/JS)を取り出す。

    PyMuPDF の widget.script_change はこのフォームで空を返すことがあるため
    低レベルに /AA/K/JS を直接読む。無ければ空文字。
    """
    try:
        js = doc.xref_get_key(xref, "AA/K/JS")
        if js and js[0] == "string":
            return js[1]
    except Exception:  # noqa: BLE001
        pass
    return ""


def run_click(doc: "fitz.Document", page_index: int, widget_xref: int,
              doc_scripts: dict[str, str]) -> bool:
    """widget_xref のウィジェットをクリックしたとして /A スクリプトを実行する。

    実際に表示状態が変わったウィジェットがあれば PDF の /F に書き戻して
    True を返す。何かがおかしければ何もせず False を返す（例外は必ずここで
    握りつぶし、呼び出し元やアプリ全体を絶対に落とさない）。
    """
    try:
        clicked_script = None
        page = doc.load_page(page_index)
        for w in (page.widgets() or []):
            if w.xref == widget_xref:
                clicked_script = w.script or ""
                break
        if clicked_script is None:
            return False
        return _run_action_impl(doc, page_index, widget_xref, doc_scripts,
                                clicked_script, change_value=None)
    except Exception:  # noqa: BLE001
        return False


def run_keystroke(doc: "fitz.Document", page_index: int, widget_xref: int,
                  doc_scripts: dict[str, str], change_value: str) -> bool:
    """コンボ選択時の Keystroke(/AA/K)を実行し、値/表示の変化を PDF へ反映する。

    change_value は選択されたエクスポート値（event.changeEx 相当）。
    DropDownSelect による _PRINT 欄への転写や StateNameSelect による
    枝ラベルの表示切替が対象。失敗時は何もせず False。
    """
    try:
        entry_script = widget_keystroke_js(doc, widget_xref)
        if not entry_script:
            return False
        return _run_action_impl(doc, page_index, widget_xref, doc_scripts,
                                entry_script, change_value=change_value)
    except Exception:  # noqa: BLE001
        return False


def _run_action_impl(doc: "fitz.Document", page_index: int, widget_xref: int,
                     doc_scripts: dict[str, str], entry_script: str,
                     change_value) -> bool:
    from py_mini_racer import MiniRacer  # 使う時だけ import（起動時コストを避ける）

    if not is_eligible(doc_scripts):
        return False

    page = doc.load_page(page_index)
    widgets = list(page.widgets() or [])
    target = next((w for w in widgets if w.xref == widget_xref), None)
    if target is None:
        return False

    m = SIMPLE_CALL_RE.match((entry_script or "").strip())
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

    # entry_name 自体が許可ヘルパーそのものの場合と、それらを内部で呼ぶ
    # ラッパー関数（例: FRM6010_03_004_1_MouseUp / FRM6010_01_005_Keystroke）の
    # 場合が実ファイルで両方観測されている。ラッパーなら定義も追加で注入する。
    entry_source = None
    if entry_name not in ALLOWED_HELPERS:
        entry_source = _find_function_source(combined, entry_name)
        if entry_source is None:
            return False

    # --- 各ウィジェットの現在状態を Python 側に集約（getField 用）------
    before_display: dict[str, int] = {}
    before_value: dict[str, str] = {}
    bridges: list[FieldBridge] = []
    for w in widgets:
        if not w.field_name:
            continue
        bridge = FieldBridge.from_widget(w)
        before_display[bridge.name] = bridge.display
        before_value[bridge.name] = bridge.valueAsString
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
                _value: value,
                get value(){ return this._value; },
                set value(v){ this._value = v;
                    this.valueAsString = (v===null||v===undefined)?"":String(v); },
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

    # entry のラッパー本体が CHK6010_* / AlertError2 / SetDateComb 等の未注入
    # 関数を呼んでも、対象の表示/値変更は先に走った後なので try/catch で無視。
    # keystroke の場合は willCommit=false・changeEx を渡す（DropDownSelect 等が
    # この条件下でのみ動くため）。
    ctx.eval(
        """
        function __run_action(eventTargetName, changeEx, isKeystroke){
            var event = {target: {name: eventTargetName}};
            if (isKeystroke) { event.willCommit = false; event.changeEx = changeEx; }
            try { %s(event); } catch (e) { /* 対象外の処理は無視 */ }
        }
        """ % entry_name,
        timeout=_JS_EXEC_TIMEOUT_MS,
    )
    is_keystroke = change_value is not None
    ctx.eval(
        "__run_action(%s, %s, %s); null;" % (
            _js_str(target.field_name),
            _js_str("" if change_value is None else str(change_value)),
            "true" if is_keystroke else "false"),
        timeout=_JS_EXEC_TIMEOUT_MS,
    )

    # --- 変化した display / value を PDF に反映する ----------------------
    changed = False
    for w in widgets:
        name = w.field_name
        if not name or name not in before_display:
            continue
        try:
            new_disp = ctx.eval(
                "__fields[%s] ? __fields[%s].display : -1"
                % (_js_str(name), _js_str(name)),
                timeout=_JS_EXEC_TIMEOUT_MS,
            )
            new_val = ctx.eval(
                "__fields[%s] ? String(__fields[%s].valueAsString) : null"
                % (_js_str(name), _js_str(name)),
                timeout=_JS_EXEC_TIMEOUT_MS,
            )
        except Exception:  # noqa: BLE001
            continue
        if new_disp is not None and int(new_disp) >= 0 \
                and int(new_disp) != before_display[name]:
            if _apply_display(doc, w.xref, int(new_disp)):
                changed = True
        if new_val is not None and str(new_val) != before_value.get(name, ""):
            if _apply_value(w, str(new_val)):
                changed = True
    return changed


def _apply_value(widget, new_value: str) -> bool:
    """ウィジェットのフィールド値を書き込む（_PRINT 欄への転写用）。"""
    try:
        widget.field_value = new_value
        widget.update()
        return True
    except Exception:  # noqa: BLE001
        return False


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
