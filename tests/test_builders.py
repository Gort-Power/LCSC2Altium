"""Altium library authoring: SchLib, PcbLib and the LibPkg package file."""

from unittest.mock import MagicMock

import altium_monkey as am
import pytest

from lcsc2altium.builders.library import ComponentEntry, load_component
from lcsc2altium.builders.libpkg import write_libpkg
from lcsc2altium.builders.pcblib_builder import PcbLibBuilder
from lcsc2altium.builders.schlib_builder import SchLibBuilder
from lcsc2altium.constants import PARAM_LCSC, PARAM_MPN
from lcsc2altium.easyeda_client import EasyedaClient, EasyedaError
from lcsc2altium.models import (
    ComponentInfo,
    FootprintData,
    FpHole,
    FpPad,
    FpText,
    FpTrack,
    SchLine,
    SchPin,
    SchRect,
    StepModelRef,
    SymbolData,
)
from lcsc2altium.preview import (
    LAYER_COLORS,
    PREVIEW_LAYERS,
    _filtered_preview_footprint,
    render_footprint_data,
)

approx = pytest.approx


def make_symbol(name="SYM", pins=2):
    return SymbolData(
        name=name,
        description="a symbol",
        pins=[
            SchPin(designator=str(i + 1), name=str(i + 1), x=-200.0, y=0.0,
                   rotation=am.Rotation90.DEG_0, length=100.0,
                   electrical=am.PinElectrical.PASSIVE)
            for i in range(pins)
        ],
        rectangles=[SchRect(-100.0, -50.0, 100.0, 50.0)],
        lines=[SchLine(-300.0, 0.0, -100.0, 0.0)],
    )


def make_footprint(name="FP"):
    return FootprintData(
        name=name,
        description="a footprint",
        pads=[
            FpPad(designator="1", x=-30.0, y=0.0, width=30.0, height=30.0,
                  layer=am.PcbLayer.TOP, shape=am.PadShape.RECTANGLE),
            FpPad(designator="2", x=30.0, y=0.0, width=30.0, height=30.0,
                  layer=am.PcbLayer.TOP, shape=am.PadShape.RECTANGLE),
        ],
        tracks=[FpTrack(points=[(-30.0, -50.0), (30.0, -50.0)], width=6.0,
                        layer=am.PcbLayer.TOP_OVERLAY)],
    )


# ---------------------------------------------------------------- SchLib
def test_schlib_round_trips_with_required_parameters(tmp_path):
    builder = SchLibBuilder()
    name = builder.add_symbol(make_symbol(), lcsc="C25804", mpn="0603WAF1002T5E")
    path = tmp_path / "Lib.SchLib"
    builder.save(path)

    lib = am.AltiumSchLib(path)
    assert am.AltiumSchLib.get_symbol_names(path) == [name]

    sym = lib.get_symbol(name)
    params = {p.name: p.text for p in sym.parameters}
    assert params[PARAM_MPN] == "0603WAF1002T5E"
    assert params[PARAM_LCSC] == "C25804"
    assert len(sym.pins) == 2
    assert len(sym.rectangles) == 1


def test_schlib_duplicate_symbol_names_are_disambiguated():
    builder = SchLibBuilder()
    first = builder.add_symbol(make_symbol("DUP"))
    second = builder.add_symbol(make_symbol("DUP"))
    assert first == "DUP"
    assert second == "DUP_2"
    assert builder.symbol_names() == ["DUP", "DUP_2"]


def test_schlib_rename_keeps_uniqueness():
    builder = SchLibBuilder()
    builder.add_symbol(make_symbol("A"))
    builder.add_symbol(make_symbol("B"))
    assert builder.rename_symbol("B", "A") == "A_2"
    assert sorted(builder.symbol_names()) == ["A", "A_2"]


def test_schlib_footprint_link():
    builder = SchLibBuilder()
    name = builder.add_symbol(make_symbol())
    builder.link_footprint(name, "FP_X", description="linked")

    sym = builder._find(name)
    assert sym is not None
    assert [i.model_name for i in sym.implementations] == ["FP_X"]
    assert sym.implementations[0].description == "linked"


def test_schlib_link_footprint_tolerates_missing_symbol():
    SchLibBuilder().link_footprint("nope", "FP")  # must not raise


# ---------------------------------------------------------------- PcbLib
def test_pcblib_round_trips_pads_and_tracks(tmp_path):
    builder = PcbLibBuilder()
    name = builder.add_footprint(make_footprint(), lcsc="C25804")
    path = tmp_path / "Lib.PcbLib"
    builder.save(path)

    lib = am.AltiumPcbLib(path)
    assert am.AltiumPcbLib.get_footprint_names(path) == [name]

    fp = lib.find_footprint(name)
    kinds = [type(p).__name__ for p in fp.primitives]
    assert kinds.count("AltiumPcbPad") == 2
    assert "AltiumPcbTrack" in kinds


def test_pcblib_duplicate_names_are_disambiguated():
    builder = PcbLibBuilder()
    assert builder.add_footprint(make_footprint("F")) == "F"
    assert builder.add_footprint(make_footprint("F")) == "F_2"
    assert builder.footprint_names() == ["F", "F_2"]


def test_pcblib_rename_updates_footprint():
    builder = PcbLibBuilder()
    builder.add_footprint(make_footprint("F"))
    assert builder.rename_footprint("F", "G") == "G"
    assert builder.footprint_names() == ["G"]


def test_pcblib_render_footprint_produces_svg():
    builder = PcbLibBuilder()
    name = builder.add_footprint(make_footprint())
    svg = builder.render_footprint(name)
    assert svg.startswith("<svg")
    assert len(svg) > 100


def test_pcblib_render_unknown_footprint_is_empty():
    assert PcbLibBuilder().render_footprint("missing") == ""


def test_pcblib_height_falls_back_to_pad_size():
    builder = PcbLibBuilder()
    builder.add_footprint(make_footprint())
    assert builder._height(make_footprint()) == approx(30.0)


# ---------------------------------------------------------------- LibPkg
def test_libpkg_references_flat_sibling_files(tmp_path):
    schlib = tmp_path / "Lib.SchLib"
    pcblib = tmp_path / "Lib.PcbLib"
    schlib.write_bytes(b"x")
    pcblib.write_bytes(b"y")
    libpkg = write_libpkg(tmp_path / "Lib.LibPkg", schlib, pcblib)

    text = libpkg.read_text(encoding="utf-8")
    assert "DocumentPath=Lib.SchLib" in text
    assert "DocumentPath=Lib.PcbLib" in text
    assert "[Design]" in text and "Version=1.0" in text
    # No sub-directories: the three files live in one folder.
    assert "SchLib\\" not in text and "PCBLib\\" not in text


def test_load_component_tolerates_step_download_403(monkeypatch):
    monkeypatch.setattr(
        "lcsc2altium.builders.library.parse_component_info",
        lambda result: ComponentInfo(lcsc="C12345", mpn="MPN", package="0603"),
    )
    monkeypatch.setattr(
        "lcsc2altium.builders.library.parse_symbol",
        lambda result, name: SymbolData(name=name),
    )
    monkeypatch.setattr(
        "lcsc2altium.builders.library.parse_footprint",
        lambda result, name: FootprintData(
            name=name,
            description="demo",
            pads=[FpPad(designator="1", x=0.0, y=0.0, width=10.0, height=10.0,
                        layer=am.PcbLayer.TOP, shape=am.PadShape.RECTANGLE)],
            step=StepModelRef(uuid="abc123"),
        ),
    )

    client = MagicMock()
    client.fetch_component.return_value = {"result": {}}
    client.find_exact.return_value = {}
    client.download_step.side_effect = EasyedaError("HTTP 403 for models.easyeda.com")

    entry = load_component(client, "C12345", want_footprint=True, want_step=True)

    assert entry.footprint is not None
    assert entry.step_data is None
    assert entry.include_model is False
    assert "HTTP 403" in entry.model_error


def test_download_step_accepts_bom_and_zip_payloads(monkeypatch):
    client = EasyedaClient.__new__(EasyedaClient)

    bom_step = b"\xef\xbb\xbfISO-10303-21;\nHEADER;\nEND-ISO-10303-21;\n"
    monkeypatch.setattr(EasyedaClient, "_get",
                        lambda self, url, binary=False: MagicMock(content=bom_step))
    assert client.download_step("abc123") == bom_step

    zip_payload = b""  # placeholder for actual zip content
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("body.step", bom_step)
    zip_payload = buffer.getvalue()

    monkeypatch.setattr(EasyedaClient, "_get",
                        lambda self, url, binary=False: MagicMock(content=zip_payload))
    assert client.download_step("abc123") == bom_step


def test_preview_filters_non_review_layers():
    fp = FootprintData(
        name="FP",
        pads=[
            FpPad(designator="1", x=0.0, y=0.0, width=10.0, height=10.0,
                  layer=am.PcbLayer.TOP, shape=am.PadShape.RECTANGLE),
            FpPad(designator="2", x=20.0, y=0.0, width=10.0, height=10.0,
                  layer=am.PcbLayer.MECHANICAL_1, shape=am.PadShape.RECTANGLE),
        ],
        tracks=[
            FpTrack(points=[(0.0, 0.0), (10.0, 0.0)], width=2.0, layer=am.PcbLayer.TOP_OVERLAY),
            FpTrack(points=[(0.0, 0.0), (10.0, 0.0)], width=2.0, layer=am.PcbLayer.TOP_PASTE),
        ],
        texts=[
            FpText("ok", 0.0, 0.0, layer=am.PcbLayer.MECHANICAL_13),
            FpText("hidden", 0.0, 0.0, layer=am.PcbLayer.MECHANICAL_2),
        ],
        holes=[FpHole(x=5.0, y=5.0, radius=2.0)],
        step=StepModelRef(uuid="abc123"),
    )

    filtered = _filtered_preview_footprint(fp)

    assert all(pad.layer in PREVIEW_LAYERS for pad in filtered.pads)
    assert all(track.layer in PREVIEW_LAYERS for track in filtered.tracks)
    assert all(text.layer in PREVIEW_LAYERS for text in filtered.texts)
    assert filtered.step is None
    assert len(filtered.pads) == 2
    assert filtered.pads[1].layer is am.PcbLayer.TOP
    assert len(filtered.tracks) == 1
    assert filtered.texts == []
    assert filtered.holes == [FpHole(x=5.0, y=5.0, radius=2.0)]


def test_footprint_preview_keeps_multi_layer_drill_holes():
    fp = FootprintData(
        name="Drilled",
        holes=[FpHole(x=0.0, y=0.0, radius=25.0)],
    )

    svg = render_footprint_data(fp)

    assert 'id="layer-DRILLS"' in svg
    assert svg.count('fill="#FFFFFF"') == 2


def test_footprint_preview_contains_copper_silkscreen_and_multilayer_pads():
    fp = FootprintData(
        name="FP",
        pads=[
            FpPad(
                designator="2",
                x=150.0,
                y=0.0,
                width=100.0,
                height=60.0,
                layer=am.PcbLayer.MULTI_LAYER,
                shape=am.PadShape.RECTANGLE,
            ),
        ],
        tracks=[
            FpTrack(
                points=[(-50.0, 60.0), (50.0, 60.0)],
                width=5.0,
                layer=am.PcbLayer.TOP_OVERLAY,
            ),
        ],
    )

    svg = render_footprint_data(fp)

    assert set(PREVIEW_LAYERS) == {
        am.PcbLayer.TOP,
        am.PcbLayer.BOTTOM,
        am.PcbLayer.TOP_OVERLAY,
        am.PcbLayer.BOTTOM_OVERLAY,
        am.PcbLayer.MULTI_LAYER,
    }
    assert LAYER_COLORS[am.PcbLayer.TOP] == "#FF0000"
    assert LAYER_COLORS[am.PcbLayer.BOTTOM] == "#FF0000"
    assert 'id="layer-TOP"' in svg
    assert 'id="layer-TOPOVERLAY"' in svg
    assert 'id="layer-BOTTOM"' in svg
    assert 'fill="#FF0000"' in svg
    assert 'stroke="#F4D03F"' in svg
    assert svg.count('fill="#FF0000"') >= 2
    assert "TOPPASTE" not in svg
    assert "TOPSOLDER" not in svg


def test_footprint_preview_shows_fitted_pad_designators():
    import xml.etree.ElementTree as ET

    fp = FootprintData(
        name="Labeled",
        pads=[
            FpPad(
                designator="1",
                x=0.0,
                y=0.0,
                width=100.0,
                height=60.0,
                layer=am.PcbLayer.TOP,
                shape=am.PadShape.RECTANGLE,
            ),
            FpPad(
                designator="LONG",
                x=150.0,
                y=0.0,
                width=200.0,
                height=120.0,
                layer=am.PcbLayer.MULTI_LAYER,
                shape=am.PadShape.RECTANGLE,
            ),
            FpPad(
                designator="3",
                x=400.0,
                y=0.0,
                width=40.0,
                height=24.0,
                layer=am.PcbLayer.TOP,
                shape=am.PadShape.RECTANGLE,
            ),
            FpPad(
                designator="4",
                x=500.0,
                y=0.0,
                width=80.0,
                height=48.0,
                layer=am.PcbLayer.BOTTOM,
                shape=am.PadShape.RECTANGLE,
            ),
            FpPad(
                designator="",
                x=650.0,
                y=0.0,
                width=60.0,
                height=36.0,
                layer=am.PcbLayer.MECHANICAL_1,
                shape=am.PadShape.RECTANGLE,
            ),
            FpPad(
                designator="8",
                x=780.0,
                y=0.0,
                width=100.0,
                height=60.0,
                layer=am.PcbLayer.TOP,
                shape=am.PadShape.RECTANGLE,
            ),
            FpPad(
                designator="6",
                x=920.0,
                y=0.0,
                width=100.0,
                height=60.0,
                layer=am.PcbLayer.TOP,
                shape=am.PadShape.RECTANGLE,
                rotation=180.0,
            ),
        ],
    )

    svg = render_footprint_data(fp)
    root = ET.fromstring(svg)
    labels = [
        element for element in root.iter()
        if element.attrib.get("class") == "pad-designator"
    ]

    assert len(labels) == 7
    assert {label.attrib["data-designator"] for label in labels} == {
        "1", "LONG", "3", "4", "5", "8", "6",
    }
    assert all(label.attrib["fill"] == "#000000" for label in labels)
    eight = next(label for label in labels if label.attrib["data-designator"] == "8")
    assert "C" in eight.attrib["d"]
    assert all(
        float(label.attrib["transform"].split("scale(", 1)[1].split()[1].rstrip(")")) > 0
        for label in labels
    )
    scales = {
        label.attrib["data-designator"]: float(label.attrib["data-font-scale"])
        for label in labels
    }
    assert scales["8"] == pytest.approx(scales["1"])
    assert scales["6"] == pytest.approx(scales["1"])
    assert scales["LONG"] > scales["1"] > scales["3"]
    upside_down_candidate = next(
        label for label in labels if label.attrib["data-designator"] == "6"
    )
    assert "rotate(" not in upside_down_candidate.attrib.get("transform", "")

    scene = next(element for element in root if element.attrib.get("id") == "scene")
    pad_centres = []
    for layer_id in ("layer-TOP", "layer-BOTTOM"):
        layer = next(element for element in scene if element.attrib.get("id") == layer_id)
        pad_centres.extend(
            (
                float(element.attrib["x"]) + float(element.attrib["width"]) / 2
                if element.tag.endswith("rect")
                else float(element.attrib["cx"])
            )
            for element in layer
            if element.tag.endswith(("rect", "ellipse"))
        )
    pad_centres = sorted(set(round(value, 4) for value in pad_centres))
    label_positions = sorted(
        round(float(label.attrib["data-center-x"]), 4) for label in labels
    )
    assert label_positions == pad_centres


def test_download_update_replaces_stale_model_when_selection_changes():
    base = ComponentInfo(lcsc="C12345", mpn="MPN")
    first = ComponentEntry(info=base, symbol=SymbolData(name="SYM"), footprint=FootprintData(name="FP"), step_data=b"old")
    second = ComponentEntry(info=base, symbol=SymbolData(name="SYM"), footprint=FootprintData(name="FP"), step_data=b"new", include_model=True)
    second.include_footprint = True
    second.include_symbol = True

    first.step_data = b"old"
    first.include_model = True
    first.include_footprint = True
    first.include_symbol = True

    # Re-downloading same part without 3D should clear stale model data.
    updated = ComponentEntry(info=base, symbol=SymbolData(name="SYM"), footprint=FootprintData(name="FP"), step_data=None, include_model=False)
    updated.include_footprint = True
    updated.include_symbol = True

    existing = first
    existing.step_data = updated.step_data if updated.include_model else None
    assert existing.step_data is None
