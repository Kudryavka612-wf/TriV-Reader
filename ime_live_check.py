"""本物の Microsoft IME で、フォームの欄に日本語を入れられるかを確かめる（手動実行）。

offscreen の QtTest は IME を通らない（変換中の再描画で確定が消える類の不具合は
見逃す）。これは画面に TriV-Reader を出し、OS の入力（SendInput）でローマ字・
変換・確定を打つ。送る直前に毎回、前面が TriV-Reader のウィンドウであることを
確かめ、違えば即中止する（ほかのアプリに打ち込まない）。CI では回さない。

    python ime_live_check.py
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

os.environ["TRIVREADER_SETTINGS_INI"] = "throwaway"
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
from viewer.main_window import MainWindow  # noqa: E402
from viewer import form_history  # noqa: E402

user32 = ctypes.windll.user32
imm32 = ctypes.windll.imm32
user32.GetForegroundWindow.restype = wintypes.HWND
imm32.ImmGetContext.argtypes = [wintypes.HWND]
imm32.ImmGetContext.restype = wintypes.HANDLE
imm32.ImmSetOpenStatus.argtypes = [wintypes.HANDLE, wintypes.BOOL]
imm32.ImmSetConversionStatus.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
imm32.ImmReleaseContext.argtypes = [wintypes.HWND, wintypes.HANDLE]
imm32.ImmGetOpenStatus.argtypes = [wintypes.HANDLE]

KEYEVENTF_KEYUP = 2
INPUT_KEYBOARD = 1


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def pump(sec):
    end = time.perf_counter() + sec
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.005)


class Abort(Exception):
    pass


def press(hwnd, vk):
    if user32.GetForegroundWindow() != hwnd:
        raise Abort("TriV-Reader が前面でなくなったので中止")
    arr = (INPUT * 2)()
    for i, flags in enumerate((0, KEYEVENTF_KEYUP)):
        arr[i].type = INPUT_KEYBOARD
        arr[i].ki = KEYBDINPUT(vk, 0, flags, 0, 0)
    user32.SendInput(2, arr, ctypes.sizeof(INPUT))
    pump(0.06)


def ime_on(hwnd, on=True):
    himc = imm32.ImmGetContext(hwnd)
    if not himc:
        return False
    imm32.ImmSetOpenStatus(himc, bool(on))
    if on:   # ひらがな（IME_CMODE_NATIVE | FULLSHAPE）
        imm32.ImmSetConversionStatus(himc, 0x1 | 0x8, 0)
    ok = bool(imm32.ImmGetOpenStatus(himc))
    imm32.ImmReleaseContext(hwnd, himc)
    return ok == bool(on)


def type_romaji(hwnd, text):
    for ch in text:
        press(hwnd, ord(ch.upper()))


w = MainWindow()
w.resize(1100, 820)
w.show()
w.open_path(os.path.join(ROOT, "tests", "fixtures", "6010_form_sample.pdf"))
pump(2.0)
w._bring_to_front()
pump(0.5)
hwnd = int(w.winId())
results = []
pv = w.page_view


def ov(name):
    return next(c for c in pv._form_overlays if c.property("trv_field") == name)


def field_value(name):
    return next(x.field_value for p in pv._doc._doc for x in p.widgets() if x.field_name == name)


try:
    if user32.GetForegroundWindow() != hwnd:
        raise Abort("TriV-Reader を前面に出せなかった（ほかのウィンドウに入力しないよう中止）")
    form_history.history().add("山田花子", "氏名")   # 候補の一覧が出る状況でも確かめる
    cases = [("FRM6010-01-008", "yamada", "1 行の欄（氏名・候補あり）"),
             ("FRM6010-01-013", "toukyou", "1 行の欄（市区町村）")]
    for name, romaji, label in cases:
        c = ov(name)
        pv.ensureWidgetVisible(c)
        c.setFocus()
        pump(0.3)
        if not ime_on(hwnd):
            raise Abort("IME をオンにできなかった")
        type_romaji(hwnd, romaji)
        press(hwnd, 0x20)       # 変換
        pump(0.3)
        press(hwnd, 0x0D)       # 確定
        pump(0.2)
        press(hwnd, 0x0D)       # 欄の確定（Enter）
        pump(0.3)
        text = c.text() if hasattr(c, "text") else c.toPlainText()
        ok_text = bool(text) and not any("a" <= ch.lower() <= "z" for ch in text)
        results.append((label, text, ok_text, field_value(name)))
    ime_on(hwnd, False)
except Abort as e:
    print("ABORT:", e, flush=True)
    results.append(("中止", "", False, None))
finally:
    try:
        ime_on(hwnd, False)
    except Exception:
        pass

for label, text, ok, saved in results:
    print(f"{'PASS' if ok and saved == text else 'FAIL'} {label}: 欄={text!r} 書き込み={saved!r}",
          flush=True)
ok = bool(results) and all(r[2] and r[3] == r[1] for r in results)
# 入力したファイルは保存しないで閉じる（os._exit で抜けると終了時に落ちることがある）。
# カーソルが欄に残っていると閉じるときに書き込まれ「保存しますか」で止まるので先に
w.page_view.finish_text_edit()
pump(0.2)
w.doc.modified = False
w._force_quit = True
w.close()
pump(0.3)
sys.exit(0 if ok else 1)
