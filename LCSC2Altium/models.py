"""Typed representations of the geometry decoded from EasyEDA payloads."""

from __future__ import annotations

from dataclasses import dataclass, field

import altium_monkey as am

from .constants import PIN_ELECTRICAL, to_altium_layer

Point = tuple[float, float]

# Direction in which Altium draws a pin for each rotation value.
_ROTATION_VECTORS: dict[am.Rotation90, Point] = {
    am.Rotation90.DEG_0: (1.0, 0.0),
    am.Rotation90.DEG_90: (0.0, 1.0),
    am.Rotation90.DEG_180: (-1.0, 0.0),
    am.Rotation90.DEG_270: (0.0, -1.0),
}


def rotation_vector(rotation: am.Rotation90) -> Point:
    """Unit vector a schematic pin extends along for the given rotation."""
    return _ROTATION_VECTORS.get(rotation, (1.0, 0.0))


# --------------------------------------------------------------------------------------
# Schematic primitives
# --------------------------------------------------------------------------------------
@dataclass(slots=True)
class SchPin:
    """A schematic pin, already expressed in Altium symbol-local mils.

    ``x`` / ``y`` are the pin's **inner** end (the side that attaches to the symbol
    body). Altium draws a pin from its location along ``rotation`` for ``length``, so
    the inner end is the anchor; :func:`outer_end` gives the free end where wires and
    the net connect.
    """

    designator: str
    name: str
    x: float
    y: float
    rotation: am.Rotation90
    length: float
    electrical: am.PinElectrical
    name_visible: bool = True
    designator_visible: bool = True

    def direction(self) -> Point:
        """Unit vector along which Altium draws the pin, from ``rotation``."""
        return _ROTATION_VECTORS.get(self.rotation, (1.0, 0.0))

    def outer_end(self) -> Point:
        """The free end of the pin (where a wire attaches)."""
        dx, dy = self.direction()
        return (self.x + dx * self.length, self.y + dy * self.length)


@dataclass(slots=True)
class SchLine:
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(slots=True)
class SchRect:
    x1: float
    y1: float
    x2: float
    y2: float
    filled: bool = False


@dataclass(slots=True)
class SchPolygon:
    points: list[Point]
    filled: bool = False


@dataclass(slots=True)
class SchPolyline:
    points: list[Point]


@dataclass(slots=True)
class SchEllipse:
    cx: float
    cy: float
    rx: float
    ry: float
    filled: bool = False


@dataclass(slots=True)
class SchArc:
    cx: float
    cy: float
    radius: float
    start_angle: float
    end_angle: float


@dataclass(slots=True)
class SchText:
    text: str
    x: float
    y: float
    rotation: float = 0.0


@dataclass(slots=True)
class SymbolData:
    """Everything needed to author one Altium schematic symbol."""

    name: str
    description: str = ""
    pins: list[SchPin] = field(default_factory=list)
    lines: list[SchLine] = field(default_factory=list)
    rectangles: list[SchRect] = field(default_factory=list)
    polygons: list[SchPolygon] = field(default_factory=list)
    polylines: list[SchPolyline] = field(default_factory=list)
    ellipses: list[SchEllipse] = field(default_factory=list)
    arcs: list[SchArc] = field(default_factory=list)
    texts: list[SchText] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.pins or self.lines or self.rectangles or self.polygons
                    or self.polylines or self.ellipses or self.arcs)


# --------------------------------------------------------------------------------------
# Footprint primitives
# --------------------------------------------------------------------------------------
@dataclass(slots=True)
class FpPad:
    designator: str
    x: float
    y: float
    width: float
    height: float
    layer: am.PcbLayer
    shape: am.PadShape
    rotation: float = 0.0
    hole: float = 0.0
    drill_offset: Point = (0.0, 0.0)
    polygon: list[Point] | None = None
    plated: bool = True
    # End points of a slotted hole, relative to the pad anchor. EasyEDA stores a
    # slot as the two ends of its axis rather than as a shape flag.
    slot_points: list[Point] | None = None

    @property
    def rotation_degrees(self) -> float:
        return self.rotation

    @rotation_degrees.setter
    def rotation_degrees(self, value: float) -> None:
        self.rotation = value


@dataclass(slots=True)
class FpTrack:
    points: list[Point]
    width: float
    layer: am.PcbLayer


@dataclass(slots=True)
class FpArc:
    cx: float
    cy: float
    radius: float
    start_angle: float
    end_angle: float
    width: float
    layer: am.PcbLayer


@dataclass(slots=True)
class FpRect:
    x: float
    y: float
    dx: float
    dy: float
    width: float
    layer: am.PcbLayer


@dataclass(slots=True)
class FpCircle:
    cx: float
    cy: float
    radius: float
    width: float
    layer: am.PcbLayer


@dataclass(slots=True)
class FpRegion:
    points: list[Point]
    layer: am.PcbLayer
    is_cutout: bool = False


@dataclass(slots=True)
class FpHole:
    x: float
    y: float
    radius: float


@dataclass(slots=True)
class FpText:
    text: str
    x: float
    y: float
    rotation: float = 0.0
    height: float = 40.0
    layer: am.PcbLayer = am.PcbLayer.TOP_OVERLAY


@dataclass(slots=True)
class StepModelRef:
    """Placement metadata of the STEP model referenced by the footprint."""

    uuid: str
    title: str = ""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    rotation_x: float = 0.0
    rotation_y: float = 0.0
    rotation_z: float = 0.0
    width: float = 0.0
    height: float = 0.0


@dataclass(slots=True)
class FootprintData:
    """Everything needed to author one Altium footprint (in mils, origin centred)."""

    name: str
    description: str = ""
    pads: list[FpPad] = field(default_factory=list)
    tracks: list[FpTrack] = field(default_factory=list)
    arcs: list[FpArc] = field(default_factory=list)
    rects: list[FpRect] = field(default_factory=list)
    circles: list[FpCircle] = field(default_factory=list)
    regions: list[FpRegion] = field(default_factory=list)
    holes: list[FpHole] = field(default_factory=list)
    texts: list[FpText] = field(default_factory=list)
    step: StepModelRef | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.pads or self.tracks or self.arcs or self.rects
                    or self.circles or self.regions or self.holes or self.texts)


# --------------------------------------------------------------------------------------
# Component level aggregate
# --------------------------------------------------------------------------------------
@dataclass(slots=True)
class ComponentInfo:
    """Metadata about one LCSC part, independent of any rendering."""

    lcsc: str
    mpn: str = ""
    manufacturer: str = ""
    description: str = ""
    package: str = ""
    value: str = ""
    category: str = ""
    stock: int = 0
    price: str = ""

    @property
    def default_symbol_name(self) -> str:
        base = self.mpn or self.lcsc
        return _sanitize(base)

    @property
    def default_footprint_name(self) -> str:
        base = self.package or self.mpn or self.lcsc
        return _sanitize(base)


def _sanitize(name: str) -> str:
    """Keep Altium-safe characters only (no whitespace or path separators)."""
    cleaned = "".join(ch if (ch.isalnum() or ch in "_-+.") else "_" for ch in name.strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "UNNAMED"


def parse_electrical(value: str) -> am.PinElectrical:
    """Always passive -- see :data:`lcsc2altium.constants.PIN_ELECTRICAL`."""
    return PIN_ELECTRICAL


def recentre_symbol(sym: SymbolData, *, include_pin_length: bool = True) -> SymbolData:
    """Move the symbol so its geometric centre lands on the origin.

    EasyEDA's ``BBox`` is unreliable as an origin -- on some parts it describes the
    canvas rather than the drawing (the USB-C symbol declares a 92 x 130 box while
    its pins span 700 units), which is what pushed the component off centre. The
    bounds are therefore measured on the converted geometry itself.
    """
    xs: list[float] = []
    ys: list[float] = []
    for pin in sym.pins:
        xs.append(pin.x)
        ys.append(pin.y)
        if include_pin_length:
            ex, ey = pin.outer_end()
            xs.append(ex)
            ys.append(ey)
    for line in sym.lines:
        xs += [line.x1, line.x2]
        ys += [line.y1, line.y2]
    for rect in sym.rectangles:
        xs += [rect.x1, rect.x2]
        ys += [rect.y1, rect.y2]
    for poly in sym.polygons:
        xs += [p[0] for p in poly.points]
        ys += [p[1] for p in poly.points]
    for line in sym.polylines:
        xs += [p[0] for p in line.points]
        ys += [p[1] for p in line.points]
    for ell in sym.ellipses:
        xs += [ell.cx - ell.rx, ell.cx + ell.rx]
        ys += [ell.cy - ell.ry, ell.cy + ell.ry]
    for arc in sym.arcs:
        xs += [arc.cx - arc.radius, arc.cx + arc.radius]
        ys += [arc.cy - arc.radius, arc.cy + arc.radius]
    for text in sym.texts:
        xs.append(text.x)
        ys.append(text.y)
    if not xs:
        return sym

    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0

    def dx(x: float) -> float:
        return x - cx

    def dy(y: float) -> float:
        return y - cy

    for pin in sym.pins:
        pin.x, pin.y = dx(pin.x), dy(pin.y)
    for line in sym.lines:
        line.x1, line.x2 = dx(line.x1), dx(line.x2)
        line.y1, line.y2 = dy(line.y1), dy(line.y2)
    for rect in sym.rectangles:
        rect.x1, rect.x2 = dx(rect.x1), dx(rect.x2)
        rect.y1, rect.y2 = dy(rect.y1), dy(rect.y2)
    for poly in sym.polygons:
        poly.points = [(dx(x), dy(y)) for x, y in poly.points]
    for line in sym.polylines:
        line.points = [(dx(x), dy(y)) for x, y in line.points]
    for ell in sym.ellipses:
        ell.cx, ell.cy = dx(ell.cx), dy(ell.cy)
    for arc in sym.arcs:
        arc.cx, arc.cy = dx(arc.cx), dy(arc.cy)
    for text in sym.texts:
        text.x, text.y = dx(text.x), dy(text.y)
    return sym


def recentre_footprint(fp: FootprintData) -> FootprintData:
    """Move the footprint so its geometric centre lands on the origin.

    ``BBox`` is ignored here for the same reason as on the symbol side: it is the
    canvas in some packages. Only real geometry is measured, so pads, silkscreen and
    the 3D body stay aligned with each other.
    """
    xs: list[float] = []
    ys: list[float] = []
    for pad in fp.pads:
        xs += [pad.x - pad.width / 2.0, pad.x + pad.width / 2.0]
        ys += [pad.y - pad.height / 2.0, pad.y + pad.height / 2.0]
        if pad.polygon:
            # Polygon vertices are stored relative to the pad centre, so they have to
            # be offset before they can join an absolute bounding box.
            xs += [pad.x + p[0] for p in pad.polygon]
            ys += [pad.y + p[1] for p in pad.polygon]
    for track in fp.tracks:
        xs += [p[0] for p in track.points]
        ys += [p[1] for p in track.points]
    for arc in fp.arcs:
        xs += [arc.cx - arc.radius, arc.cx + arc.radius]
        ys += [arc.cy - arc.radius, arc.cy + arc.radius]
    for rect in fp.rects:
        xs += [rect.x, rect.x + rect.dx]
        ys += [rect.y, rect.y + rect.dy]
    for circle in fp.circles:
        xs += [circle.cx - circle.radius, circle.cx + circle.radius]
        ys += [circle.cy - circle.radius, circle.cy + circle.radius]
    for region in fp.regions:
        xs += [p[0] for p in region.points]
        ys += [p[1] for p in region.points]
    for hole in fp.holes:
        xs += [hole.x - hole.radius, hole.x + hole.radius]
        ys += [hole.y - hole.radius, hole.y + hole.radius]
    for text in fp.texts:
        xs.append(text.x)
        ys.append(text.y)
    if not xs:
        return fp

    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0

    def dx(x: float) -> float:
        return x - cx

    def dy(y: float) -> float:
        return y - cy

    for pad in fp.pads:
        pad.x, pad.y = dx(pad.x), dy(pad.y)
        # pad.polygon and pad.drill_offset are stored relative to the pad centre, so
        # they must not be shifted: only the anchor moves.
    for track in fp.tracks:
        track.points = [(dx(x), dy(y)) for x, y in track.points]
    for arc in fp.arcs:
        arc.cx, arc.cy = dx(arc.cx), dy(arc.cy)
    for rect in fp.rects:
        rect.x, rect.y = dx(rect.x), dy(rect.y)
    for circle in fp.circles:
        circle.cx, circle.cy = dx(circle.cx), dy(circle.cy)
    for region in fp.regions:
        region.points = [(dx(x), dy(y)) for x, y in region.points]
    for hole in fp.holes:
        hole.x, hole.y = dx(hole.x), dy(hole.y)
    for text in fp.texts:
        text.x, text.y = dx(text.x), dy(text.y)
    if fp.step is not None:
        fp.step.x, fp.step.y = dx(fp.step.x), dy(fp.step.y)
    return fp


def simplify_polygon(points: list[Point], tolerance: float | None = None) -> list[Point]:
    """Drop repeated and collinear vertices from a closed outline.

    EasyEDA traces rectangles as long runs of duplicated and near-collinear points.
    The tolerance defaults to a fraction of the shape's own size so it scales with
    the geometry instead of the absolute coordinate magnitude.
    """
    if len(points) < 4:
        return points
    if tolerance is None:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-9)
        tolerance = span * 1e-4

    cleaned: list[Point] = []
    for point in points:
        if not cleaned or _far(point, cleaned[-1], tolerance):
            cleaned.append(point)
    # Drop the closing point that repeats the first one.
    if len(cleaned) > 1 and not _far(cleaned[0], cleaned[-1], tolerance):
        cleaned.pop()
    if len(cleaned) < 4:
        return points

    # Remove collinear runs: a vertex whose two neighbours lie on the same line.
    changed = True
    while changed and len(cleaned) > 4:
        changed = False
        result: list[Point] = []
        count = len(cleaned)
        for index in range(count):
            prev_pt = cleaned[(index - 1) % count]
            cur_pt = cleaned[index]
            next_pt = cleaned[(index + 1) % count]
            cross = ((cur_pt[0] - prev_pt[0]) * (next_pt[1] - prev_pt[1])
                     - (cur_pt[1] - prev_pt[1]) * (next_pt[0] - prev_pt[0]))
            scale = max(1.0, abs(cur_pt[0] - prev_pt[0]) + abs(cur_pt[1] - prev_pt[1]))
            if abs(cross) > tolerance * scale:
                result.append(cur_pt)
            else:
                changed = True
        cleaned = result or cleaned
    return cleaned


def _far(a: Point, b: Point, tolerance: float) -> bool:
    return abs(a[0] - b[0]) > tolerance or abs(a[1] - b[1]) > tolerance


def polygon_is_rectangle(points: list[Point], rel_tol: float = 1e-3) -> bool:
    """True when every vertex of *points* lies on the border of its bounding box.

    Tested directly instead of relying on vertex reduction, because EasyEDA pads
    carry redundant and slightly jittered vertices that defeat naive simplification.
    """
    if len(points) < 4:
        return False
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if (x1 - x0) <= 0.0 or (y1 - y0) <= 0.0:
        return False
    tol_x = (x1 - x0) * rel_tol
    tol_y = (y1 - y0) * rel_tol
    # A point lies on a rectangle border when it sits on a vertical edge or a
    # horizontal one (corners satisfy both).
    return all(
        min(abs(x - x0), abs(x - x1)) <= tol_x
        or min(abs(y - y0), abs(y - y1)) <= tol_y
        for x, y in points
    )


__all__ = [
    "ComponentInfo", "FootprintData", "FpArc", "FpCircle", "FpHole", "FpPad",
    "FpRect", "FpRegion", "FpText", "FpTrack", "Point", "SchArc", "SchEllipse",
    "SchLine", "SchPin", "SchPolygon", "SchPolyline", "SchRect", "SchText",
    "StepModelRef", "SymbolData", "parse_electrical", "to_altium_layer",
]
