"""ページの入力欄（Widget）を速く読む。

PyMuPDF の ``page.widgets()`` は、欄ごとにページの先頭から目的の欄を探し直す
（``load_widget`` → ``JM_get_widget_by_xref``）。欄が数百あるページ（官公庁の
勤務状況の表は 1 ページに 319 欄）では欄数の二乗になり、1 回読むだけで 0.3 秒
かかっていた。ここでは MuPDF の欄の並びを 1 回だけたどり、同じ Widget を作る。

PyMuPDF の内部（``_as_pdf_page`` / ``TOOLS._fill_widget``）を使うので、版が
変わって使えなければ ``page.widgets()`` に戻る（遅いだけで結果は同じ）。
"""

from __future__ import annotations

import weakref

import fitz


def widgets(page: "fitz.Page") -> list:
    """``list(page.widgets())`` と同じものを返す（同じ順・同じ Widget）。"""
    try:
        return _widgets_one_pass(page)
    except Exception:  # noqa: BLE001  PyMuPDF の内部が変わったとき
        return list(page.widgets() or [])


def _widgets_one_pass(page: "fitz.Page") -> list:
    mupdf = fitz.mupdf
    pdf_page = fitz._as_pdf_page(page.this)
    out = []
    annot = mupdf.pdf_first_widget(pdf_page)
    while annot.m_internal:
        nxt = mupdf.pdf_next_widget(annot)
        a = fitz.Annot(annot)
        a.thisown = True
        a.parent = weakref.proxy(page)
        page._annot_refs[id(a)] = a
        w = fitz.Widget()
        fitz.TOOLS._fill_widget(a, w)
        out.append(w)
        annot = nxt
    return out


def widget(page: "fitz.Page", xref: int):
    """xref の欄（このページに無ければ None）。全欄を作らずに済む。

    Widget はページを弱参照で持つので、書き換え（update）が終わるまで呼び出し側
    で ``page`` を持っておくこと（``widget(doc.load_page(i), x)`` は不可）。
    """
    if not any(x == xref and t == fitz.PDF_ANNOT_WIDGET
               for x, t, _id in page.annot_xrefs()):
        return None
    try:
        return page.load_widget(xref)
    except Exception:  # noqa: BLE001
        return None
