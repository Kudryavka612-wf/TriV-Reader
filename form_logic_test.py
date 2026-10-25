"""公文書フォームの入力で、Acrobat Reader と同じに揃えた振る舞いの回帰テスト。

- 開いてすぐ入力できる（今のページの欄を先に作り、残りは入力の合間に作る）。
  途中のページから作っても Tab はページ順に回る
- 欄が数百あるページでも、欄の読み込みが欄数の二乗にならない
- 文字を全部消した欄が、消えたまま保存される（以前は消した文字が残った）
- フォームの JS が切り替える「入力できない欄」: 公金受取口座の利用で「2」を
  選ぶまで口座欄は入力も〇の選択もできない。「1」に変えると口座欄を消して
  入力できなくする。口座を入れたまま空欄にすると、警告して「2」に戻す
- フォームをクリア: 値も〇の選択も入力できる欄の切り替えも、配られたときの
  状態に戻る。元に戻す（Ctrl+Z）で入力した状態に戻る
- 入力候補: 以前に入れた内容を候補に出す。マイナンバー・口座番号などは覚えない。
  設定で止められ、履歴は消せる。テストでは本人の履歴に触れない
- 欄を抜けたときの入力チェック: フォームの JS のチェック（必須・年の範囲・元号の
  選び忘れ・半角カナ）を走らせ、誤りは止めずに知らせる（赤い枠・ステータスバー・
  欄のヒント）。直すと消え、拡大縮小しても残る。年は「5」→「05」にそろえる
  （元号を選んだ後も）。半角カナだけの欄は、ひらがな・全角カナを半角カナに直す

同梱のフィクスチャ（tests/fixtures/6010_form_sample.pdf）を使う。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 利用者本人の設定（レジストリ）を書き換えないよう、使い捨ての INI で動かす
os.environ.setdefault("TRIVREADER_SETTINGS_INI", "throwaway")

import fitz
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from viewer import fastwidgets, theme  # noqa: E402

FORM = os.path.join(ROOT, "tests", "fixtures", "6010_form_sample.pdf")
results: list[bool] = []


def check(name: str, ok, extra: str = "") -> bool:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    return bool(ok)


def key(ctl, k) -> None:
    """キーを押す。入力候補の一覧が開いていれば、本物のキーボードと同じく一覧へ。"""
    QTest.keyClick(QApplication.activePopupWidget() or ctl, k)


def pump(app, sec: float = 0.3) -> None:
    end = time.perf_counter() + sec
    while time.perf_counter() < end:
        app.processEvents()


def test_fast_widgets() -> None:
    with fitz.open(FORM) as d:
        same = True
        for page in d:
            a = [(w.xref, w.field_name, w.field_type, w.field_value, tuple(w.rect),
                  w.script, w.field_flags, w.field_display) for w in page.widgets()]
            b = [(w.xref, w.field_name, w.field_type, w.field_value, tuple(w.rect),
                  w.script, w.field_flags, w.field_display)
                 for w in fastwidgets.widgets(page)]
            same = same and a == b
        check("fast widget reader returns exactly what PyMuPDF returns", same)
        page = d[2]   # 勤務状況: 319 欄
        t0 = time.perf_counter()
        list(page.widgets())
        slow = time.perf_counter() - t0
        t0 = time.perf_counter()
        fastwidgets.widgets(page)
        fast = time.perf_counter() - t0
        check("…and reads a 319-field page much faster", fast < slow / 2,
              f"({fast * 1000:.0f}ms vs {slow * 1000:.0f}ms)")


def test_open_and_tab(app, w) -> None:
    t0 = time.perf_counter()
    w.open_path(FORM)
    pv = w.page_view
    first = None
    while time.perf_counter() - t0 < 4:
        app.processEvents()
        if first is None and pv._form_overlays:
            first = time.perf_counter() - t0
    # 手元では 0.3〜0.4 秒（以前は 1.7 秒）。遅い CI でも落ちないよう余裕を持たせる
    check("form fields are usable soon after opening", first is not None and first < 3.0,
          f"({first:.2f}s)" if first else "")
    n_all = len(pv._form_overlays)
    check("…and every page's fields are built", n_all > 250, f"({n_all})")

    # 途中のページで拡大縮小（＝そのページから作り直し）しても Tab はページ順
    pv.set_page(2)
    pump(app, 0.2)
    pv.set_zoom(pv.zoom * 1.1)
    pv._overlay_rebuild_timer.timeout.emit()
    pump(app, 2.0)
    check("after a rebuild from page 3 the first fields built are page 3's",
          pv._form_overlays and pv._form_overlays[0].property("trv_page") == 2)
    foc = [c for c in pv._form_overlays if c.focusPolicy() & Qt.FocusPolicy.TabFocus]
    start = next(c for c in foc if c.property("trv_page") == 0)
    pages, cur = [], start
    for _ in range(len(foc)):
        pages.append(cur.property("trv_page"))
        nxt = cur.nextInFocusChain()
        while nxt not in foc and nxt is not start:
            nxt = nxt.nextInFocusChain()
        if nxt is start:
            break
        cur = nxt
    check("Tab goes through the fields in page order", pages == sorted(pages)
          and len(pages) == len(foc), f"({len(pages)}/{len(foc)})")


def test_clear_value(root) -> None:
    from viewer.document import PdfDocument
    pd = PdfDocument()
    pd.open(FORM)
    xref = next(x.xref for x in pd._doc[0].widgets() if x.field_name == "FRM6010-01-008")
    pd.set_field_value(0, xref, "東京", "FRM6010-01-008")
    pd.set_field_value(0, xref, "", "FRM6010-01-008")
    out = os.path.join(root, "cleared.pdf")
    pd.save_as(out)
    pd.close()
    with fitz.open(out) as d:
        v = next(x.field_value for x in d[0].widgets() if x.field_name == "FRM6010-01-008")
    check("a field whose text was all deleted is saved empty", v in ("", None), repr(v))


def test_readonly_by_js(app, w) -> None:
    from viewer.page_view import _GovFormClickOverlay
    pv = w.page_view
    pv.set_page(0)
    pump(app, 0.3)
    doc = pv._doc

    def ov(name):
        return next((c for c in pv._form_overlays if c.property("trv_field") == name), None)

    def disp(name):
        return next(x.field_display for x in doc._doc[0].widgets() if x.field_name == name)

    bank = ov("FRM6010-01-015")          # 金融機関名称
    combo = ov("FRM6010-01-024")         # 公金受取口座の利用について
    off = next(x for x in doc._doc[0].widgets() if x.field_name == "FRM6010-01-016_銀行_OFF")
    lx = doc.pdf_rect_to_label(0, off.rect, pv.zoom)[0]
    cell = next(c for c in pv._form_overlays if isinstance(c, _GovFormClickOverlay)
                and abs(c.geometry().x() - lx) < 2)
    check("account fields start read-only (as the form sets them)",
          bank.isReadOnly() and not (bank.focusPolicy() & Qt.FocusPolicy.TabFocus))
    pv.ensureWidgetVisible(cell)
    pump(app, 0.1)
    QTest.mouseClick(cell, Qt.MouseButton.LeftButton)
    pump(app, 0.1)
    check("a locked 〇 choice (bank type) cannot be selected",
          disp("FRM6010-01-016_銀行_ON") == 1)

    combo.setCurrentIndex(combo.findText(" 2"))
    pump(app, 0.4)
    check("choosing '2' unlocks the account fields", not bank.isReadOnly()
          and bool(bank.focusPolicy() & Qt.FocusPolicy.TabFocus))
    check("…and shows the form's message", "振込先" in w.statusBar().currentMessage(),
          w.statusBar().currentMessage())
    QTest.mouseClick(cell, Qt.MouseButton.LeftButton)
    pump(app, 0.1)
    check("…and the bank type can now be selected", disp("FRM6010-01-016_銀行_ON") == 0)

    bank.setPlainText("みずほ")
    bank.committed.emit()
    pump(app, 0.1)
    combo.setCurrentIndex(combo.findText(" 1"))
    pump(app, 0.4)
    check("changing to '1' clears the account and locks it again",
          bank.toPlainText() == "" and bank.isReadOnly()
          and disp("FRM6010-01-016_銀行_ON") == 1)

    combo.setCurrentIndex(combo.findText(" 2"))
    pump(app, 0.3)
    bank.setPlainText("みずほ")
    bank.committed.emit()
    pump(app, 0.1)
    combo.setCurrentIndex(combo.findText(" "))
    pump(app, 0.4)
    status = w.statusBar().currentMessage()
    check("blanking it while an account is entered warns and goes back to '2'",
          combo.currentText().strip() == "2" and not bank.isReadOnly()
          and "クリア" in status, status)
    boxes = [b for b in QApplication.topLevelWidgets()
             if isinstance(b, QMessageBox) and b.isVisible()]
    check("…without a dialog that takes the keyboard away", not boxes)
    printed = next(x.field_value for x in doc._doc[0].widgets()
                   if x.field_name == "FRM6010-01-024_PRINT")
    check("…and the printed twin follows the restored value", str(printed).strip() == "2",
          repr(printed))


def test_clear_form(app, w) -> None:
    pv = w.page_view
    doc = pv._doc

    def snap(d):
        return {(p.number, x.xref): (str(x.field_value or "").replace("Off", ""),
                                     x.field_display, x.field_flags & 1)
                for p in d for x in p.widgets()}

    with fitz.open(FORM) as d:
        pristine = snap(d)
    filled = snap(doc._doc)
    check("setup: the form has been filled in by the earlier steps",
          sum(1 for k in pristine if pristine[k] != filled.get(k)) > 5)
    check("'clear form' is available for a form", w.act_clear_form.isEnabled())
    orig = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    try:
        w.act_clear_form.trigger()
    finally:
        QMessageBox.question = orig
    pump(app, 1.5)
    doc = w.page_view._doc
    after = snap(doc._doc)
    bad = [k for k in pristine if pristine[k] != after.get(k)]
    check("clearing the form brings back the state it was handed out in",
          not bad, f"({len(bad)} differ: {[(k, pristine[k], after.get(k)) for k in bad][:3]})")
    bank = next(c for c in w.page_view._form_overlays
                if c.property("trv_field") == "FRM6010-01-015")
    check("…including the on-screen fields (account locked again, empty)",
          bank.isReadOnly() and bank.toPlainText() == "")
    w._undo_annot()
    pump(app, 1.5)
    back = snap(w.page_view._doc._doc)
    check("undo brings the entries back", back == filled,
          f"({sum(1 for k in filled if filled[k] != back.get(k))} differ)")


def test_history(app, w) -> None:
    from PySide6.QtGui import QInputMethodEvent

    from viewer import form_history
    path = form_history.history_path()
    check("the input history of a test run is not the user's own",
          "trivreader" not in path.lower(), path)
    form_history.history().clear()
    w.activateWindow()   # 前のテストの警告を閉じたあと、入力できるように
    pv = w.page_view
    pv.set_page(0)
    pump(app, 0.3)

    def ov(name):
        return next(c for c in pv._form_overlays if c.property("trv_field") == name)

    def type_in(ctl, text):
        pv.ensureWidgetVisible(ctl)
        ctl.setFocus()
        pump(app, 0.05)
        ctl.selectAll()
        QTest.keyClick(ctl, Qt.Key.Key_Delete)
        ev = QInputMethodEvent("", [])
        ev.setCommitString(text)
        app.sendEvent(ctl, ev)
        key(ctl, Qt.Key.Key_Return)
        key(ctl, Qt.Key.Key_Return)   # 一覧が開いていたら 1 回目はそれを閉じる
        pump(app, 0.05)

    type_in(ov("FRM6010-01-008"), "山田太郎")          # 氏名
    type_in(ov("FRM6010-01-021"), "1234")              # 個人番号（マイナンバー）１
    type_in(ov("FRM6010-01-020"), "7654321")           # 口座番号（入力できない状態）
    values = [e[0] for e in form_history.history()._load()]
    check("a typed name is remembered", "山田太郎" in values, str(values))
    check("My Number and bank account fields are not remembered",
          "1234" not in values and "7654321" not in values)
    check("a 12-digit number is never remembered",
          not form_history.history().add("1234 5678 9012", "備考"))

    other = ov("FRM6010-02-001")                       # 被保険者氏名（2 ページ目）
    pv.ensureWidgetVisible(other)
    other.setFocus()
    pump(app, 0.1)
    comp = other.completer()
    ev = QInputMethodEvent("", [])
    ev.setCommitString("山")
    app.sendEvent(other, ev)
    pump(app, 0.2)
    check("the name is suggested in another name field",
          comp is not None and comp.completionCount() >= 1
          and comp.currentCompletion() == "山田太郎",
          f"({comp and comp.model().stringList()}, focus={other.hasFocus()}, "
          f"text={other.text()!r}, n={comp and comp.completionCount()})")
    if comp is not None and comp.popup().isVisible():
        QTest.keyClick(comp.popup(), Qt.Key.Key_Escape)   # 一覧を閉じる
        pump(app, 0.1)
    other.setText("")
    pump(app, 0.05)

    form_history.set_enabled(False)
    try:
        other.clearFocus()
        pump(app, 0.05)
        other.setFocus()
        pump(app, 0.05)
        comp = other.completer()
        check("with the setting off, nothing is suggested",
              comp is None or not comp.model().stringList())
    finally:
        form_history.set_enabled(True)
    form_history.history().clear()
    check("the history can be cleared", form_history.history().count() == 0
          and not os.path.exists(path))


def test_checks(app, w) -> None:
    from PySide6.QtGui import QInputMethodEvent
    pv = w.page_view
    w.activateWindow()
    pv.set_page(0)
    pump(app, 0.3)

    def ov(name):
        return next(c for c in pv._form_overlays if c.property("trv_field") == name)

    def enter(ctl, text, ime=False):
        pv.ensureWidgetVisible(ctl)
        ctl.setFocus()
        pump(app, 0.05)
        ctl.selectAll()
        QTest.keyClick(ctl, Qt.Key.Key_Delete)
        if ime:
            ev = QInputMethodEvent("", [])
            ev.setCommitString(text)
            app.sendEvent(ctl, ev)
        else:
            QTest.keyClicks(ctl, text)
        pump(app, 0.05)
        key(ctl, Qt.Key.Key_Tab)
        pump(app, 0.4)

    def value(name):
        return next(x.field_value for x in pv._doc._doc[0].widgets() if x.field_name == name)

    year, era = ov("FRM6010-01-004"), ov("FRM6010-01-003")   # 生年月日（年）・元号
    era.setCurrentIndex(0)
    pump(app, 0.3)
    enter(year, "5")
    check("a one-digit year becomes two digits (5 -> 05)",
          year.text() == "05" and value("FRM6010-01-004") == "05",
          f"({year.text()!r}, {value('FRM6010-01-004')!r})")
    check("…and the form's check points at the era that was not chosen",
          bool(era.property("trv_req")) and "元号" in w.statusBar().currentMessage(),
          w.statusBar().currentMessage())
    era.setCurrentIndex(1)
    pump(app, 0.4)
    check("choosing the era clears its mark", not era.property("trv_req"))
    enter(year, "7")
    check("…and the year is still padded once the era is chosen (the form itself does not)",
          year.text() == "07" and value("FRM6010-01-004") == "07")
    enter(year, "00")
    check("an out-of-range year is marked, with the form's message as the field's hint",
          bool(year.property("trv_req")) and "2桁" in year.toolTip(), year.toolTip())
    enter(year, "63")
    check("fixing it removes the mark", not year.property("trv_req"))
    # 欄にいるまま保存しても、年は 2 桁にそろうが、打ちかけで誤りの印は付かない
    pv.ensureWidgetVisible(year)
    year.setFocus()
    pump(app, 0.05)
    year.selectAll()
    QTest.keyClick(year, Qt.Key.Key_Delete)
    QTest.keyClicks(year, "8")
    pv.finish_text_edit()
    pump(app, 0.3)
    check("saving with the cursor in the year pads it without flagging anything",
          value("FRM6010-01-004") == "08" and not year.property("trv_req"),
          f"({value('FRM6010-01-004')!r}, {year.property('trv_req')})")
    key(year, Qt.Key.Key_Tab)
    pump(app, 0.3)

    name = ov("FRM6010-01-008")                               # 氏名（必須）
    enter(name, "山田", ime=True)
    enter(name, "")
    check("leaving a required field empty is marked",
          bool(name.property("trv_req")) and "必須" in w.statusBar().currentMessage())
    pv.set_zoom(pv.zoom * 1.1)
    pv._overlay_rebuild_timer.timeout.emit()
    pump(app, 1.5)
    name = ov("FRM6010-01-008")
    check("the mark and its message survive zooming",
          bool(name.property("trv_req")) and "必須" in name.toolTip())
    enter(name, "山田", ime=True)
    check("…and go away once it is filled in", not name.property("trv_req"),
          f"(text={name.text()!r}, value={value('FRM6010-01-008')!r})")

    kana = ov("FRM6010-01-007")                               # 氏名（カタカナ）: 半角カナだけ
    enter(kana, "やまだ　たろう", ime=True)
    check("a half-width-kana field takes hiragana / full-width kana as half-width",
          kana.text() == "ﾔﾏﾀﾞ ﾀﾛｳ" and not kana.property("trv_req"), repr(kana.text()))


def main() -> int:
    app = QApplication(sys.argv[:1])
    theme.apply_theme(app, dark=False)
    from viewer.main_window import MainWindow
    root = tempfile.mkdtemp(prefix="trv_formlogic_")
    w = MainWindow()
    w.resize(1100, 820)
    w.show()
    app.processEvents()
    try:
        test_fast_widgets()
        test_clear_value(root)
        test_open_and_tab(app, w)
        test_readonly_by_js(app, w)
        test_checks(app, w)
        test_clear_form(app, w)
        test_history(app, w)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====", flush=True)
    os._exit(0 if ok == len(results) else 1)   # 未保存の確認を出さずに終える


if __name__ == "__main__":
    main()
