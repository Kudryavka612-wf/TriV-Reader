"""フォームの入力候補（以前に入力した内容）。Acrobat の「自動入力」にあたる。

氏名・住所・電話番号のように何度も同じことを書く欄で、前に入れた内容を候補に
出す。同じ項目（フォームの JS が持つ項目名。無ければ欄の名前）で入れたものを
先に、ほかの欄で入れたものをその後に並べる。

覚えないもの: マイナンバー・口座番号・パスワード・暗証番号など（項目名で判断）、
12 桁（マイナンバー）や 14〜16 桁（カード番号）の数字、パスワード欄、1 文字だけ・
長すぎる値・複数行の値。履歴は利用者のデータフォルダの JSON に置き、設定の
「フォーム」から消せる。
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

# 項目名にこれを含む欄は覚えない
SENSITIVE_WORDS = ("個人番号", "マイナンバー", "口座番号", "暗証", "パスワード",
                   "password", "カード番号", "セキュリティコード")
_SEPARATORS_RE = re.compile(r"[\s\-‐‑‒–—―ー－]")

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


def is_sensitive(value: str, *names: str) -> bool:
    """覚えてはいけない値か（項目名・欄の名前と値そのものから判断）。"""
    text = " ".join(n for n in names if n).lower()
    if any(w.lower() in text for w in SENSITIVE_WORDS):
        return True
    digits = _SEPARATORS_RE.sub("", unicodedata.normalize("NFKC", value))
    return digits.isdigit() and len(digits) in (12, 14, 15, 16)


class FormHistory:
    """[値, 項目, 時刻] を新しい順に持つ。値が同じものは 1 つにまとめる。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._entries: list | None = None

    def _load(self) -> list:
        if self._entries is None:
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                self._entries = [e for e in data.get("entries", [])
                                 if isinstance(e, list) and len(e) == 3
                                 and isinstance(e[0], str)]
            except (OSError, ValueError, AttributeError):
                self._entries = []
        return self._entries

    def add(self, value: str, key: str, *names: str) -> bool:
        """覚える（覚えてよい値なら True）。"""
        value = (value or "").strip()
        if (len(value) < 2 or len(value) > MAX_LEN or "\n" in value
                or is_sensitive(value, key, *names)):
            return False
        entries = self._load()
        entries[:] = [e for e in entries if not (e[0] == value and e[1] == key)]
        entries.insert(0, [value, key or "", int(time.time())])
        del entries[MAX_ENTRIES:]
        self._save()
        return True

    def suggestions(self, key: str, max_len: int = 0) -> list[str]:
        """候補。この項目で入れたものを先に、ほかで入れたものを後に（重複なし）。"""
        entries = self._load()
        own = [e[0] for e in entries if e[1] == key]
        rest = [e[0] for e in entries if e[1] != key]
        out: list[str] = []
        for v in own + rest:
            if v not in out and (not max_len or len(v) <= max_len):
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
                json.dump({"version": 1, "entries": self._entries}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass   # 書けなくても入力そのものは止めない
