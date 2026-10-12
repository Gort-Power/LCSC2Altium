"""Author an Altium ``.SchLib`` from a parsed :class:`SymbolData`."""

from __future__ import annotations

import logging

import altium_monkey as am

from ..constants import ATTRIBUTION, PARAM_LCSC, PARAM_MPN
from ..models import SymbolData

log = logging.getLogger(__name__)

Point = am.SchPointMils

# Altium schematic stroke font sizes are stored in whole mils.
DEFAULT_FONT_SIZE = 10


class SchLibBuilder:
    """Incrementally populates a single ``AltiumSchLib`` instance."""

    def __init__(self) -> None:
        self.lib = am.AltiumSchLib()
        self._names: set[str] = set()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def unique_name(base: str, taken: set[str]) -> str:
        """Return *base*, or ``base_2``, ``base_3``... when already present."""
        name = base or "UNNAMED"
        if name not in taken:
            return name
        index = 2
        while f"{name}_{index}" in taken:
            index += 1
        return f"{name}_{index}"

    # ------------------------------------------------------------------ symbols
    def add_symbol(self, symbol: SymbolData, *, lcsc: str = "", mpn: str = "") -> str:
        """Add one symbol to the library and return the name actually used."""
        name = self.unique_name(symbol.name, self._names)
        self._names.add(name)

        sym = self.lib.add_symbol(name, description=symbol.description or "")

        for line in symbol.lines:
            sym.add_line(int(round(line.x1)), int(round(line.y1)),
                         int(round(line.x2)), int(round(line.y2)),
                         color=0, line_width=am.LineWidth.SMALL)

        for rect in symbol.rectangles:
            sym.add_rectangle(
                int(round(rect.x1)), int(round(rect.y1)),
                int(round(rect.x2)), int(round(rect.y2)),
                color=0, area_color=0xFFFFFF, is_solid=rect.filled,
                transparent=not rect.filled,
                line_width=am.LineWidth.SMALL,
            )

        for poly in symbol.polygons:
            if len(poly.points) >= 3:
                sym.add_polygon(
                    [(int(round(x)), int(round(y))) for x, y in poly.points],
                    color=0, area_color=0xFFFFFF,
                    is_solid=poly.filled, line_width=am.LineWidth.SMALL,
                )

        for polyline in symbol.polylines:
            if len(polyline.points) >= 2:
                sym.add_polyline(
                    [(int(round(x)), int(round(y))) for x, y in polyline.points],
                    color=0, line_width=am.LineWidth.SMALL,
                )

        for ell in symbol.ellipses:
            sym.add_ellipse(
                int(round(ell.cx)), int(round(ell.cy)),
                int(round(ell.rx)), int(round(ell.ry)),
                color=0, area_color=0xFFFFFF, is_solid=ell.filled,
                line_width=am.LineWidth.SMALL,
            )

        for text in symbol.texts:
            sym.add_label(
                text.text, int(round(text.x)), int(round(text.y)),
                color=0, orientation=int(text.rotation) % 360,
            )

        for pin in symbol.pins:
            dx, dy = pin.direction()
            pin_x = pin.x - dx * pin.length
            pin_y = pin.y - dy * pin.length
            sym.add_pin(am.make_sch_pin(
                designator=pin.designator,
                location_mils=Point(pin_x, pin_y),
                name=pin.name,
                orientation=pin.rotation,
                length_mils=pin.length,
                electrical_type=pin.electrical,
                name_visible=pin.name_visible,
                designator_visible=pin.designator_visible,
            ))

        # The two parameters required by the specification.
        if mpn:
            sym.add_parameter(PARAM_MPN, mpn, is_hidden=True)
        if lcsc:
            sym.add_parameter(PARAM_LCSC, lcsc, is_hidden=True)
        sym.add_parameter("Source", ATTRIBUTION, is_hidden=True)

        sym.set_part_count(1)
        return name

    def link_footprint(self, symbol_name: str, footprint_name: str,
                       description: str = "") -> None:
        """Attach a PCB footprint implementation to an already added symbol."""
        sym = self._find(symbol_name)
        if sym is None:
            log.warning("cannot link footprint: symbol %r missing", symbol_name)
            return
        sym.add_footprint(footprint_name, description=description, is_current=True)

    def rename_symbol(self, old: str, new: str) -> str:
        """Rename an authored symbol, keeping uniqueness."""
        new = self.unique_name(new, self._names - {old})
        renamed = self.lib.rename_symbol(old, new)
        self._names.discard(old)
        self._names.add(new)
        return getattr(renamed, "name", new)

    def _find(self, name: str):
        for sym in self.lib.symbols:
            if sym.name == name:
                return sym
        return None

    # ------------------------------------------------------------------ output
    def symbol_names(self) -> list[str]:
        """Current symbol names, including any renames applied afterwards."""
        return [sym.name for sym in self.lib.symbols]

    def save(self, path) -> None:
        self.lib.save(path)

    def render_symbol(self, name: str, **kwargs) -> str:
        return self.lib.symbol_to_svg(name, **kwargs)
