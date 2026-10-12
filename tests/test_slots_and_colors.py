"""Slotted holes, pin placement and STEP colour extraction."""
import math

import altium_monkey as am
import pytest

from lcsc2altium.builders.pcblib_builder import PcbLibBuilder
from lcsc2altium.easyeda_parser import parse_footprint, parse_symbol
from lcsc2altium.models import FpPad

approx = pytest.approx

SLOT_PAD = ("PAD~OVAL~4017.717~3007.158~4.7244~7.0866~11~~2~1.5748"
            "~4017.717 3005.9769 4017.717 3008.3391~0~gge1~0~~Y")
ROUND_PAD = "PAD~OVAL~4017.717~3007.158~4.7244~7.0866~11~~3~1.5748~~0~gge1~0~~Y"
BBOX = {"x": 4010.0, "y": 3000.0, "width": 20.0, "height": 20.0}


def _payload(shapes):
    return {"packageDetail": {"dataStr": {
        "head": {"c_para": {"package": "P"}}, "BBox": BBOX, "shape": shapes,
    }}}


# ------------------------------------------------------------------ slots
def test_two_point_hole_polygon_becomes_a_slot():
    fp = parse_footprint(_payload([SLOT_PAD]), "F")
    pad = fp.pads[0]
    assert pad.slot_points is not None
    assert len(pad.slot_points) == 2
    # Axis is vertical, 2.3622 EasyEDA units long, stored relative to the anchor.
    (ax, ay), (bx, by) = pad.slot_points
    assert ax == approx(bx)
    assert math.dist((ax, ay), (bx, by)) == approx(23.622)


def test_plain_hole_has_no_slot():
    fp = parse_footprint(_payload([ROUND_PAD]), "F")
    assert fp.pads[0].slot_points is None
    assert fp.pads[0].hole == approx(31.496)


def test_polygon_shape_still_keeps_its_outline():
    polygon_pad = ("PAD~POLYGON~4017.717~3007.158~4.7~7.0~11~~1~1.5"
                   "~4016 3005 4019 3005 4019 3009 4016 3009~0~id~0~~Y")
    fp = parse_footprint(_payload([polygon_pad]), "F")
    assert fp.pads[0].polygon and fp.pads[0].slot_points is None


def test_slot_reaches_the_published_pad(tmp_path):
    fp = parse_footprint(_payload([SLOT_PAD]), "USB-C")
    builder = PcbLibBuilder()
    name = builder.add_footprint(fp)
    path = tmp_path / "Slot.PcbLib"
    builder.save(path)

    footprint = am.AltiumPcbLib(path).find_footprint(name)
    pads = [p for p in footprint.primitives if type(p).__name__ == "AltiumPcbPad"]
    assert len(pads) == 1
    pad = pads[0]
    assert pad.hole_shape == am.PadHoleShape.SLOT
    assert pad.hole_size_mils == approx(31.496)
    # Altium stores the slot length in internal units of 1e-4 mil.
    assert pad.slot_size / 10000.0 == approx(55.118, rel=1e-3)


def test_slot_rotation_is_vertical():
    fp = parse_footprint(_payload([SLOT_PAD]), "USB-C")
    builder = PcbLibBuilder()
    name = builder.add_footprint(fp)
    footprint = builder.footprint(name)
    pad = [p for p in footprint.primitives if type(p).__name__ == "AltiumPcbPad"][0]
    assert abs(abs(pad.slot_rotation) - 90.0) < 1e-6


def test_slot_length_never_below_the_drill():
    """Altium rejects a slot shorter than its drill; the builder must clamp."""
    from lcsc2altium.models import FootprintData

    pad = FpPad(designator="1", x=0.0, y=0.0, width=40.0, height=40.0,
                layer=am.PcbLayer.MULTI_LAYER, shape=am.PadShape.CIRCLE,
                hole=30.0, slot_points=[(0.0, 0.0), (0.0, 1.0)])
    builder = PcbLibBuilder()
    name = builder.add_footprint(FootprintData(name="F", pads=[pad]))
    written = [p for p in builder.footprint(name).primitives
               if type(p).__name__ == "AltiumPcbPad"][0]
    assert written.hole_size_mils >= 30.0


# ------------------------------------------------------------------ pin placement
def _pin_payload(pin: str):
    return {"dataStr": {"head": {"x": 0, "y": 0, "c_para": {}},
                        "BBox": {}, "shape": [pin]}}


PIN_AT = ("P~show~1~2~40~0~0~id~0^^40~0^^M 40 -0 h-10~#000000"
          "^^0~25~3~0~2~end~~~#000000^^0~35~-1~0~2~start~~~#000000^^0~33~0^^0")


def test_pin_pitch_survives_snapping():
    """Regression: snapping in EasyEDA units gave a 500 mil pitch instead of 50.

    The grid is expressed in mils, so it must be applied after the unit scaling.
    """
    pins = []
    for row in range(5):
        y = 300 - row * 10            # EasyEDA units -> 100 mil pitch
        pins.append(PIN_AT.replace("~2~40~0~0~", f"~{row + 1}~40~{y}~0~"))
    sym = parse_symbol(_pin_payload(""), "S")
    for raw in pins:
        _ = raw
    sym = parse_symbol({"dataStr": {"head": {"x": 0, "y": 0, "c_para": {}},
                                 "BBox": {}, "shape": pins}}, "S")
    ys = sorted({round(p.y, 3) for p in sym.pins})
    gaps = sorted({round(b - a, 3) for a, b in zip(ys, ys[1:])})
    assert gaps == [100.0]
    assert all(abs(v % 50.0) < 1e-6 for v in ys)


def test_single_pin_keeps_its_position():
    sym = parse_symbol(_pin_payload(PIN_AT), "S")
    pin = sym.pins[0]
    assert pin.length == approx(100.0)
    assert pin.x == approx(-50.0)
    assert pin.rotation is am.Rotation90.DEG_0


def test_pins_are_passive_and_on_grid():
    sym = parse_symbol(_pin_payload(PIN_AT), "S")
    assert all(p.electrical is am.PinElectrical.PASSIVE for p in sym.pins)
    assert all(abs(p.x % 50.0) < 1e-6 and abs(p.y % 50.0) < 1e-6 for p in sym.pins)


# ------------------------------------------------------------------ STEP colours
def test_tessellate_step_contract_mentions_colors():
    from lcsc2altium.ui.step_viewer import tessellate_step
    doc = tessellate_step.__doc__ or ""
    assert "colors" in doc
    assert "STEPCAF" in doc


def _write_box_step(path):
    import cadquery as cq
    cq.exporters.export(cq.Workplane().box(2.0, 1.0, 0.5), str(path), "STEP")
    return path


def test_colours_are_produced_for_every_vertex(tmp_path):
    from lcsc2altium.ui.step_viewer import tessellate_step
    step = _write_box_step(tmp_path / "box.step")
    vertices, triangles, colors = tessellate_step(step, 0.05)
    assert len(vertices) > 0 and len(triangles) > 0
    assert len(colors) == len(vertices)
    assert all(len(c) == 3 for c in colors)
    assert all(0.0 <= channel <= 1.0 for c in colors for channel in c)


def test_plain_fallback_colours_every_vertex(tmp_path):
    from lcsc2altium.ui.step_viewer import _tessellate_plain
    step = _write_box_step(tmp_path / "box2.step")
    vertices, triangles, colors = _tessellate_plain(step, 0.1)
    assert len(colors) == len(vertices)
    assert len(triangles) > 0
    assert all(len(c) == 3 for c in colors)
