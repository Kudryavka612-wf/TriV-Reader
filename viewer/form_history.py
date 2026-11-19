"""フォームの入力候補（以前に入力した内容）。Acrobat の「自動入力」にあたる。

氏名・住所のように何度も同じことを書く欄で、前に入れた内容を候補に出す。

事務所の決定（個人情報の扱い）:
- 候補は「同じ名前の欄」で入れたものだけ。欄の名前（PDF の欄の名前）を
  正規化してから比べ、ほかの欄（別の顧問先のフォームの別の欄など）で入れた
  ものは出さない。
- 覚えないもの: 空白・ハイフンを除き全角数字を半角にしてから 7 桁以上の数字が
  続く値（電話・郵便番号・口座・マイナンバー・基礎年金番号など）。欄の名前・
  欄の説明（/TU）・フォームの JS が持つ項目名のどれかが、番号・ID・口座・
  パスワード・暗証番号・コードなどを思わせる欄の値。1 文字だけ・長すぎる値・
  複数行の値。パスワード欄は呼び出し側で候補を付けない。
- 覚えるのは、利用者が確定した値だけ（Enter・Tab・欄の外のクリック）。保存・
  自動保存・拡大縮小などで書き込んだ打ちかけの値は、呼び出し側で渡さない。
- 以前の版で覚えた値のうち、いまの決まりで覚えないものは、読み込んだときに
  消してファイルも書き直す。

履歴は利用者のデータフォルダの JSON に置き、設定の「フォーム」から消せる。
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata

from . import storage

MAX_ENTRIES = 400
MAX_LEN = 120
MAX_DIGITS = 6     # これより長く数字が続く値は覚えない（7 桁以上）

# 欄の名前・説明にこれを含む欄は覚えない（NFKC・小文字にしてから比べる）
SENSITIVE_WORDS = (
    "番号", "ナンバー", "なんばー", "マイナンバー", "個人番号", "口座", "暗証",
    "パスワード", "ぱすわーど", "コード", "こーど", "基礎年金", "被保険者",
    "保険証", "証券", "記号", "カード", "セキュリティ", "免許", "旅券", "パスポート",
    "受給者", "暗号",
    "kouza", "kooza", "koza", "account", "acct", "pass", "mynumber", "my_number",
    "kojinbango", "kojin_bango", "bango", "bangou", "number", "code", "security",
    "secret", "card", "nenkin", "hihoken", "hokensho", "license", "licence", "token",
)
# 単語として現れたときだけ覚えない短い語（"pin" は "shipping" に、"no" は "notes" に含まれる）
_SENSITIVE_TOKEN_RE = re.compile(
    r"(?<![a-z])(?:no|num|nr|pin|id|cd|pw|pwd)(?![a-z])", re.IGNORECASE)
# 大文字で区切った書き方（TelNo・UserID・PinCode）と記号
_SENSITIVE_CASED_RE = re.compile(r"No(?![a-z])|NO(?![A-Z])|ID(?![a-z])|Id(?![a-z])|PIN|Pin(?![a-z])|№")
_SEPARATORS_RE = re.compile(r"[\s\-‐‑‒–—―ー－−_]")
_LONG_DIGITS_RE = re.compile(r"\d{%d,}" % (MAX_DIGITS + 1))

_enabled = True
_history: "FormHistory | None" = None


def set_enabled(on: bool) -> None:
    global _enabled
    _enabled = bool(on)


def enabled() -> bool:
    return _enabled


def history() -> "FormHistory":
    global _history
    if _history is None:
        _history = FormHistory(history_path())
    return _history


def history_path() -> str:
    """履歴の置き場所。テストで使い捨ての設定にしているときは、本人の履歴に
    触れないよう、その設定の隣（使い捨てなら一時フォルダ）に置く。"""
    override = os.environ.get("TRIVREADER_SETTINGS_INI")
    if override:
        if override == "throwaway":
            import atexit
            import tempfile
            path = os.path.join(tempfile.gettempdir(),
                                f"_trv_form_history_{os.getpid()}.json")
            atexit.register(lambda: os.path.exists(path) and os.remove(path))
            return path
        return os.path.splitext(override)[0] + "_form_history.json"
    return os.path.join(storage.data_dir(), "form_history.json")


def normalize_key(name: str) -> str:
    """候補を引く鍵（欄の名前）。全角半角・大文字小文字・空白の違いは同じ欄とみなす。"""
    text = unicodedata.normalize("NFKC", name or "").casefold()
    return re.sub(r"\s+", "", text)


def sensitive_name(*names: str) -> bool:
    """欄の名前・説明・項目名のどれかが、番号・ID・口座・パスワードなどを思わせるか。"""
    for raw in names:
        if not raw:
            continue
        text = unicodedata.normalize("NFKC", str(raw))
        low = text.casefold()
        if any(w in low for w in SENSITIVE_WORDS):
            return True
        if _SENSITIVE_CASED_RE.search(text) or _SENSITIVE_TOKEN_RE.search(text):
            return True
    return False


def sensitive_value(value: str) -> bool:
    """値そのものが覚えてはいけない形か（7 桁以上の数字が続く）。"""
    text = _SEPARATORS_RE.sub("", unicodedata.normalize("NFKC", value or ""))
    return bool(_LONG_DIGITS_RE.search(text))


def is_sensitive(value: str, *names: str) -> bool:
    """覚えてはいけない値か（項目名・欄の名前と値そのものから判断）。"""
    return sensitive_name(*names) or sensitive_value(value)


def _keep(value: str, key: str) -> bool:
    return (2 <= len(value) <= MAX_LEN and "\n" not in value
            and not is_sensitive(value, key))


class FormHistory:
    """[値, 欄の鍵, 時刻] を新しい順に持つ。同じ欄で値が同じものは 1 つにまとめる。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._entries: list | None = None

    def _load(self) -> list:
        if self._entries is None:
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                raw = [e for e in data.get("entries", [])
                       if isinstance(e, list) and len(e) == 3
                       and isinstance(e[0], str) and isinstance(e[1], str)]
            except (OSError, ValueError, AttributeError):
                raw = []
            # 以前の版で覚えた値も、いまの決まりで覚えないものは捨てる（ファイルからも）
            entries, seen = [], set()
            for value, key, stamp in raw:
                key = normalize_key(key)
                if not _keep(value, key) or (value, key) in seen:
                    continue
                seen.add((value, key))
                entries.append([value, key, stamp])
            self._entries = entries[:MAX_ENTRIES]
            if len(self._entries) != len(raw):
                self._save()
        return self._entries

    def add(self, value: str, key: str, *names: str) -> bool:
        """覚える（覚えてよい値なら True）。

        key は欄の名前。names には欄の説明（/TU）・フォームの JS の項目名など、
        覚えてよい欄かを判断する手がかりを渡す。
        """
        value = (value or "").strip()
        raw_key, key = key or "", normalize_key(key)
        # 名前は正規化の前の形でも見る（"TelNo" の大文字の区切りは小文字にすると消える）
        if not key or not _keep(value, key) or is_sensitive(value, raw_key, *names):
            return False
        entries = self._load()
        entries[:] = [e for e in entries if not (e[0] == value and e[1] == key)]
        entries.insert(0, [value, key, int(time.time())])
        del entries[MAX_ENTRIES:]
        self._save()
        return True

    def suggestions(self, key: str, max_len: int = 0, *names: str) -> list[str]:
        """候補。同じ名前の欄で入れたものだけ（新しい順・重複なし）。

        番号・口座などを思わせる欄（names も見る）には何も出さない。
        """
        raw_key, key = key or "", normalize_key(key)
        if not key or sensitive_name(raw_key, key, *names):
            return []
        out: list[str] = []
        for v, k, _t in self._load():
            if k == key and v not in out and (not max_len or len(v) <= max_len):
                out.append(v)
        return out

    def count(self) -> int:
        return len(self._load())

    def clear(self) -> None:
        self._entries = []
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 2, "entries": self._entries}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass   # 書けなくても入力そのものは止めない
