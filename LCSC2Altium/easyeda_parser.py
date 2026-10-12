"""Decode EasyEDA primitive strings into the typed models used by the builders.

EasyEDA returns geometry as a flat list of pipe-delimited strings. Two grammars appear:

``symbol``   ``<KIND>~<field>~<field>...``  with kinds R, E, P, T, PL, PG, PT, A, AR
``footprint`` ``<KIND>~<field>~<field>...`` with kinds TRACK, PAD, ARC, CIRCLE,
             SOLIDREGION, SVGNODE, RECT, HOLE, TEXT

Fields may embed ``~`` sub-tokens (double tildes ``^^`` separate sub-lists) so a naive
``split("~")`` is not always lossless; :func:`split_fields` keeps the raw parts and the
callers pick what they need.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Iterable

import altium_monkey as am

from .constants import EASYEDA_UNIT_MILS, PIN_ELECTRICAL, snap_to_grid, to_altium_layer
from .easyeda_client import extract_data_str
from .models import (
    ComponentInfo,
    FootprintData,
    FpCircle,
    FpHole,
    FpPad,
    FpRect,
    FpRegion,
    FpText,
    FpTrack,
    Point,
    SchEllipse,
    SchLine,
    SchPin,
    SchPolygon,
    SchPolyline,
    SchRect,
    SchText,
    StepModelRef,
    SymbolData,
    parse_electrical,
    recentre_footprint,
    recentre_symbol,
)

log = logging.getLogger(__name__)

MIL_TO_MM = 1.0 / 39.37007874015748


def _f(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: str | None, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def split_fields(raw: str) -> list[str]:
    return raw.split("~")


def _origin(bbox: Any, head: Any) -> tuple[float, float]:
    """Pick the centre of the drawing so the Altium symbol/footprint sits on 0,0.

    ``BBox`` is preferred; when it is missing or degenerate the editor's own origin
    (``head.x`` / ``head.y``) is used instead.
    """
    if isinstance(bbox, dict):
        x = _f(bbox.get("x"))
        y = _f(bbox.get("y"))
        w = _f(bbox.get("width"))
        h = _f(bbox.get("height"))
        if w or h:
            return (x + w / 2.0, y + h / 2.0)
    if isinstance(head, dict):
        return (_f(head.get("x")), _f(head.get("y")))
    return (0.0, 0.0)


# --------------------------------------------------------------------------------------
# EasyEDA unit -> Altium mil scaling
# --------------------------------------------------------------------------------------
def scale_symbol(sym: SymbolData, factor: float = EASYEDA_UNIT_MILS) -> SymbolData:
    """Convert symbol geometry from EasyEDA units into Altium mils, in place.

    Pin positions are snapped to :data:`SCHEMATIC_GRID_MILS` afterwards, which is
    why the scaling happens first: the grid is expressed in mils, and snapping the
    raw EasyEDA units would quantise pins onto a 500 mil pitch instead of 50.
    """
    for pin in sym.pins:
        pin.x = snap_to_grid(pin.x * factor)
        pin.y = snap_to_grid(pin.y * factor)
        pin.length *= factor
    for line in sym.lines:
        line.x1 *= factor
        line.y1 *= factor
        line.x2 *= factor
        line.y2 *= factor
    for rect in sym.rectangles:
        rect.x1 *= factor
        rect.y1 *= factor
        rect.x2 *= factor
        rect.y2 *= factor
    for poly in sym.polygons:
        poly.points = [(x * factor, y * factor) for x, y in poly.points]
    for line in sym.polylines:
        line.points = [(x * factor, y * factor) for x, y in line.points]
    for ell in sym.ellipses:
        ell.cx *= factor
        ell.cy *= factor
        ell.rx *= factor
        ell.ry *= factor
    for arc in sym.arcs:
        arc.cx *= factor
        arc.cy *= factor
        arc.radius *= factor
    for text in sym.texts:
        text.x *= factor
        text.y *= factor
    return sym


def scale_footprint(fp: FootprintData, factor: float = EASYEDA_UNIT_MILS,
                    mirror_y: bool = True) -> FootprintData:
    """Convert footprint geometry from EasyEDA units into Altium mils, in place.

    ``mirror_y`` flips the Y axis. EasyEDA footprints are Y-down while an Altium
    ``PcbLib`` is Y-up, so without the flip every footprint arrives mirrored: pin 1
    ends up on the wrong corner and text reads upside down. Rotations follow the same
    reflection -- angles about X and Z reverse, angles about Y do not.
    """
    sign = -1.0 if mirror_y else 1.0

    for pad in fp.pads:
        pad.x *= factor
        pad.y *= factor * sign
        pad.rotation_degrees *= sign
        pad.width *= factor
        pad.height *= factor
        pad.hole *= factor
        pad.drill_offset = (pad.drill_offset[0] * factor, pad.drill_offset[1] * factor * sign)
        if pad.polygon:
            pad.polygon = [(x * factor, y * factor * sign) for x, y in pad.polygon]
        if pad.slot_points:
            # Mirroring Y reverses the sense of the slot angle, which the builder
            # recomputes from the endpoints, so the points only need scaling.
            pad.slot_points = [(x * factor, y * factor * sign) for x, y in pad.slot_points]
    for track in fp.tracks:
        track.points = [(x * factor, y * factor * sign) for x, y in track.points]
        track.width *= factor
    for arc in fp.arcs:
        arc.cx *= factor
        arc.cy *= factor * sign
        arc.radius *= factor
        arc.width *= factor
        arc.start_angle *= sign
        arc.end_angle *= sign
    for rect in fp.rects:
        # Move the anchor to the mirrored top edge so the (x, y) + (dx, dy) corner
        # walk in the builder still traces the same rectangle.
        rect.y = (rect.y + rect.dy) * factor * sign
        rect.x *= factor
        rect.dx *= factor
        rect.dy *= factor
        rect.width *= factor
    for circle in fp.circles:
        circle.cx *= factor
        circle.cy *= factor * sign
        circle.radius *= factor
        circle.width *= factor
    for region in fp.regions:
        region.points = [(x * factor, y * factor * sign) for x, y in region.points]
    for hole in fp.holes:
        hole.x *= factor
        hole.y *= factor * sign
        hole.radius *= factor
    for text in fp.texts:
        text.x *= factor
        text.y *= factor * sign
        text.rotation *= sign
        text.height *= factor
    if fp.step is not None:
        fp.step.x *= factor
        fp.step.y *= factor * sign
        fp.step.z *= factor
        fp.step.rotation_x *= sign
        # rotation_y is unaffected by a Y mirror; rotation_z reverses.
        fp.step.rotation_z *= sign
        fp.step.width *= factor
        fp.step.height *= factor
    return fp


# ======================================================================================
# SVG path helpers
# ======================================================================================
_PATH_CMD_RE = re.compile(r"([MmLlAaZzHhVvCc])\s*([-+0-9.eE,\s]*)")
_NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _numbers(text: str) -> list[float]:
    return [float(n) for n in _NUM_RE.findall(text)]


def _flatten_svg_path(path: str, relative_origin: Point = (0.0, 0.0)) -> list[Point]:
    """Convert an SVG path into a polyline point list.

    Only the commands EasyEDA emits are supported (``M``, ``L``, ``A``, ``Z``, ``C``);
    curves are flattened by sampling. Arcs follow the SVG endpoint parameterisation.
    """
    ox, oy = relative_origin
    points: list[Point] = []
    cur = (0.0, 0.0)
    start = (0.0, 0.0)

    def push(p: Point) -> None:
        if not points or points[-1] != p:
            points.append(p)

    for match in _PATH_CMD_RE.finditer(path):
        cmd = match.group(1)
        args = _numbers(match.group(2))
        if cmd in "Mm":
            if len(args) >= 2:
                cur = (args[0], args[1])
                start = cur
                push((cur[0] - ox, cur[1] - oy))
        elif cmd in "Ll":
            for i in range(0, len(args) - 1, 2):
                cur = (args[i], args[i + 1])
                push((cur[0] - ox, cur[1] - oy))
        elif cmd in "HhVv":
            if cmd == "H":
                for value in args:
                    cur = (value, cur[1])
                    push((cur[0] - ox, cur[1] - oy))
            else:
                for value in args:
                    cur = (cur[0], value)
                    push((cur[0] - ox, cur[1] - oy))
        elif cmd in "Aa":
            for i in range(0, len(args) - 6, 7):
                rx, ry, rot, large, sweep, ex, ey = args[i:i + 7]
                pts = _arc_to_points(cur, (ex, ey), rx, ry, rot, int(large), int(sweep))
                for p in pts:
                    push((p[0] - ox, p[1] - oy))
                cur = (ex, ey)
        elif cmd in "Cc":
            # Flatten cubic beziers with a fixed subdivision (EasyEDA only uses these
            # for decorative outlines).
            if len(args) >= 6:
                c1 = (args[0], args[1])
                c2 = (args[2], args[3])
                end = (args[4], args[5])
                for step in range(1, 9):
                    t = step / 8.0
                    p = _bezier(cur, c1, c2, end, t)
                    push((p[0] - ox, p[1] - oy))
                cur = end
        elif cmd in "Zz":
            cur = start
            push((cur[0] - ox, cur[1] - oy))

    return points


def _bezier(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> Point:
    u = 1.0 - t
    x = u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0]
    y = u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]
    return (x, y)


def _arc_to_points(
    start: Point, end: Point, rx: float, ry: float, rotation: float,
    large_arc: int, sweep: int,
) -> list[Point]:
    """SVG arc endpoint -> centre parameterisation, sampled into points."""
    x1, y1 = start
    x2, y2 = end
    if abs(x1 - x2) < 1e-9 and abs(y1 - y2) < 1e-9:
        return []
    rx, ry = abs(rx), abs(ry)
    if rx < 1e-9 or ry < 1e-9:
        return [end]

    phi = math.radians(rotation % 360.0)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)

    dx2, dy2 = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cos_phi * dx2 + sin_phi * dy2
    y1p = -sin_phi * dx2 + cos_phi * dy2

    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1.0:
        scale = math.sqrt(lam)
        rx *= scale
        ry *= scale

    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    if abs(den) < 1e-12:
        return [end]
    coef = math.sqrt(max(0.0, num / den))
    if large_arc == sweep:
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx

    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2.0
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2.0

    def angle(ux: float, uy: float, vx: float, vy: float) -> float:
        norm = math.hypot(ux, uy) * math.hypot(vx, vy)
        if norm < 1e-12:
            return 0.0
        cos_a = max(-1.0, min(1.0, (ux * vx + uy * vy) / norm))
        a = math.acos(cos_a)
        return -a if (ux * vy - uy * vx) < 0 else a

    theta = angle(1.0, 0.0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    delta = angle((x1p - cxp) / rx, (y1p - cyp) / ry,
                  (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if sweep == 0 and delta > 0:
        delta -= 2 * math.pi
    elif sweep == 1 and delta < 0:
        delta += 2 * math.pi

    steps = max(6, min(64, int(abs(delta) / (2 * math.pi) * 48)))
    out: list[Point] = []
    for i in range(1, steps + 1):
        t = theta + delta * i / steps
        px = cx + rx * math.cos(t) * cos_phi - ry * math.sin(t) * sin_phi
        py = cy + rx * math.cos(t) * sin_phi + ry * math.sin(t) * cos_phi
        out.append((px, py))
    return out


# ======================================================================================
# Component metadata
# ======================================================================================
def parse_component_info(result: dict) -> ComponentInfo:
    """Extract human-facing metadata from an EasyEDA ``result`` mapping."""
    lcsc = str(result.get("szlcsc", {}).get("number") or result.get("lcsc", {}).get("number") or "")
    data = extract_data_str(result.get("dataStr"))
    head = data.get("head", {}) if isinstance(data, dict) else {}
    c_para = head.get("c_para", {}) if isinstance(head, dict) else {}

    def para(*names: str) -> str:
        for name in names:
            value = c_para.get(name)
            if value:
                return str(value)
        return ""

    info = ComponentInfo(
        lcsc=lcsc,
        mpn=para("Manufacturer Part", "name") or str(result.get("title") or ""),
        manufacturer=para("Manufacturer"),
        description=str(result.get("description") or ""),
        package=para("package"),
        value=para("Value", "nameAlias"),
        category=para("JLCPCB Part Class"),
    )

    search = result.get("__search__")
    if isinstance(search, dict):
        info.manufacturer = str(search.get("manufacturer") or info.manufacturer)
        info.package = str(search.get("package") or info.package)
        info.stock = _i(search.get("stock"), 0)
        prices = search.get("price")
        if isinstance(prices, list) and prices and isinstance(prices[0], list):
            info.price = f"{prices[0][1]} USD / {prices[0][0]}"
    return info


# ======================================================================================
# Schematic symbol
# ======================================================================================
_ROTATION_MAP = {0: am.Rotation90.DEG_0, 90: am.Rotation90.DEG_90,
                 180: am.Rotation90.DEG_180, 270: am.Rotation90.DEG_270}


def parse_symbol(result: dict, name: str) -> SymbolData:
    """Parse the schematic symbol of *result* into symbol-local Altium mils."""
    data = extract_data_str(result.get("dataStr"))
    head = data.get("head", {}) if isinstance(data, dict) else {}
    c_para = head.get("c_para", {}) if isinstance(head, dict) else {}
    desc = str(c_para.get("Value") or result.get("description") or "")

    symbol = SymbolData(name=name, description=desc)

    # Parse in raw coordinates; the origin is fixed afterwards from the geometry
    # itself. EasyEDA's BBox describes the canvas rather than the drawing on some
    # packages (the USB-C symbol declares 92 x 130 while its pins span 700 units),
    # which is what pushed the component off centre before.
    for raw in data.get("shape", []) or []:
        if not isinstance(raw, str) or not raw:
            continue
        kind, _, tail = raw.partition("~")
        f = split_fields(tail)
        try:
            handler = _SYMBOL_HANDLERS.get(kind)
            if handler is not None:
                handler(symbol, f, 0.0, 0.0)
        except (IndexError, ValueError, ZeroDivisionError) as exc:
            log.debug("skipped symbol primitive %s: %s", kind, exc)

    recentre_symbol(symbol)
    return scale_symbol(symbol)


def _h_pin(sym: SymbolData, f: list[str], ox: float, oy: float) -> None:
    # f[1]=electrical f[2]=pin number f[3]=x f[4]=y f[5]=rotation
    # f[8]=pin geometry (carries the length) f[9]=name visibility block
    # f[13]=pin name f[17]=number visibility block f[21]=rendered pin number
    designator = (f[21] if len(f) > 21 and f[21] else (f[2] if len(f) > 2 else "")).strip()
    if not designator:
        return
    name = (f[13] if len(f) > 13 else "").strip()
    x = _f(f[3]) - ox
    y = -(_f(f[4]) - oy)

    # EasyEDA and Altium agree on the rotation value here; the earlier 180 degree
    # offset turned every pin on its head.
    raw_rot = _i(f[5], 0) if len(f) > 5 else 0
    rotation = _ROTATION_MAP.get(raw_rot % 360, am.Rotation90.DEG_0)

    length = 200.0
    if len(f) > 8 and f[8]:
        token = f[8].split("^^")[-1]
        for prefix in ("h", "v"):
            if prefix in token:
                token = token.split(prefix)[-1]
                break
        parsed = abs(_f(token, 0.0))
        if parsed:
            length = parsed

    sym.pins.append(SchPin(
        designator=designator, name=name or designator,
        x=x, y=y,
        rotation=rotation, length=length,
        electrical=parse_electrical(f[1]) if len(f) > 1 else PIN_ELECTRICAL,
        name_visible=_visibility(f[9]) if len(f) > 9 else True,
        designator_visible=_visibility(f[17]) if len(f) > 17 else True,
    ))


def _visibility(field: str) -> bool:
    """EasyEDA stores a multi-value block as ``a~b^^flag~...``; flag 0 means shown."""
    tail = field.split("^^")[-1].split("~")[0]
    return tail not in ("0", "", "false")


def _h_rect(sym: SymbolData, f: list[str], ox: float, oy: float) -> None:
    # 0:x1 1:y1 4:width 5:length 9:fill_color
    x1 = _f(f[0]) - ox
    y1 = -(_f(f[1]) - oy)
    width = _f(f[4]) if len(f) > 4 else 0.0
    length = _f(f[5]) if len(f) > 5 else 0.0
    # Y is negated, so the extent grows downwards in the source frame.
    x2 = x1 + width
    y2 = y1 - length
    fill = (f[9] if len(f) > 9 else "") not in ("", "none")
    sym.rectangles.append(SchRect(x1, y1, x2, y2, filled=fill))


def _h_ellipse(sym: SymbolData, f: list[str], ox: float, oy: float) -> None:
    cx = _f(f[0]) - ox
    cy = -(_f(f[1]) - oy)
    rx = _f(f[2]) if len(f) > 2 else 0.0
    ry = rx
    if len(f) > 3 and f[3]:
        ry = _f(f[3])
    sym.ellipses.append(SchEllipse(cx, cy, rx, ry, filled=True))


def _h_polyline(sym: SymbolData, f: list[str], ox: float, oy: float) -> None:
    nums = _numbers(f[0]) if f else []
    pts = [(_f(nums[i]) - ox, -(_f(nums[i + 1]) - oy)) for i in range(0, len(nums) - 1, 2)]
    if len(pts) >= 2:
        sym.polylines.append(SchPolyline(pts))


def _h_polygon(sym: SymbolData, f: list[str], ox: float, oy: float) -> None:
    nums = _numbers(f[0]) if f else []
    pts = [(_f(nums[i]) - ox, -(_f(nums[i + 1]) - oy)) for i in range(0, len(nums) - 1, 2)]
    if len(pts) >= 3:
        pts.append(pts[0])
        sym.polygons.append(SchPolygon(pts, filled=True))


def _h_triangle(sym: SymbolData, f: list[str], ox: float, oy: float) -> None:
    path = re.sub(r"[MLAZC]", " ", f[0] if f else "")
    nums = _numbers(path)
    pts = [(_f(nums[i]) - ox, -(_f(nums[i + 1]) - oy)) for i in range(0, len(nums) - 1, 2)]
    if len(pts) >= 3:
        pts.append(pts[0])
        sym.polygons.append(SchPolygon(pts, filled=True))


def _h_arc(sym: SymbolData, f: list[str], ox: float, oy: float) -> None:
    path = f[0] if f else ""
    nums = _numbers(path)
    if len(nums) < 7:
        return
    x1, y1, rx, ry, rot, large, sweep, x2, y2 = nums[:9]
    pts = _arc_to_points((x1, y1), (x2, y2), rx, ry, rot, int(large), int(sweep))
    conv = [(p[0] - ox, -(p[1] - oy)) for p in pts]
    if len(conv) >= 2:
        sym.polylines.append(SchPolyline(conv))


def _h_line(sym: SymbolData, f: list[str], ox: float, oy: float) -> None:
    sym.lines.append(SchLine(x1=_f(f[0]) - ox, y1=-(_f(f[1]) - oy),
                              x2=_f(f[2]) - ox, y2=-(_f(f[3]) - oy)))


def _h_text(sym: SymbolData, f: list[str], ox: float, oy: float) -> None:
    if len(f) < 12 or not f[11]:
        return
    sym.texts.append(SchText(
        text=f[11],
        x=_f(f[1]) - ox,
        y=-(_f(f[2]) - oy),
        rotation=_i(f[3], 0) % 360,
    ))


def _h_arrow(sym: SymbolData, f: list[str], ox: float, oy: float) -> None:
    path = re.sub(r"[MLAZC]", " ", f[5] if len(f) > 5 else "")
    nums = _numbers(path)
    pts = [(_f(nums[i]) - ox, -(_f(nums[i + 1]) - oy)) for i in range(0, len(nums) - 1, 2)]
    if len(pts) >= 3:
        pts.append(pts[0])
        sym.polygons.append(SchPolygon(pts, filled=True))


_SYMBOL_HANDLERS = {
    "P": _h_pin,
    "R": _h_rect,
    "E": _h_ellipse,
    "PL": _h_polyline,
    "PG": _h_polygon,
    "PT": _h_triangle,
    "A": _h_arc,
    "AR": _h_arrow,
    "T": _h_text,
    "L": _h_line,
}


# ======================================================================================
# Footprint
# ======================================================================================
def _pxy(x: float, y: float, ox: float, oy: float) -> Point:
    """Footprint coordinates stay in mils and are recentred on the origin.

    Altium PcbLib stores footprint data in a Y-down frame, the same convention the
    EasyEDA source uses, so Y is not negated here.
    """
    return (x - ox, y - oy)


_PAD_SHAPES = {
    "RECT": am.PadShape.RECTANGLE,
    "RECTANGLE": am.PadShape.RECTANGLE,
    "ELLIPSE": am.PadShape.CIRCLE,
    "ROUND": am.PadShape.CIRCLE,
    # Altium has no oval primitive; a 50% rounded rectangle is the stadium shape.
    "OVAL": am.PadShape.ROUNDED_RECTANGLE,
    "POLYGON": am.PadShape.CUSTOM,
}


def parse_footprint(result: dict, name: str) -> FootprintData | None:
    """Parse ``packageDetail`` into a footprint definition."""
    package = result.get("packageDetail")
    if not isinstance(package, dict):
        return None
    data = extract_data_str(package.get("dataStr"))
    if not data or "shape" not in data:
        return None

    head = data.get("head", {}) if isinstance(data, dict) else {}
    c_para = head.get("c_para", {}) if isinstance(head, dict) else {}
    package_name = str(c_para.get("package") or package.get("title") or name)

    fp = FootprintData(name=name, description=f"{package_name} footprint")

    # Like the symbol, the footprint is recentred from its own geometry rather than
    # from BBox, which is the canvas on some packages.
    for raw in data.get("shape", []) or []:
        if not isinstance(raw, str) or not raw:
            continue
        kind, _, tail = raw.partition("~")
        f = split_fields(tail)
        try:
            handler = _FOOTPRINT_HANDLERS.get(kind)
            if handler is not None:
                handler(fp, f, 0.0, 0.0, raw)
        except (IndexError, ValueError, ZeroDivisionError) as exc:
            log.debug("skipped footprint primitive %s: %s", kind, exc)

    recentre_footprint(fp)
    return scale_footprint(fp)


def _h_track(fp: FootprintData, f: list[str], ox: float, oy: float, raw: str) -> None:
    # TRACK~width~layer~net~points~id~locked  (width comes first, unlike the PCB layers
    # of a SchDoc; the raw payload and EasyEDA's own SVG agree on this order)
    if len(f) < 4:
        return
    width = _f(f[0])
    layer = to_altium_layer(f[1])
    if layer in (am.PcbLayer.TOP, am.PcbLayer.BOTTOM):
        # Some packages keep placement helpers (courtyard / body outline) on the copper
        # layers; EasyEDA never draws them, so emitting them would add phantom copper.
        log.debug("skipping helper track on copper layer %s", f[1])
        return
    nums = _numbers(f[3])
    pts = [_pxy(nums[i], nums[i + 1], ox, oy) for i in range(0, len(nums) - 1, 2)]
    if len(pts) >= 2 and width > 0:
        fp.tracks.append(FpTrack(points=pts, width=width, layer=layer))


def _h_pad(fp: FootprintData, f: list[str], ox: float, oy: float, raw: str) -> None:
    # PAD~shape~x~y~width~height~layer~net~number~holeRadius~polygon~rotation~id~
    #     drillOffset~locked~plated~...
    if len(f) < 11:
        return
    shape_name = f[0].upper()
    x, y = _f(f[1]), _f(f[2])
    width, height = _f(f[3]), _f(f[4])
    layer = to_altium_layer(f[5])
    designator = (f[7] if len(f) > 7 else "").strip()
    polygon_raw = f[9] if len(f) > 9 else ""
    rotation = _f(f[10]) if len(f) > 10 else 0.0
    drill_offset = _f(f[12]) if len(f) > 12 else 0.0
    # EasyEDA stores the drill *radius*; Altium wants a diameter.
    hole_diameter = abs(_f(f[8]) if len(f) > 8 else 0.0) * 2.0
    plated = (f[14] if len(f) > 14 else "Y").strip().upper() != "N"

    poly: list[Point] | None = None
    slot: list[Point] | None = None
    if polygon_raw:
        nums = _numbers(polygon_raw)
        points = [_pxy(nums[i], nums[i + 1], x, y) for i in range(0, len(nums) - 1, 2)]
        if shape_name == "POLYGON":
            # The outline is kept exactly as stored: EasyEDA traces it with many
            # duplicate and slightly jittered vertices, and polygon_is_rectangle()
            # tolerates that, while simplifying first would drop real corners.
            if len(points) >= 3:
                poly = points
        elif hole_diameter > 0 and len(points) == 2:
            # Two points plus a drill radius describe a slotted hole: the points are
            # the ends of its axis. A plain round hole carries no polygon at all.
            axis = math.dist(points[0], points[1])
            if axis > 1e-6:
                slot = points

    fp.pads.append(FpPad(
        designator=designator, x=x - ox, y=y - oy,
        width=width, height=height, layer=layer,
        shape=_PAD_SHAPES.get(shape_name, am.PadShape.ROUNDED_RECTANGLE),
        rotation=rotation,
        hole=hole_diameter,
        drill_offset=(0.0, drill_offset),
        polygon=poly,
        plated=plated,
        slot_points=slot,
    ))


def _h_arc(fp: FootprintData, f: list[str], ox: float, oy: float, raw: str) -> None:
    # ARC~width~layer~net~svgpath~helperDots~id~locked
    if len(f) < 4:
        return
    width = _f(f[0])
    layer = to_altium_layer(f[1])
    path = f[3]
    match = re.search(
        r"M\s*([-\d.]+)[\s,]+([-\d.]+)\s*A\s*([-\d.]+)[\s,]+([-\d.]+)[\s,]+([-\d.]+)"
        r"[\s,]+(\d)[\s,]+(\d)[\s,]+([-\d.]+)[\s,]+([-\d.]+)", path)
    if not match:
        return
    x1, y1, rx, ry, rot, large, sweep, x2, y2 = (float(g) for g in match.groups())

    if abs(x1 - x2) < 1e-6 and abs(y1 - y2) < 1e-6:
        # A full circle is encoded as a degenerate arc; recover centre from the sweep.
        radius = abs(rx)
        cx = x1 + radius if int(sweep) == 1 else x1 - radius
        fp.circles.append(FpCircle(cx=cx - ox, cy=y1 - oy, radius=radius,
                                    width=width, layer=layer))
        return

    pts = _arc_to_points((x1, y1), (x2, y2), abs(rx), abs(ry), rot, int(large), int(sweep))
    if len(pts) < 2:
        return
    conv = [_pxy(p[0], p[1], ox, oy) for p in pts]
    # Emit as a polyline track chain; it round-trips cleanly through Altium.
    for a, b in zip(conv, conv[1:]):
        fp.tracks.append(FpTrack(points=[a, b], width=width, layer=layer))


def _h_circle(fp: FootprintData, f: list[str], ox: float, oy: float, raw: str) -> None:
    # CIRCLE~cx~cy~r~strokeWidth~layer~net~id~locked
    if len(f) < 5:
        return
    if str(f[4]).strip() == "100":
        # Decorative circle drawn on pads; skip as JLC2KiCad does.
        return
    fp.circles.append(FpCircle(
        cx=_f(f[0]) - ox, cy=_f(f[1]) - oy,
        radius=_f(f[2]), width=_f(f[3]), layer=to_altium_layer(f[4]),
    ))


def _h_solidregion(fp: FootprintData, f: list[str], ox: float, oy: float, raw: str) -> None:
    # SOLIDREGION~layer~net~path~type~id~...
    if len(f) < 4:
        return
    layer_id = f[0]
    kind = str(f[3]).strip().lower()
    pts = _flatten_svg_path(f[2])
    if len(pts) < 3:
        return
    if kind == "npth":
        # Non-plated cut-out: reduce the outline to a circular hole at its centre.
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        fp.holes.append(FpHole(
            x=(min(xs) + max(xs)) / 2.0 - ox,
            y=(min(ys) + max(ys)) / 2.0 - oy,
            radius=max(max(xs) - min(xs), max(ys) - min(ys)) / 2.0,
        ))
        return
    layer = am.PcbLayer.MECHANICAL_1 if layer_id.strip() == "10" else to_altium_layer(layer_id)
    if layer in (am.PcbLayer.TOP_PASTE, am.PcbLayer.BOTTOM_PASTE):
        # Paste apertures that mirror their own pad: Altium derives the paste layer
        # from the pad, so emitting them again would double the stencil apertures.
        log.debug("skipping paste region duplicating a pad (layer %s)", layer_id)
        return
    fp.regions.append(FpRegion(
        points=[_pxy(p[0], p[1], ox, oy) for p in pts], layer=layer,
    ))


def _h_rect(fp: FootprintData, f: list[str], ox: float, oy: float, raw: str) -> None:
    # RECT~x~y~dx~dy~layer~id~strokeWidth~rotation
    if len(f) < 8:
        return
    fp.rects.append(FpRect(
        x=_f(f[0]) - ox, y=_f(f[1]) - oy, dx=_f(f[2]), dy=_f(f[3]),
        width=_f(f[7]), layer=to_altium_layer(f[4]),
    ))


def _h_hole(fp: FootprintData, f: list[str], ox: float, oy: float, raw: str) -> None:
    # HOLE~x~y~radius~id
    if len(f) < 3:
        return
    fp.holes.append(FpHole(x=_f(f[0]) - ox, y=_f(f[1]) - oy, radius=_f(f[2])))


def _h_text(fp: FootprintData, f: list[str], ox: float, oy: float, raw: str) -> None:
    # TEXT~type~x~y~rotation~color~font~size~mirror~locked~content
    if len(f) < 10 or not f[9]:
        return
    fp.texts.append(FpText(
        text=f[9], x=_f(f[1]) - ox, y=_f(f[2]) - oy,
        rotation=_f(f[3], 0.0), height=_f(f[6], 40.0) or 40.0,
    ))


def _h_svgnode(fp: FootprintData, f: list[str], ox: float, oy: float, raw: str) -> None:
    # SVGNODE~<json payload>
    if not f:
        return
    try:
        payload = json.loads(f[0])
    except json.JSONDecodeError:
        return
    attrs = payload.get("attrs", {}) if isinstance(payload, dict) else {}
    uuid = str(attrs.get("uuid") or "")
    if not uuid:
        return
    origin = _numbers(str(attrs.get("c_origin", "0,0")))
    ox_c, oy_c = (origin[0], origin[1]) if len(origin) >= 2 else (0.0, 0.0)
    rotation = _numbers(str(attrs.get("c_rotation", "0,0,0")))
    fp.step = StepModelRef(
        uuid=uuid,
        title=str(attrs.get("title") or ""),
        x=ox_c - ox,
        y=oy_c - oy,
        z=_f(attrs.get("z")),
        rotation_x=_f(rotation[0]) if len(rotation) > 0 else 0.0,
        rotation_y=_f(rotation[1]) if len(rotation) > 1 else 0.0,
        rotation_z=_f(rotation[2]) if len(rotation) > 2 else 0.0,
        width=_f(attrs.get("c_width")),
        height=_f(attrs.get("c_height")),
    )


_FOOTPRINT_HANDLERS = {
    "TRACK": _h_track,
    "PAD": _h_pad,
    "ARC": _h_arc,
    "CIRCLE": _h_circle,
    "SOLIDREGION": _h_solidregion,
    "SVGNODE": _h_svgnode,
    "RECT": _h_rect,
    "HOLE": _h_hole,
    "TEXT": _h_text,
}


def iter_kinds(shapes: Iterable[str]) -> dict[str, int]:
    """Count primitive kinds; handy for diagnostics and tests."""
    counts: dict[str, int] = {}
    for item in shapes:
        kind = item.split("~", 1)[0]
        counts[kind] = counts.get(kind, 0) + 1
    return counts
