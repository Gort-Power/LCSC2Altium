"""Centring, polygon handling, passive pins and grid snapping."""
import pytest

import altium_monkey as am

from lcsc2altium.constants import PIN_ELECTRICAL, SCHEMATIC_GRID_MILS, snap_to_grid
from lcsc2altium.easyeda_parser import parse_footprint, parse_symbol
from lcsc2altium.models import (
    FootprintData,
    FpPad,
    FpTrack,
    polygon_is_rectangle,
    recentre_footprint,
    recentre_symbol,
    simplify_polygon,
    SymbolData,
    SchPin,
    SchRect,
)

approx = pytest.approx


# ------------------------------------------------------------------ pins
def test_every_pin_is_passive():
    assert PIN_ELECTRICAL is am.PinElectrical.PASSIVE


def test_pin_electricity_is_ignored_from_the_source():
    # "1" is Input and "4" is Power in EasyEDA; both must become Passive.
    for code in ("0", "1", "2", "3", "4", "5", "6", "7"):
        pin = (f"P~show~{code}~2~40~0~0~id~0^^40~0^^M 40 -0 h-10~#000000"
               "^^0~25~3~0~2~end~~~#000000^^0~35~-1~0~2~start~~~#000000^^0~33~0^^0")
        result = {"dataStr": {"head": {"x": 0, "y": 0, "c_para": {}},
                              "BBox": {}, "shape": [pin]}}
        assert parse_symbol(result, "S").pins[0].electrical is am.PinElectrical.PASSIVE


def test_snap_to_grid():
    assert SCHEMATIC_GRID_MILS == 50.0
    assert snap_to_grid(0) == 0
    assert snap_to_grid(24) == 0
    assert snap_to_grid(26) == 50
    assert snap_to_grid(-51) == -50
    assert snap_to_grid(74) == 50
    assert snap_to_grid(5, grid=0) == 5


# ------------------------------------------------------------------ centring
def test_recentre_symbol_centres_everything():
    sym = SymbolData(
        name="S",
        pins=[SchPin(designator="1", name="1", x=-200.0, y=-100.0,
                     rotation=am.Rotation90.DEG_0, length=100.0,
                     electrical=am.PinElectrical.PASSIVE)],
        rectangles=[SchRect(-50.0, -50.0, 50.0, 50.0)],
    )
    recentre_symbol(sym)
    xs = [p.x for p in sym.pins] + [p.x + p.length for p in sym.pins]
    xs += [sym.rectangles[0].x1, sym.rectangles[0].x2]
    assert (min(xs) + max(xs)) / 2 == approx(0.0)
    ys = [p.y for p in sym.pins] + [sym.rectangles[0].y1, sym.rectangles[0].y2]
    assert (min(ys) + max(ys)) / 2 == approx(0.0)


def test_recentre_footprint_ignores_bbox_and_local_polygons():
    # A polygon pad whose outline is stored relative to the anchor must not drag the
    # bounding box: local coordinates joined an absolute box and pushed the whole
    # footprint thousands of mils off centre.
    fp = FootprintData(name="F", pads=[
        FpPad(designator="1", x=1000.0, y=0.0, width=20.0, height=20.0,
              layer=am.PcbLayer.TOP, shape=am.PadShape.CUSTOM,
              polygon=[(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0)]),
        FpPad(designator="2", x=1020.0, y=0.0, width=20.0, height=20.0,
              layer=am.PcbLayer.TOP, shape=am.PadShape.RECTANGLE),
    ])
    recentre_footprint(fp)
    # bounds run from 990 to 1030 -> centre 1010
    assert fp.pads[0].x == approx(-10.0)
    assert fp.pads[1].x == approx(10.0)
    # the local outline is untouched
    assert fp.pads[0].polygon == [(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0)]


def test_recentre_footprint_keeps_relative_geometry_together():
    fp = FootprintData(name="F", tracks=[
        FpTrack(points=[(-100.0, 0.0), (100.0, 0.0)], width=6.0,
                layer=am.PcbLayer.TOP_OVERLAY),
    ])
    recentre_footprint(fp)
    assert fp.tracks[0].points == [(approx(-100.0), approx(0.0)),
                                   (approx(100.0), approx(0.0))]


def test_recentring_an_empty_model_is_a_noop():
    assert recentre_symbol(SymbolData(name="s")).name == "s"
    assert recentre_footprint(FootprintData(name="f")).name == "f"


# ------------------------------------------------------------------ polygons
def test_polygon_is_rectangle_accepts_jitter_and_duplicates():
    # Exactly the shape EasyEDA emits: 16 vertices with duplicates and ~1e-3 jitter.
    raw = [
        (-11.817, -25.586), (11.806, -25.586), (11.806, -25.585),
        (11.806, 25.594), (11.805, 25.594), (-11.817, 25.594),
        (-11.817, 25.593), (-11.816, 25.593), (-11.817, -25.586),
        (-11.817, -25.586), (11.806, -25.586), (11.806, 25.594),
        (-11.817, 25.594), (-11.817, -25.586),
    ]
    assert polygon_is_rectangle(raw) is True


def test_polygon_is_rectangle_rejects_a_real_shape():
    assert polygon_is_rectangle([(0, 0), (10, 0), (10, 10), (5, 5)]) is False
    assert polygon_is_rectangle([(0, 0), (10, 0), (10, 10)]) is False


def test_polygon_is_rectangle_rejects_degenerate():
    assert polygon_is_rectangle([(0, 0), (0, 5)]) is False
    assert polygon_is_rectangle([(1, 1), (1, 1), (1, 1), (1, 1)]) is False


def test_simplify_polygon_drops_duplicates():
    pts = [(0, 0), (10, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    assert len(simplify_polygon(pts)) == 4


def test_simplify_polygon_keeps_short_lists():
    assert simplify_polygon([(0, 0), (1, 1)]) == [(0, 0), (1, 1)]


# ------------------------------------------------------------------ end to end
def test_footprint_is_recentred_from_geometry():
    # Canvas BBox is far from the real geometry; it must be ignored.
    pad = "PAD~RECT~3988~2987~20~20~1~~1~0~~0~id~0~~Y"
    result = {"packageDetail": {"dataStr": {
        "head": {"c_para": {"package": "P"}},
        "BBox": {"x": 0, "y": 0, "width": 10, "height": 10},
        "shape": [pad],
    }}}
    fp = parse_footprint(result, "F")
    assert fp.pads[0].x == approx(0.0)
    assert fp.pads[0].y == approx(0.0)
