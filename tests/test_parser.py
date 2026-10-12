"""Decoding of EasyEDA primitive strings."""

import altium_monkey as am
import pytest

from lcsc2altium.easyeda_parser import (
    _arc_to_points,
    _flatten_svg_path,
    _numbers,
    iter_kinds,
    parse_component_info,
    parse_footprint,
    parse_symbol,
    scale_footprint,
    scale_symbol,
)
from lcsc2altium.models import (
    FootprintData,
    FpPad,
    SymbolData,
)

approx = pytest.approx


def _symbol_payload(shapes, bbox=None, c_para=None):
    return {
        "dataStr": {
            "head": {"x": 0, "y": 0, "c_para": c_para or {}},
            "BBox": bbox or {"x": 0, "y": 0, "width": 0, "height": 0},
            "shape": shapes,
        }
    }


def _package_payload(shapes, bbox=None, c_para=None):
    return {
        "packageDetail": {
            "title": "PKG",
            "dataStr": {
                "head": {"c_para": c_para or {"package": "PKG"}, "uuid_3d": ""},
                "BBox": bbox or {"x": 0, "y": 0, "width": 0, "height": 0},
                "shape": shapes,
            },
        }
    }


PIN = (
    "P~show~1~2~40~0~0~rep2~0^^40~0^^M 40 -0 h-10~#000000"
    "^^0~25~3~0~2~end~~~#000000^^0~35~-1~0~2~start~~~#000000^^0~33~0^^0"
)


# ---------------------------------------------------------------- numbers / paths
def test_numbers_extracts_negatives_and_decimals():
    assert _numbers("M 10 -3.5 L 2 0") == [10.0, -3.5, 2.0, 0.0]


def test_flatten_svg_path_line_commands():
    points = _flatten_svg_path("M 0 0 L 10 0 L 10 10 L 0 10 Z")
    assert points[0] == (0.0, 0.0)
    assert points[1] == (10.0, 0.0)
    assert (0.0, 0.0) in points  # Z closes the shape


def test_flatten_svg_path_relative_origin():
    points = _flatten_svg_path("M 100 50 L 110 50", (100.0, 50.0))
    assert points[0] == (0.0, 0.0)
    assert points[1] == (10.0, 0.0)


def test_arc_full_circle_is_degenerate():
    assert _arc_to_points((0.0, 0.0), (0.0, 0.0), 5.0, 5.0, 0.0, 0, 1) == []


def test_arc_reaches_its_end_point():
    pts = _arc_to_points((0.0, 0.0), (10.0, 10.0), 10.0, 10.0, 0.0, 0, 1)
    assert pts
    assert pts[-1][0] == approx(10.0)
    assert pts[-1][1] == approx(10.0)


# ---------------------------------------------------------------- symbol
def test_symbol_pin_geometry_is_scaled_and_flip_correctly():
    result = _symbol_payload([PIN])
    sym = parse_symbol(result, "S")

    assert len(sym.pins) == 1
    p = sym.pins[0]
    assert p.designator == "2"
    # Raw hotspot at x=40 is snapped to the 50 mil EasyEDA grid (1.0 unit), then the
    # symbol is recentred on its own bounds and finally scaled by 10.
    assert p.x == approx(-50.0)
    assert p.y == approx(0.0)
    assert p.length == approx(100.0)
    # EasyEDA and Altium share the rotation value; no 180 degree offset.
    assert p.rotation is am.Rotation90.DEG_0


def test_every_pin_is_passive():
    # EasyEDA reports passive parts as inputs/outputs; everything is forced passive.
    result = _symbol_payload([PIN])
    assert all(p.electrical is am.PinElectrical.PASSIVE
               for p in parse_symbol(result, "S").pins)


def test_pin_positions_snap_to_the_50_mil_grid():
    from lcsc2altium.constants import SCHEMATIC_GRID_MILS, snap_to_grid
    assert SCHEMATIC_GRID_MILS == 50.0
    assert snap_to_grid(24.0) == 0.0
    assert snap_to_grid(26.0) == 50.0
    assert snap_to_grid(-51.0) == -50.0


def test_symbol_rectangle_uses_width_and_length_fields():
    result = _symbol_payload(["R~10~-4~~~20~8~#880000~1~0~none~rep4~0"])
    sym = parse_symbol(result, "S")
    assert len(sym.rectangles) == 1
    r = sym.rectangles[0]
    # width 20 / length 8 units, recentred on the rectangle itself (x spans 10..30)
    # and scaled; Y is negated so the extent grows the other way.
    assert (r.x1, r.x2) == approx((-100.0, 100.0))
    assert (r.y1, r.y2) == approx((40.0, -40.0))


def test_symbol_is_recentred_on_its_geometry():
    # A BBox far away from the drawing must not drag the origin: the symbol is
    # centred on its real geometry instead.
    result = _symbol_payload(
        [PIN],
        bbox={"x": 473.9, "y": 249.7, "width": 92.1, "height": 130.3},
    )
    sym = parse_symbol(result, "S")
    xs = [p.x for p in sym.pins] + [p.x + p.length for p in sym.pins]
    assert (min(xs) + max(xs)) / 2.0 == approx(0.0, abs=1e-6)


def test_symbol_survives_garbage_primitives():
    result = _symbol_payload(["R~oops~bad~x~y~z", "P~", "BOGUS~1~2"])
    sym = parse_symbol(result, "S")
    assert isinstance(sym, SymbolData)


def test_symbol_empty_payload_is_empty():
    assert parse_symbol({}, "S").is_empty


# ---------------------------------------------------------------- footprint
def test_footprint_track_width_comes_before_layer():
    # TRACK~width~layer~net~points~id~locked -- width is the FIRST field, which the
    # raw payload and EasyEDA's own SVG both confirm.
    track = "TRACK~0.6~3~~0 0 10 0~gge1~0"
    result = _package_payload([track], bbox={"x": 0, "y": 0, "width": 10, "height": 0})
    fp = parse_footprint(result, "F")

    assert len(fp.tracks) == 1
    t = fp.tracks[0]
    assert t.layer is am.PcbLayer.TOP_OVERLAY
    assert t.width == approx(6.0)              # 0.6 EasyEDA unit = 0.1524 mm
    assert t.points[0][0] == approx(-50.0)     # recentred, then scaled and mirrored


def test_footprint_silkscreen_width_matches_source_mm():
    # A DIP-8 silkscreen line is 1 EasyEDA unit: 0.254 mm in the source, which must
    # arrive in the Altium library as 10 mils -- not as 3 units (0.762 mm).
    track = "TRACK~1~3~~0 0 100 0~gge1~0"
    result = _package_payload([track], bbox={"x": 0, "y": 0, "width": 100, "height": 0})
    fp = parse_footprint(result, "F")
    assert fp.tracks[0].width == approx(10.0)


def test_footprint_copper_tracks_are_dropped():
    # Some packages keep placement helpers on copper; EasyEDA never renders them.
    result = _package_payload(
        ["TRACK~3~1~~0 0 100 0~gge1~0"],
        bbox={"x": 0, "y": 0, "width": 100, "height": 0},
    )
    assert parse_footprint(result, "F").tracks == []


def test_footprint_is_mirrored_on_y():
    # EasyEDA is Y-down, an Altium PcbLib is Y-up: a pad above the EasyEDA origin
    # must end up below it (negated) so the footprint is not upside down.
    pad = "PAD~RECT~10~150~20~20~1~~1~0~~0~id~0~~Y"
    result = _package_payload([pad], bbox={"x": 0, "y": 0, "width": 20, "height": 300})
    fp = parse_footprint(result, "F")
    assert fp.pads[0].y == approx((150.0 - 150.0) * 10.0)
    assert fp.pads[0].y == approx(0.0)


def test_footprint_mirror_reverses_pad_rotation():
    pad = "PAD~RECT~10~0~20~20~1~~1~0~~30~id~0~~Y"
    result = _package_payload([pad], bbox={"x": 0, "y": 0, "width": 20, "height": 20})
    fp = parse_footprint(result, "F")
    assert fp.pads[0].rotation_degrees == approx(-30.0)


def test_scale_footprint_mirror_toggle():
    # scale_footprint is the single place the reflection happens, so both branches
    # are checked directly on a hand-built record.
    fp = FootprintData(name="F", pads=[FpPad(
        designator="1", x=0.0, y=100.0, width=10.0, height=10.0,
        layer=am.PcbLayer.TOP, shape=am.PadShape.RECTANGLE, rotation=45.0,
    )])

    mirrored = scale_footprint(FootprintData(
        name="F", pads=[FpPad(
            designator="1", x=0.0, y=100.0, width=10.0, height=10.0,
            layer=am.PcbLayer.TOP, shape=am.PadShape.RECTANGLE, rotation=45.0,
        )]), 1.0, mirror_y=True)
    assert mirrored.pads[0].y == approx(-100.0)
    assert mirrored.pads[0].rotation_degrees == approx(-45.0)

    kept = scale_footprint(fp, 1.0, mirror_y=False)
    assert kept.pads[0].y == approx(100.0)
    assert kept.pads[0].rotation_degrees == approx(45.0)


def test_footprint_th_pad_hole_is_doubled():
    pad = "PAD~ELLIPSE~10~10~7.0866~7.0866~11~~3~1.9685~~0~id~0~~Y"
    result = _package_payload([pad], bbox={"x": 0, "y": 0, "width": 20, "height": 20})
    fp = parse_footprint(result, "F")

    assert len(fp.pads) == 1
    p = fp.pads[0]
    assert p.designator == "3"
    assert p.layer is am.PcbLayer.MULTI_LAYER
    assert p.shape is am.PadShape.CIRCLE
    assert p.hole == approx(39.37)           # stored radius doubled
    assert p.width == approx(70.866)
    assert p.plated is True


def test_footprint_smt_pad_has_no_hole():
    pad = "PAD~RECT~10~10~2.2047~6.8701~1~~1~0~~0~id~0~~Y"
    result = _package_payload([pad], bbox={"x": 0, "y": 0, "width": 20, "height": 20})
    p = parse_footprint(result, "F").pads[0]
    assert p.hole == approx(0.0)
    assert p.layer is am.PcbLayer.TOP


def test_footprint_svgnode_yields_step_model():
    node = ('SVGNODE~{"attrs":{"uuid":"abc123","title":"R0603",'
            '"c_origin":"4000,3000","z":"0","c_rotation":"0,0,90"}}')
    result = _package_payload([node], bbox={"x": 4000, "y": 3000, "width": 10, "height": 10})
    fp = parse_footprint(result, "F")
    assert fp.step is not None
    assert fp.step.uuid == "abc123"
    # Rotation about Z reverses under the Y mirror; about Y it would not.
    assert fp.step.rotation_z == approx(-90.0)


def test_footprint_circle_uses_correct_layer_field():
    result = _package_payload(
        ["CIRCLE~10~10~5~0.6~101~id~0"],
        bbox={"x": 0, "y": 0, "width": 20, "height": 20},
    )
    fp = parse_footprint(result, "F")
    assert fp.circles[0].layer is am.PcbLayer.MECHANICAL_4


def test_footprint_npth_region_becomes_hole():
    region = "SOLIDREGION~11~~M 0 0 L 10 0 L 10 10 L 0 10 Z~npth~id"
    result = _package_payload([region], bbox={"x": 0, "y": 0, "width": 10, "height": 10})
    fp = parse_footprint(result, "F")
    assert fp.regions == []
    assert len(fp.holes) == 1
    assert fp.holes[0].radius == approx(50.0)


def test_footprint_missing_package_returns_none():
    assert parse_footprint({}, "F") is None


# ---------------------------------------------------------------- metadata
def test_component_info_reads_c_para():
    result = {
        "title": "0603WAF1002T5E",
        "description": "10K",
        "szlcsc": {"number": "C25804"},
        "dataStr": {
            "head": {"c_para": {
                "Manufacturer Part": "0603WAF1002T5E",
                "Manufacturer": "UNI-ROYAL",
                "package": "R0603",
                "Value": "10kΩ",
            }}
        },
    }
    info = parse_component_info(result)
    assert info.lcsc == "C25804"
    assert info.mpn == "0603WAF1002T5E"
    assert info.package == "R0603"
    assert info.default_footprint_name == "R0603"


def test_default_names_are_sanitised():
    info = parse_component_info({"szlcsc": {"number": "C1"}, "dataStr": {}})
    info.mpn = "bad/name with spaces"
    assert "/" not in info.default_symbol_name
    assert " " not in info.default_symbol_name


def test_component_info_falls_back_to_title():
    result = {"title": "PART-X", "szlcsc": {"number": "C9"}, "dataStr": {"head": {"c_para": {}}}}
    assert parse_component_info(result).mpn == "PART-X"


# ---------------------------------------------------------------- helpers
def test_iter_kinds_counts_primitives():
    assert iter_kinds(["P~a", "P~b", "R~c"]) == {"P": 2, "R": 1}


def test_scaling_helpers_leave_empty_data_alone():
    assert scale_symbol(SymbolData(name="x"), 1.0).name == "x"
    assert scale_footprint(FootprintData(name="x"), 1.0).name == "x"
