"""MuPDF が書き出したページの SVG を、QPainter の図形として描く（印刷用）。

印刷が Acrobat より明らかに重かった原因。以前は SVG を QSvgRenderer で描いて
いたが、Windows の印刷経路はそれを図形として送れず、600dpi のフルカラー画像に
して送っていた（文字だけの A4 でも 1 ページ 2,500 万画素・約 1.8 秒）。

ここでは MuPDF の SVG（text_as_path=True。使う要素は path / use / g / clipPath /
image だけ）を自前で解釈し、透明を使わない普通の図形として描く。Windows の
印刷経路はこれを図形のまま送るので、文字はプリンターの解像度でくっきり出て、
データも小さく速い（Acrobat と同じやり方）。

知らない要素・半透明・重ね方（ハイライトの乗算）・グラデーション等が出てきたら
何も描かずに False を返す。呼び出し側はそのページを画像で刷る（見た目を崩さない）。
"""
from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QTransform

_SVG = "{http://www.w3.org/2000/svg}"
_XLINK = "{http://www.w3.org/1999/xlink}href"
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtZzAa]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_CAPS = {"butt": Qt.PenCapStyle.FlatCap, "round": Qt.PenCapStyle.RoundCap,
         "square": Qt.PenCapStyle.SquareCap}
_JOINS = {"miter": Qt.PenJoinStyle.SvgMiterJoin, "round": Qt.PenJoinStyle.RoundJoin,
          "bevel": Qt.PenJoinStyle.BevelJoin}
# 透明や合成を使う属性（あればそのページは画像で刷る）。opacity 類は 1 なら可
_UNSUPPORTED_ATTRS = ("opacity", "fill-opacity", "stroke-opacity", "mask", "filter",
                      "style")
_IGNORED_TAGS = ("defs", "clipPath", "title", "desc", "metadata")


class Unsupported(Exception):
    """この SVG は自前では描けない（画像で刷る）。"""


def parse_path(d: str) -> QPainterPath:
    """SVG の d 属性を QPainterPath にする（円弧 A は MuPDF が使わないので非対応）。"""
    path = QPainterPath()
    tokens = _TOKEN.findall(d or "")
    n = len(tokens)
    i, cmd = 0, None
    cx = cy = sx = sy = 0.0
    last_ctrl = None

    def num() -> float:
        nonlocal i
        if i >= n or tokens[i].isalpha():
            raise Unsupported("path numbers")
        v = float(tokens[i])
        i += 1
        return v

    while i < n:
        t = tokens[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in "Zz":
                path.closeSubpath()
                cx, cy = sx, sy
                last_ctrl = None
                continue
        elif cmd is None:
            raise Unsupported("path")
        rel = cmd.islower()
        c = cmd.upper()
        ox, oy = (cx, cy) if rel else (0.0, 0.0)
        if c == "M":
            cx, cy = num() + ox, num() + oy
            path.moveTo(cx, cy)
            sx, sy = cx, cy
            cmd = "l" if rel else "L"      # 続く座標は直線
            last_ctrl = None
        elif c == "L":
            cx, cy = num() + ox, num() + oy
            path.lineTo(cx, cy)
            last_ctrl = None
        elif c == "H":
            cx = num() + ox
            path.lineTo(cx, cy)
            last_ctrl = None
        elif c == "V":
            cy = num() + oy
            path.lineTo(cx, cy)
            last_ctrl = None
        elif c == "C":
            x1, y1 = num() + ox, num() + oy
            x2, y2 = num() + ox, num() + oy
            cx, cy = num() + ox, num() + oy
            path.cubicTo(x1, y1, x2, y2, cx, cy)
            last_ctrl = (x2, y2)
        elif c == "S":
            x1, y1 = (2 * cx - last_ctrl[0], 2 * cy - last_ctrl[1]) if last_ctrl else (cx, cy)
            x2, y2 = num() + ox, num() + oy
            cx, cy = num() + ox, num() + oy
            path.cubicTo(x1, y1, x2, y2, cx, cy)
            last_ctrl = (x2, y2)
        elif c == "Q":
            x1, y1 = num() + ox, num() + oy
            cx, cy = num() + ox, num() + oy
            path.quadTo(x1, y1, cx, cy)
            last_ctrl = None
        else:
            raise Unsupported(f"path command {cmd}")
    return path


def parse_transform(text: str | None) -> QTransform:
    """transform 属性（matrix / translate / scale。MuPDF は matrix を使う）。"""
    out = QTransform()
    if not text:
        return out
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text):
        v = [float(x) for x in _NUM.findall(args)]
        if name == "matrix" and len(v) == 6:
            t = QTransform(v[0], v[1], v[2], v[3], v[4], v[5])
        elif name == "translate" and v:
            t = QTransform.fromTranslate(v[0], v[1] if len(v) > 1 else 0.0)
        elif name == "scale" and v:
            t = QTransform.fromScale(v[0], v[1] if len(v) > 1 else v[0])
        else:
            raise Unsupported(f"transform {name}")
        out = t * out   # 書いた順に左から掛かる（右に書いたものが先に効く）
    return out


def parse_color(text: str | None):
    """塗り・線の色。"#rrggbb" → QColor、"none" → None、省略 → 黒（SVG の既定）。"""
    if text is None:
        return QColor(0, 0, 0)
    text = text.strip()
    if text == "none":
        return None
    if text.startswith("url("):
        raise Unsupported("paint server")   # 模様・グラデーション
    c = QColor(text)
    if not c.isValid():
        raise Unsupported(f"color {text}")
    return c


class _Renderer:
    """p=None で呼ぶと「描けるか」だけを確かめる（何も描かない）。"""

    def __init__(self, root: ET.Element) -> None:
        self.ids: dict[str, ET.Element] = {}
        for el in root.iter():
            ident = el.get("id")
            if ident:
                self.ids[ident] = el
        self._paths: dict[int, QPainterPath] = {}
        self._images: dict[int, QImage] = {}

    def path_of(self, el: ET.Element) -> QPainterPath:
        key = id(el)
        p = self._paths.get(key)
        if p is None:
            p = parse_path(el.get("d", ""))
            p.setFillRule(Qt.FillRule.OddEvenFill if el.get("fill-rule") == "evenodd"
                          else Qt.FillRule.WindingFill)
            self._paths[key] = p
        return p

    @staticmethod
    def check_attrs(el: ET.Element) -> None:
        for a in _UNSUPPORTED_ATTRS:
            v = el.get(a)
            if v is None:
                continue
            if a.endswith("opacity"):
                try:
                    if float(v) >= 1.0:
                        continue
                except ValueError:
                    pass
            raise Unsupported(a)

    def clip_of(self, ref: str, xf: QTransform) -> QPainterPath:
        m = re.match(r"url\(#([^)]+)\)", ref or "")
        el = self.ids.get(m.group(1)) if m else None
        if el is None or el.tag != _SVG + "clipPath":
            raise Unsupported("clip-path")
        if el.get("clipPathUnits") not in (None, "userSpaceOnUse"):
            raise Unsupported("clipPathUnits")
        out = None
        for child in el:
            if child.tag != _SVG + "path":
                raise Unsupported(f"clip child {child.tag}")
            shape = (parse_transform(child.get("transform")) * xf).map(self.path_of(child))
            out = shape if out is None else out.united(shape)
        return out if out is not None else QPainterPath()

    def draw(self, p: QPainter | None, el: ET.Element, xf: QTransform) -> None:
        tag = el.tag[len(_SVG):] if el.tag.startswith(_SVG) else el.tag
        if tag in _IGNORED_TAGS:
            return
        self.check_attrs(el)
        if tag in ("svg", "g"):
            xf2 = parse_transform(el.get("transform")) * xf if tag == "g" else xf
            clip_ref = el.get("clip-path")
            clip = self.clip_of(clip_ref, xf2) if clip_ref else None
            if p is not None:
                p.save()
                if clip is not None:
                    p.setTransform(QTransform())   # 切り抜きは描く先の座標で渡す
                    p.setClipPath(clip, Qt.ClipOperation.IntersectClip)
            for child in el:
                self.draw(p, child, xf2)
            if p is not None:
                p.restore()
        elif tag == "path":
            self.paint(p, el, el, self.path_of(el), parse_transform(el.get("transform")) * xf)
        elif tag == "use":
            ref = (el.get(_XLINK) or el.get("href") or "").lstrip("#")
            target = self.ids.get(ref)
            if target is None or target.tag != _SVG + "path":
                raise Unsupported("use target")
            self.check_attrs(target)
            xf2 = (parse_transform(target.get("transform"))
                   * parse_transform(el.get("transform")) * xf)
            self.paint(p, el, target, self.path_of(target), xf2)
        elif tag == "image":
            img = self.image_of(el)
            if p is not None:
                p.setTransform(parse_transform(el.get("transform")) * xf)
                p.drawImage(QRectF(0, 0, float(el.get("width", img.width())),
                                   float(el.get("height", img.height()))), img)
        else:
            raise Unsupported(tag)

    @staticmethod
    def _attr(use: ET.Element, target: ET.Element, name: str):
        """use の指定を優先し、無ければ参照先の指定（SVG の継承と同じ向き）。"""
        v = use.get(name)
        return v if v is not None else target.get(name)

    def paint(self, p, use: ET.Element, target: ET.Element, path: QPainterPath,
              xf: QTransform) -> None:
        fill = parse_color(self._attr(use, target, "fill"))
        stroke_attr = self._attr(use, target, "stroke")
        pen = None
        if stroke_attr not in (None, "none"):
            color = parse_color(stroke_attr)
            pen = QPen(color)
            width = float(self._attr(use, target, "stroke-width") or 1)
            pen.setWidthF(width)
            pen.setCapStyle(_CAPS.get(self._attr(use, target, "stroke-linecap") or "butt",
                                      Qt.PenCapStyle.FlatCap))
            pen.setJoinStyle(_JOINS.get(self._attr(use, target, "stroke-linejoin") or "miter",
                                        Qt.PenJoinStyle.SvgMiterJoin))
            pen.setMiterLimit(float(self._attr(use, target, "stroke-miterlimit") or 4))
            dash = self._attr(use, target, "stroke-dasharray")
            if dash and dash != "none":
                values = [float(x) for x in _NUM.findall(dash)]
                if values and any(values):
                    if len(values) % 2:
                        values *= 2
                    w = width or 1.0
                    # Qt の破線は線幅に対する比で指定する
                    pen.setDashPattern([max(v / w, 0.01) for v in values])
                    pen.setDashOffset(float(self._attr(use, target, "stroke-dashoffset")
                                            or 0) / w)
        if p is None:
            return
        p.setTransform(xf)
        if fill is not None:
            p.fillPath(path, fill)
        if pen is not None:
            p.strokePath(path, pen)

    def image_of(self, el: ET.Element) -> QImage:
        img = self._images.get(id(el))
        if img is not None:
            return img
        href = el.get(_XLINK) or el.get("href") or ""
        m = re.match(r"data:image/(png|jpeg|jpg);base64,(.*)", href, re.S)
        if not m:
            raise Unsupported("image href")
        img = QImage()
        if not img.loadFromData(base64.b64decode(m.group(2))):
            raise Unsupported("image data")
        if img.hasAlphaChannel():
            raise Unsupported("image alpha")   # 透過のある画像は画像として刷る
        self._images[id(el)] = img
        return img


def draw_svg(painter: QPainter, svg: str, target: QRectF) -> bool:
    """svg を target（painter の今の座標で）いっぱいに描く。

    描けなければ何も描かずに False を返す。途中で非対応が見つかっても紙を
    汚さないよう、先に全体を確かめてから描く（解釈した図形は使い回す）。
    """
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return False
    vb = [float(x) for x in _NUM.findall(root.get("viewBox", ""))]
    if len(vb) != 4 or vb[2] <= 0 or vb[3] <= 0:
        return False
    r = _Renderer(root)
    base = (QTransform.fromTranslate(-vb[0], -vb[1])
            * QTransform.fromScale(target.width() / vb[2], target.height() / vb[3])
            * QTransform.fromTranslate(target.left(), target.top())
            * painter.worldTransform())
    try:
        r.draw(None, root, base)           # 描けるかを先に確かめる
    except (Unsupported, ValueError, IndexError):
        return False
    from PySide6.QtPrintSupport import QPrinter

    painter.save()
    # 紙（プリンター）には滑らか処理をかけない。画面の下見はかける
    painter.setRenderHint(QPainter.RenderHint.Antialiasing,
                          not isinstance(painter.device(), QPrinter))
    try:
        r.draw(painter, root, base)
    finally:
        painter.restore()
    return True
