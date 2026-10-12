"""Author an Altium ``.PcbLib`` from a parsed :class:`FootprintData`.

Handles pads, tracks, arcs, fills, regions, silkscreen text, non-plated holes and the
embedded STEP 3D body (Altium stores STEP payloads inside a component-body record).
"""

from __future__ import annotations

import logging
import math

import altium_monkey as am

from ..constants import ATTRIBUTION, PARAM_LCSC
from ..models import FootprintData, FpRect, polygon_is_rectangle

log = logging.getLogger(__name__)

MIN_TRACK_WIDTH = 0.2      # Altium rejects zero-width tracks
DEFAULT_TEXT_HEIGHT = 40.0  # mils


def _try_set(record, attribute: str, value: str) -> bool:
    """Best-effort attribute write; some record shapes expose fields read-only."""
    try:
        setattr(record, attribute, value)
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("could not set %s.%s: %s", type(record).__name__, attribute, exc)
        return False


class PcbLibBuilder:
    """Incrementally populates a single ``AltiumPcbLib`` instance.

    altium-monkey keeps authored footprints private until the library is written, so
    this wrapper keeps its own name -> footprint map. That lets previews be rendered
    straight from in-memory data, before any file exists.
    """

    def __init__(self) -> None:
        self.lib = am.AltiumPcbLib()
        self._names: set[str] = set()
        self._by_name: dict[str, object] = {}
        self._models: dict[str, tuple[object, object]] = {}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def unique_name(base: str, taken: set[str]) -> str:
        name = base or "UNNAMED"
        if name not in taken:
            return name
        index = 2
        while f"{name}_{index}" in taken:
            index += 1
        return f"{name}_{index}"

    # ------------------------------------------------------------------ footprint
    def add_footprint(
        self,
        fp: FootprintData,
        *,
        step_data: bytes | None = None,
        lcsc: str = "",
    ) -> str:
        """Add one footprint, optionally embedding its STEP payload."""
        name = self.unique_name(fp.name, self._names)
        self._names.add(name)

        footprint = self.lib.add_footprint(name, description=fp.description or "")

        for pad in fp.pads:
            self._add_pad(footprint, pad)
        for track in fp.tracks:
            width = max(track.width, MIN_TRACK_WIDTH)
            for start, end in zip(track.points, track.points[1:]):
                try:
                    footprint.add_track(start, end, width_mils=width, layer=track.layer)
                except Exception as exc:  # noqa: BLE001 - keep one bad segment from killing the build
                    log.debug("track skipped in %s: %s", name, exc)
        for arc in fp.arcs:
            self._add_arc(footprint, arc)
        for circle in fp.circles:
            self._add_circle(footprint, circle)
        for rect in fp.rects:
            self._add_rect(footprint, rect)
        for region in fp.regions:
            self._add_region(footprint, region)
        for hole in fp.holes:
            self._add_hole(footprint, hole)
        for text in fp.texts:
            self._add_text(footprint, text)

        if step_data:
            try:
                attached = self._add_step_model(footprint, name, step_data, fp.step)
                if attached is not None:
                    self._models[name] = attached
            except Exception as exc:  # noqa: BLE001 - a broken model must not lose the footprint
                log.warning("3D model could not be embedded in %s: %s", name, exc)

        footprint.set_parameter("HEIGHT", f"{max(self._height(fp), 0.0):.4f}mil")
        if fp.description:
            footprint.set_parameter("DESCRIPTION", fp.description)
        if lcsc:
            footprint.set_parameter(PARAM_LCSC, lcsc)
        footprint.set_parameter("Source", ATTRIBUTION)

        self._by_name[name] = footprint
        return name

    def rename_footprint(self, old: str, new: str) -> str:
        """Rename an authored footprint and keep the internal map in sync.

        The embedded 3D model and its body follow the new name, so the 3D body is
        always named after the footprint that carries it.
        """
        footprint = self._by_name.pop(old, None)
        if footprint is None:
            return old
        new = self.unique_name(new, self._names - {old})
        self.lib.rename_footprint(footprint, new)
        self._names.discard(old)
        self._names.add(new)
        self._by_name[new] = footprint

        attached = self._models.pop(old, None)
        if attached is not None:
            model, body = attached
            model_name = f"{new}.step"
            _try_set(model, "name", model_name)
            # The component body keeps its own copy of the model reference.
            _try_set(body, "name", new)
            _try_set(body, "identifier", new)
            _try_set(body, "model_name", model_name)
            self._models[new] = attached
        return new

    # ------------------------------------------------------------------ primitives
    @staticmethod
    def _add_pad(footprint, pad) -> None:
        if pad.polygon and not polygon_is_rectangle(pad.polygon):
            # Genuinely custom outline: a region-backed custom pad.
            hole_points = None
            if pad.hole > 0:
                cx = pad.x + pad.drill_offset[0]
                cy = pad.y + pad.drill_offset[1]
                r = pad.hole / 2.0
                hole_points = [[(cx - r, cy - r), (cx + r, cy + r)]]
            footprint.add_custom_pad(
                designator=pad.designator,
                position_mils=(pad.x, pad.y),
                outline_points_mils=pad.polygon,
                layer=pad.layer,
                hole_points_mils=hole_points,
            )
            return

        if pad.polygon:
            # The outline is a plain rectangle: emit a normal rectangular pad.
            # altium-monkey's PCB preview renderer cannot place a region-backed
            # custom pad (it anchors the outline at the origin), so rectangular
            # custom shapes are authored as primitive pads instead.
            xs = [p[0] for p in pad.polygon]
            ys = [p[1] for p in pad.polygon]
            # Polygon vertices are relative to the pad anchor, so the outline centre
            # has to be added back on -- using it directly would stack every such
            # pad on the footprint origin.
            centre_x = pad.x + (min(xs) + max(xs)) / 2.0
            centre_y = pad.y + (min(ys) + max(ys)) / 2.0
            footprint.add_pad(
                designator=pad.designator,
                position_mils=(centre_x, centre_y),
                width_mils=max(max(xs) - min(xs), 0.1),
                height_mils=max(max(ys) - min(ys), 0.1),
                layer=pad.layer,
                shape=am.PadShape.RECTANGLE,
                rotation_degrees=pad.rotation_degrees,
                hole_size_mils=pad.hole,
                plated=pad.plated,
            )
            return

        kwargs = dict(
            designator=pad.designator,
            position_mils=(pad.x, pad.y),
            width_mils=max(pad.width, 0.1),
            height_mils=max(pad.height, 0.1),
            layer=pad.layer,
            shape=pad.shape,
            rotation_degrees=pad.rotation,
        )
        if pad.hole > 0:
            kwargs["hole_size_mils"] = pad.hole
            kwargs["plated"] = pad.plated
            kwargs["hole_positive_tolerance_mils"] = 1.0
            kwargs["hole_negative_tolerance_mils"] = 1.0
        if pad.slot_points and len(pad.slot_points) == 2:
            # Altium's slot_length is the overall length including the round ends,
            # so the straight part between the two stored points is added to the
            # drill diameter.
            (ax, ay), (bx, by) = pad.slot_points
            axis = math.hypot(bx - ax, by - ay)
            kwargs["hole_shape"] = am.PadHoleShape.SLOT
            kwargs["slot_length_mils"] = max(axis + pad.hole, pad.hole)
            kwargs["slot_rotation_degrees"] = math.degrees(math.atan2(by - ay, bx - ax))
        if pad.shape == am.PadShape.ROUNDED_RECTANGLE:
            # 50% radius reproduces the stadium shape of an oval pad.
            kwargs["corner_radius_percent"] = 50
        footprint.add_pad(**kwargs)

    @staticmethod
    def _add_arc(footprint, arc) -> None:
        if arc.radius <= 0:
            return
        start, end = arc.end_angle, arc.start_angle
        if abs(start - end) < 0.01 or abs(abs(start - end) - 360.0) < 0.01:
            footprint.add_arc(
                center_mils=(arc.cx, arc.cy), radius_mils=arc.radius,
                start_angle_degrees=0.0, end_angle_degrees=360.0,
                width_mils=max(arc.width, MIN_TRACK_WIDTH), layer=arc.layer,
            )
            return
        footprint.add_arc(
            center_mils=(arc.cx, arc.cy), radius_mils=arc.radius,
            start_angle_degrees=arc.start_angle, end_angle_degrees=arc.end_angle,
            width_mils=max(arc.width, MIN_TRACK_WIDTH), layer=arc.layer,
        )

    @staticmethod
    def _add_circle(footprint, circle) -> None:
        if circle.radius <= 0:
            return
        width = max(circle.width, MIN_TRACK_WIDTH)
        # A zero-width CIRCLE in EasyEDA is a solid disc; express it as a thin ring
        # so it stays a legal Altium primitive.
        footprint.add_arc(
            center_mils=(circle.cx, circle.cy), radius_mils=circle.radius,
            start_angle_degrees=0.0, end_angle_degrees=360.0,
            width_mils=width, layer=circle.layer,
        )

    @staticmethod
    def _add_rect(footprint, rect: FpRect) -> None:
        # Normalise to opposite corners so a mirrored anchor still traces correctly.
        x0, x1 = sorted((rect.x, rect.x + rect.dx))
        y0, y1 = sorted((rect.y, rect.y + rect.dy))
        if rect.width <= 0:
            footprint.add_fill((x0, y0), (x1, y1), layer=rect.layer)
            return
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        width = max(rect.width, MIN_TRACK_WIDTH)
        for start, end in zip(corners, corners[1:] + corners[:1]):
            footprint.add_track(start, end, width_mils=width, layer=rect.layer)

    @staticmethod
    def _add_region(footprint, region) -> None:
        points = [(float(x), float(y)) for x, y in region.points]
        if len(points) < 3:
            return
        try:
            footprint.add_region(
                outline_points_mils=points,
                layer=region.layer,
                kind=am.PcbRegionKind.COPPER,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("region skipped: %s", exc)

    @staticmethod
    def _add_hole(footprint, hole) -> None:
        if hole.radius <= 0:
            return
        diameter = max(hole.radius * 2.0, 0.1)
        footprint.add_pad(
            designator="",
            position_mils=(hole.x, hole.y),
            width_mils=diameter,
            height_mils=diameter,
            layer=am.PcbLayer.MULTI_LAYER,
            shape=am.PadShape.CIRCLE,
            hole_size_mils=diameter,
            plated=False,
        )

    @staticmethod
    def _add_text(footprint, text) -> None:
        if not text.text:
            return
        footprint.add_text(
            text=text.text,
            position_mils=(text.x, text.y),
            height_mils=max(text.height, 10.0),
            layer=text.layer,
            rotation_degrees=text.rotation,
        )

    def _add_step_model(self, footprint, name: str, step_data: bytes, step=None):
        """Embed the STEP payload and reference it from a component body.

        The body is placed on the footprint origin with no offset: Altium centres an
        embedded 3D body on the component origin by default, and EasyEDA's
        ``c_origin`` is an offset inside the package rather than a placement the
        viewer expects. Rotation is kept, because some packages (SMD chips, USB-C
        connectors) ship the STEP rotated relative to the land pattern.
        """
        model = self.lib.add_embedded_model(
            name=f"{name}.step",
            model_data=step_data,
            model_source="EasyEDA/LCSC",
        )
        kwargs = {}
        if step is not None and (step.rotation_x or step.rotation_y or step.rotation_z):
            kwargs = dict(
                rotation_x_degrees=step.rotation_x,
                rotation_y_degrees=step.rotation_y,
                rotation_z_degrees=step.rotation_z,
            )
        body = footprint.add_embedded_3d_model(model, name=name, **kwargs)
        return (model, body)

    # ------------------------------------------------------------------ output
    @staticmethod
    def _height(fp: FootprintData) -> float:
        """Rough body height in mils, used for the footprint HEIGHT parameter."""
        heights = []
        if fp.step is not None and fp.step.z > 0:
            heights.append(fp.step.z)
        if fp.holes:
            heights.append(max(h.radius * 2.0 for h in fp.holes))
        if heights:
            return max(heights)
        if fp.pads:
            return max(p.height for p in fp.pads)
        return 0.0

    def footprint_names(self) -> list[str]:
        """Current footprint names, including any renames applied afterwards."""
        return list(self._by_name)

    def footprint(self, name: str):
        return self._by_name.get(name)

    def save(self, path) -> None:
        self.lib.save(path)

    def render_footprint(self, name: str, options=None) -> str:
        fp = self._by_name.get(name)
        if fp is None:
            return ""
        try:
            return fp.to_svg(options)
        except Exception as exc:  # noqa: BLE001
            log.warning("footprint render failed for %s: %s", name, exc)
            return ""
