"""SVG/3D preview helpers shared by the UI viewports."""

from __future__ import annotations

import copy
import logging
import os
import xml.etree.ElementTree as ET

import altium_monkey as am
from PyQt6.QtGui import QFont, QFontDatabase, QFontMetricsF, QPainterPath
from PyQt6.QtWidgets import QApplication

from .models import FootprintData, SymbolData

log = logging.getLogger(__name__)

_DESIGNATOR_APP: QApplication | None = None
_DESIGNATOR_FONT: tuple[QFont, QFontMetricsF] | None = None


def _designator_font() -> tuple[QFont, QFontMetricsF]:
    global _DESIGNATOR_APP, _DESIGNATOR_FONT
    if _DESIGNATOR_FONT is not None:
        return _DESIGNATOR_FONT

    app = QApplication.instance()
    if app is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _DESIGNATOR_APP = QApplication([])

    family = None
    windir = os.environ.get("WINDIR", r"C:\Windows")
    font_paths = (
        os.path.join(windir, "Fonts", "arial.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    )
    for font_path in font_paths:
        if os.path.isfile(font_path):
            font_id = QFontDatabase.addApplicationFont(font_path)
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                family = families[0]
                break
    if family is None:
        families = QFontDatabase.families()
        if families:
            family = QFontDatabase.systemFont(
                QFontDatabase.SystemFont.GeneralFont
            ).family()
        else:
            raise RuntimeError("No installed font is available for pad designators")

    font = QFont(family)
    font.setPixelSize(1000)
    metrics = QFontMetricsF(font)
    sample = QPainterPath()
    sample.addText(0.0, 0.0, font, "8")
    if metrics.horizontalAdvance("8") >= 1000 or sample.elementCount() < 10:
        raise RuntimeError(f"Font {family!r} has no usable vector glyph outlines")

    _DESIGNATOR_FONT = font, metrics
    return _DESIGNATOR_FONT

# Layer palette for the footprint preview. Altium's SVG renderer displays
# Multi-Layer pads on both copper layers, so they use the copper color.
LAYER_COLORS: dict[am.PcbLayer, str] = {
    am.PcbLayer.TOP: "#FF0000",
    am.PcbLayer.BOTTOM: "#FF0000",
    am.PcbLayer.TOP_OVERLAY: "#F4D03F",
    am.PcbLayer.BOTTOM_OVERLAY: "#F4D03F",
}


# Keep the preview limited to copper, silkscreen and Multi-Layer.
PREVIEW_LAYERS = (
    am.PcbLayer.TOP,
    am.PcbLayer.BOTTOM,
    am.PcbLayer.TOP_OVERLAY,
    am.PcbLayer.BOTTOM_OVERLAY,
    am.PcbLayer.MULTI_LAYER,
)
LAYER_ORDER = PREVIEW_LAYERS


def footprint_svg_options() -> am.PcbSvgRenderOptions:
    """Render options for a clean, white-background footprint preview."""
    return am.PcbSvgRenderOptions(
        layer_colors=dict(LAYER_COLORS),
        layer_render_order=list(LAYER_ORDER),
        svg_size_unit="mm",
        include_metadata=False,
        show_board_outline=False,
        show_empty_layers=False,
        group_mode="layer",
        drill_hole_mode="knockout",
        drill_hole_overlay_color="#FF5252",
        show_pad_designators=False,
        show_origin_datum=False,
        precision=4,
    )


def render_symbol(lib: am.AltiumSchLib, name: str, *, background: str = "#FFFFFF",
                  width: int | None = None, height: int | None = None) -> str:
    """Render one symbol from a SchLib as a standalone SVG string."""
    try:
        return lib.symbol_to_svg(
            name, width=width, height=height, background=background, auto_fit=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("symbol render failed for %s: %s", name, exc)
        return ""


def _filtered_preview_footprint(fp: FootprintData) -> FootprintData:
    """Keep visible geometry and represent every pad on a preview copper layer."""
    filtered = copy.deepcopy(fp)
    for pad in filtered.pads:
        if pad.layer not in PREVIEW_LAYERS:
            pad.layer = _preview_copper_layer(pad.layer)
    filtered.tracks = [track for track in filtered.tracks if track.layer in PREVIEW_LAYERS]
    filtered.arcs = [arc for arc in filtered.arcs if arc.layer in PREVIEW_LAYERS]
    filtered.rects = [rect for rect in filtered.rects if rect.layer in PREVIEW_LAYERS]
    filtered.circles = [circle for circle in filtered.circles if circle.layer in PREVIEW_LAYERS]
    filtered.regions = [region for region in filtered.regions if region.layer in PREVIEW_LAYERS]
    filtered.texts = [text for text in filtered.texts if text.layer in PREVIEW_LAYERS]
    filtered.step = None
    return filtered


def _preview_copper_layer(layer: am.PcbLayer) -> am.PcbLayer:
    """Map otherwise-hidden pad layers to the nearest visible copper side."""
    if "BOTTOM" in layer.name:
        return am.PcbLayer.BOTTOM
    return am.PcbLayer.TOP


def _filter_preview_svg_layers(svg: str) -> str:
    """Remove unrequested layers but retain drill-hole geometry."""
    root = ET.fromstring(svg)
    scene = next(
        (child for child in root if child.attrib.get("id") == "scene"),
        None,
    )
    if scene is not None:
        allowed_ids = {
            f"layer-{layer.name.replace('_', '')}"
            for layer in PREVIEW_LAYERS
        }
        allowed_ids.add("layer-DRILLS")
        for layer_group in list(scene):
            if layer_group.attrib.get("id") not in allowed_ids:
                scene.remove(layer_group)
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    return ET.tostring(root, encoding="unicode")


def _add_preview_pad_designators(svg: str, footprint, footprint_data: FootprintData) -> str:
    """Add scalable, fitted vector labels at every preview pad center."""
    root = ET.fromstring(svg)
    scene = next(
        (child for child in root if child.attrib.get("id") == "scene"),
        None,
    )
    if scene is None:
        return svg

    options = footprint_svg_options()
    renderer = am.PcbSvgRenderer(options)
    pcbdoc = footprint._to_transient_pcbdoc_for_svg()
    min_x, _, _, max_y = renderer._compute_bounds_mils(pcbdoc)

    namespace = "http://www.w3.org/2000/svg"
    group = ET.SubElement(scene, f"{{{namespace}}}g", {"id": "pad-designators"})
    font, font_metrics = _designator_font()
    designators = [
        pad.designator.strip() or str(index)
        for index, pad in enumerate(footprint_data.pads, start=1)
    ]
    paths = []
    max_advance = 0.0
    for label in designators:
        text_path = QPainterPath()
        text_path.addText(0.0, 0.0, font, label)
        bounds = text_path.boundingRect()
        paths.append((text_path, bounds))
        max_advance = max(max_advance, font_metrics.horizontalAdvance(label))

    mil_to_mm = 0.0254
    for pad, label, (text_path, bounds) in zip(footprint_data.pads, designators, paths):
        width = max(pad.width, 0.0)
        height = max(pad.height, 0.0)
        center_x, center_y = pad.x, pad.y
        if pad.polygon:
            xs = [point[0] for point in pad.polygon]
            ys = [point[1] for point in pad.polygon]
            if xs and ys:
                center_x += (min(xs) + max(xs)) / 2.0
                center_y += (min(ys) + max(ys)) / 2.0
                width = max(max(xs) - min(xs), 0.0)
                height = max(max(ys) - min(ys), 0.0)

        available_width = width * mil_to_mm * 0.8
        available_height = height * mil_to_mm * 0.8
        scale = min(
            available_width / max(max_advance, 1.0),
            available_height / max(font_metrics.height(), 1.0),
        )
        if scale <= 0.0 or bounds.isEmpty():
            continue

        x = (center_x - min_x) * mil_to_mm
        center_y_svg = (max_y - center_y) * mil_to_mm
        d = _painter_path_to_svg(text_path)
        transform = (
            f"translate({x:.6f} {center_y_svg:.6f}) "
            f"scale({scale:.8f} {scale:.8f}) "
            f"translate({-bounds.center().x():.6f} {-bounds.center().y():.6f})"
        )
        rotation = ((-pad.rotation + 180.0) % 360.0) - 180.0
        if rotation < -90.0:
            rotation += 180.0
        elif rotation > 90.0:
            rotation -= 180.0
        if rotation:
            transform = (
                f"translate({x:.6f} {center_y_svg:.6f}) "
                f"rotate({rotation:.4f}) "
                f"scale({scale:.8f} {scale:.8f}) "
                f"translate({-bounds.center().x():.6f} {-bounds.center().y():.6f})"
            )
        attributes = {
            "class": "pad-designator",
            "data-designator": label,
            "data-center-x": f"{x:.4f}",
            "data-center-y": f"{center_y_svg:.4f}",
            "data-font-height": f"{scale * bounds.height():.8f}",
            "data-font-scale": f"{scale:.8f}",
            "d": d,
            "transform": transform,
            "fill": "#000000",
            "pointer-events": "none",
        }
        ET.SubElement(group, f"{{{namespace}}}path", attributes)

    ET.register_namespace("", namespace)
    return ET.tostring(root, encoding="unicode")


def _painter_path_to_svg(path: QPainterPath) -> str:
    """Serialize smooth Qt font outlines as SVG path commands."""
    commands: list[str] = []
    index = 0
    subpath_open = False
    while index < path.elementCount():
        element = path.elementAt(index)
        if element.isMoveTo():
            if subpath_open:
                commands.append("Z")
            commands.append(f"M{element.x:.4f},{element.y:.4f}")
            subpath_open = True
        elif element.isLineTo():
            commands.append(f"L{element.x:.4f},{element.y:.4f}")
        elif element.type == QPainterPath.ElementType.CurveToElement:
            control_1 = path.elementAt(index + 1)
            control_2 = path.elementAt(index + 2)
            commands.append(
                f"C{element.x:.4f},{element.y:.4f} "
                f"{control_1.x:.4f},{control_1.y:.4f} "
                f"{control_2.x:.4f},{control_2.y:.4f}"
            )
            index += 2
        index += 1
    if subpath_open:
        commands.append("Z")
    return " ".join(commands)


def render_footprint_data(fp: FootprintData, *, background: str = "#FFFFFF") -> str:
    """Render a :class:`FootprintData` that has not been written to disk yet.

    A throw-away in-memory PcbLib is used so the preview always matches exactly what
    the builder will emit.
    """
    from .builders.pcblib_builder import PcbLibBuilder

    filtered = _filtered_preview_footprint(fp)
    builder = PcbLibBuilder()
    name = builder.add_footprint(filtered)
    options = footprint_svg_options()
    svg = builder.render_footprint(name, options=options)
    svg = _filter_preview_svg_layers(svg)
    return _add_preview_pad_designators(
        svg,
        builder.footprint(name),
        filtered,
    )


def render_symbol_data(symbol: SymbolData, *, background: str = "#FFFFFF",
                       lcsc: str = "", mpn: str = "") -> str:
    """Render a :class:`SymbolData` that has not been written to disk yet."""
    from .builders.schlib_builder import SchLibBuilder

    builder = SchLibBuilder()
    name = builder.add_symbol(symbol, lcsc=lcsc, mpn=mpn)
    return builder.render_symbol(name, background=background)


def estimate_symbol_size(symbol: SymbolData) -> tuple[float, float]:
    """Bounding box (width, height) of a symbol in mils, used for layout sanity."""
    xs: list[float] = []
    ys: list[float] = []
    for pin in symbol.pins:
        ex, ey = pin.outer_end()
        xs += [pin.x, ex]
        ys += [pin.y, ey]
    for rect in symbol.rectangles:
        xs += [rect.x1, rect.x2]
        ys += [rect.y1, rect.y2]
    for line in symbol.lines:
        xs += [line.x1, line.x2]
        ys += [line.y1, line.y2]
    if not xs:
        return (0.0, 0.0)
    return (max(xs) - min(xs), max(ys) - min(ys))
