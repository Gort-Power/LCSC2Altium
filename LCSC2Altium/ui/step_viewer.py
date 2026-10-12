"""Interactive STEP preview built on VTK, with CadQuery used for tessellation.

This follows the approach of https://github.com/VolkanSah/STEP-Viewer-Pro (PyQt6 +
VTK + CadQuery), reworked into an embeddable widget whose background follows the
application theme.

Both optional dependencies are probed lazily so the rest of the app keeps working when
they are unavailable.
"""

from __future__ import annotations

import logging
import os
import hashlib
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QApplication, QFrame, QLabel, QSizePolicy, QVBoxLayout, QWidget

log = logging.getLogger(__name__)

def _probe() -> tuple[bool, str]:
    """Return ``(available, reason)`` for the 3D stack."""
    if os.environ.get("LCSC2ALTIUM_DISABLE_3D"):
        return False, "disabled via LCSC2ALTIUM_DISABLE_3D"
    try:
        import vtk  # noqa: F401
        from vtkmodules.qt.QVTKRenderWindowInteractor import (  # noqa: F401
            QVTKRenderWindowInteractor,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"VTK unavailable: {exc}"
    try:
        import cadquery  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"CadQuery unavailable: {exc}"
    return True, ""


VTK_AVAILABLE, VTK_REASON = _probe()

# Platforms that never provide a real OpenGL context. VTK does not raise cleanly
# there, it crashes the process, so the widget must not be constructed at all.
_HEADLESS_PLATFORMS = {"offscreen", "minimal", "vnc"}


def gui_supports_opengl() -> bool:
    """Whether the running QPA platform can host a VTK render window."""
    if not VTK_AVAILABLE:
        return False
    app = QApplication.instance()
    if app is None:
        return False
    return app.platformName().lower() not in _HEADLESS_PLATFORMS

# Tessellation is only for the interactive preview; exported STEP data stays exact.
# A coarse deviation keeps preview meshes small enough for interactive rendering.
TESSELLATION_TOLERANCE = 0.25
_MESH_CACHE_LIMIT = 128 * 1024 * 1024
_MESH_CACHE: OrderedDict[tuple[str, float], tuple[object, object, object]] = OrderedDict()
_MESH_CACHE_SIZE = 0
_MESH_CACHE_LOCK = threading.Lock()
_TESSELLATION_LOCK = threading.Lock()


class TessellationCancelled(Exception):
    """Raised when a superseded preview is stopped."""


def _run_tessellation(data: bytes, tolerance: float, cancelled):
    with tempfile.TemporaryDirectory(prefix="lcsc2altium-step-") as temp_dir:
        input_path = Path(temp_dir) / "model.step"
        input_path.write_bytes(data)
        if cancelled.is_set():
            raise TessellationCancelled()
        vertices, triangles, colors = tessellate_step(input_path, tolerance)
    if cancelled.is_set():
        raise TessellationCancelled()
    import numpy as np

    return (
        np.ascontiguousarray(vertices, dtype=np.float64),
        np.ascontiguousarray(triangles, dtype=np.int64),
        np.ascontiguousarray(colors, dtype=np.float64),
    )


def _mesh_cache_cost(vertices, triangles, colors) -> int:
    return sum(array.nbytes for array in (vertices, triangles, colors))


def _cached_tessellation(data: bytes, tolerance: float, cancelled=None):
    global _MESH_CACHE_SIZE
    started = time.perf_counter()
    key = (hashlib.sha256(data).hexdigest(), tolerance)
    with _MESH_CACHE_LOCK:
        cached = _MESH_CACHE.get(key)
        if cached is not None:
            _MESH_CACHE.move_to_end(key)
            return cached

    cancelled = cancelled or threading.Event()
    with _TESSELLATION_LOCK:
        with _MESH_CACHE_LOCK:
            cached = _MESH_CACHE.get(key)
            if cached is not None:
                _MESH_CACHE.move_to_end(key)
                return cached
        if cancelled.is_set():
            raise TessellationCancelled()
        result = _run_tessellation(data, tolerance, cancelled)
    log.info(
        "STEP tessellation completed in %.3fs: %d vertices, %d triangles",
        time.perf_counter() - started,
        len(result[0]),
        len(result[1]),
    )

    cost = _mesh_cache_cost(*result)
    if cost <= _MESH_CACHE_LIMIT:
        with _MESH_CACHE_LOCK:
            previous = _MESH_CACHE.pop(key, None)
            if previous is not None:
                _MESH_CACHE_SIZE -= _mesh_cache_cost(*previous)
            _MESH_CACHE[key] = result
            _MESH_CACHE_SIZE += cost
            while _MESH_CACHE_SIZE > _MESH_CACHE_LIMIT:
                _, removed = _MESH_CACHE.popitem(last=False)
                _MESH_CACHE_SIZE -= _mesh_cache_cost(*removed)
    return result


class StepTessellationWorker(QObject):
    """Tessellate a STEP model away from the UI thread."""

    completed = pyqtSignal(int, object)

    def __init__(self, request_id: int, data: bytes, tolerance: float) -> None:
        super().__init__()
        self._request_id = request_id
        self._data = data
        self._tolerance = tolerance
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @pyqtSlot()
    def run(self) -> None:
        try:
            result = _cached_tessellation(self._data, self._tolerance, self._cancelled)
        except TessellationCancelled:
            result = ("cancelled",)
        except Exception as exc:  # noqa: BLE001
            log.warning("STEP tessellation failed: %s", exc)
            result = (str(exc),)
        self.completed.emit(self._request_id, result)


class StepViewer(QFrame):
    """Displays a STEP payload with an orbitable VTK camera."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ViewportFrame")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(120)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        if not VTK_AVAILABLE:
            self._message = f"3D preview unavailable.\n{VTK_REASON}"
        else:
            self._message = "No 3D model"
        self._placeholder = QLabel(self._message)
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setStyleSheet("background: transparent; border: none;")
        self._layout.addWidget(self._placeholder)

        self._widget = None
        self._renderer = None
        self._actor = None
        self._interactor = None
        self._request_id = 0
        self._threads: set[QThread] = set()
        self._workers: dict[QThread, StepTessellationWorker] = {}

        if gui_supports_opengl():
            try:
                self._build_vtk()
            except Exception as exc:  # noqa: BLE001 - no GL context, bad driver, ...
                log.warning("3D viewer unavailable: %s", exc)
                self._widget = None
                self._message = f"3D preview unavailable:\n{exc}"
                self._placeholder.setText(self._message)
        elif VTK_AVAILABLE:
            self._message = (
                "3D preview needs a real display.\n"
                "It is disabled on headless/offscreen platforms."
            )
            self._placeholder.setText(self._message)
        else:  # pragma: no cover - covered by the headless branch above
            self._placeholder.setText(self._message)

    @property
    def active(self) -> bool:
        """True when a real 3D render window is in use."""
        return self._widget is not None

    # ------------------------------------------------------------------ setup
    def _build_vtk(self) -> None:
        from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
        from vtkmodules.vtkRenderingCore import vtkRenderer

        self._widget = QVTKRenderWindowInteractor(self)
        # QVTK's size hint can be much taller than the preview area. If the
        # layout honors it when the widget is shown, the parent window grows.
        self._widget.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Ignored,
        )
        self._renderer = vtkRenderer()
        self._widget.GetRenderWindow().AddRenderer(self._renderer)
        self._layout.addWidget(self._widget, 1)
        self._widget.Initialize()
        self._widget.Start()
        self.set_background("#151A21")

    # ------------------------------------------------------------------ theming
    def set_background(self, color: str) -> None:
        if self._renderer is None:
            return
        try:
            self._renderer.SetBackground(*_hex_to_rgb(color))
            self._render()
        except Exception as exc:  # noqa: BLE001
            log.debug("background update skipped: %s", exc)

    # ------------------------------------------------------------------ content
    def show_placeholder(self, text: str) -> None:
        self._request_id += 1
        self._cancel_workers()
        self._clear_actor()
        if self._widget is not None:
            self._widget.setVisible(False)
        self._placeholder.setText(text)
        self._placeholder.setVisible(True)

    def load_step(self, data: bytes, name: str = "model") -> str | None:
        """Tessellate *data* in the background and display it when ready."""
        if self._widget is None:
            self.show_placeholder(self._message)
            return self._message or "3D viewer unavailable"

        if not data:
            self.show_placeholder("No 3D model")
            return None

        self._request_id += 1
        request_id = self._request_id
        self._cancel_workers()
        self._clear_actor()
        self._placeholder.setText("Preparing 3D model…")
        self._placeholder.setVisible(True)
        self._widget.setVisible(False)

        thread = QThread(self)
        worker = StepTessellationWorker(request_id, data, TESSELLATION_TOLERANCE)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._on_tessellation_completed)
        worker.completed.connect(thread.quit)
        worker.completed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda t=thread: self._on_thread_finished(t))
        self._threads.add(thread)
        self._workers[thread] = worker
        thread.start()
        return None

    def _cancel_workers(self) -> None:
        for worker in tuple(self._workers.values()):
            worker.cancel()

    def _on_thread_finished(self, thread: QThread) -> None:
        self._threads.discard(thread)
        self._workers.pop(thread, None)

    @pyqtSlot(int, object)
    def _on_tessellation_completed(self, request_id: int, result: object) -> None:
        if request_id != self._request_id:
            return
        if not isinstance(result, tuple) or len(result) != 3:
            message = str(result[0]) if isinstance(result, tuple) and result else "unknown error"
            self.show_placeholder(f"Could not read the 3D model:\n{message}")
            return

        vertices, triangles, colors = result
        if len(vertices) == 0 or len(triangles) == 0:
            self.show_placeholder("The 3D model contains no geometry")
            return
        try:
            self._set_geometry(vertices, triangles, colors)
        except Exception as exc:  # noqa: BLE001
            log.warning("VTK geometry upload failed: %s", exc)
            self.show_placeholder(f"3D rendering failed:\n{exc}")
            return
        self._placeholder.setVisible(False)
        self._widget.setVisible(True)
        self._resize_render_window()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Re-draw when the viewport becomes visible.

        VTK needs an explicit render after the widget is shown and resized; without
        it the canvas stays blank even though the actor is in the scene.
        """
        super().showEvent(event)
        if self._actor is not None:
            self._resize_render_window()

    def _resize_render_window(self) -> None:
        if self._widget is None or self._renderer is None:
            return
        try:
            size = self._widget.size()
            width, height = max(size.width(), 2), max(size.height(), 2)
            self._widget.GetRenderWindow().SetSize(width, height)
            self._renderer.ResetCamera()
            self._renderer.ResetCameraClippingRange()
            self._widget.GetRenderWindow().Render()
        except Exception as exc:  # noqa: BLE001
            log.debug("resize render skipped: %s", exc)

    def _set_geometry(self, vertices, triangles, colors=None) -> None:
        # vtkCellArray moved from vtkCommonCore to vtkCommonDataModel in VTK 9.x;
        # try both so the viewer works across VTK releases.
        try:
            from vtkmodules.vtkCommonDataModel import vtkCellArray
        except ImportError:  # pragma: no cover - older VTK layout
            from vtkmodules.vtkCommonCore import vtkCellArray  # type: ignore[no-redef]
        import numpy as np
        from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        started = time.perf_counter()
        vertex_array = np.ascontiguousarray(vertices, dtype=np.float64)
        triangle_array = np.ascontiguousarray(triangles, dtype=np.int64)
        points = vtkPoints()
        points.SetData(numpy_to_vtk(vertex_array, deep=True))

        cells = vtkCellArray()
        packed_cells = np.empty((len(triangle_array), 4), dtype=np.int64)
        packed_cells[:, 0] = 3
        packed_cells[:, 1:] = triangle_array
        cells.SetCells(
            len(triangle_array),
            numpy_to_vtkIdTypeArray(packed_cells.reshape(-1), deep=True),
        )

        poly = vtkPolyData()
        poly.SetPoints(points)
        poly.SetPolys(cells)

        # The STEP mesh already consists of face-local triangles. Avoid running a
        # synchronous whole-mesh normals filter on the UI thread when models are large.
        if colors is not None and len(colors) == points.GetNumberOfPoints():
            color_array = np.ascontiguousarray(
                np.clip(np.rint(np.asarray(colors) * 255), 0, 255),
                dtype=np.uint8,
            )
            scalars = numpy_to_vtk(color_array, deep=True, array_type=3)
            scalars.SetNumberOfComponents(3)
            scalars.SetName("Colors")
            poly.GetPointData().SetScalars(scalars)

        mapper = vtkPolyDataMapper()
        mapper.SetInputData(poly)

        actor = vtkActor()
        actor.SetMapper(mapper)
        if mapper.GetInput().GetPointData().GetScalars() is not None:
            # Colorized preview is intentionally unlit; calculating normals for
            # large imported meshes can stall rendering and is not needed to show
            # the source STEP materials.
            actor.GetProperty().LightingOff()
        else:
            actor.GetProperty().SetColor(0.88, 0.72, 0.32)
            actor.GetProperty().LightingOff()
        actor.GetProperty().SetSpecular(0.35)
        actor.GetProperty().SetSpecularPower(20.0)

        self._clear_actor()
        self._actor = actor
        self._renderer.AddActor(actor)
        log.info(
            "VTK mesh upload completed in %.3fs",
            time.perf_counter() - started,
        )

    def _clear_actor(self) -> None:
        if self._actor is not None and self._renderer is not None:
            self._renderer.RemoveActor(self._actor)
            self._actor = None

    def _render(self) -> None:
        if self._widget is not None:
            try:
                self._widget.GetRenderWindow().Render()
            except Exception as exc:  # noqa: BLE001
                log.debug("render skipped: %s", exc)

    # ------------------------------------------------------------------ teardown
    def close(self) -> None:
        self._request_id += 1
        self._cancel_workers()
        for thread in tuple(self._threads):
            thread.quit()
            thread.wait()
        try:
            if self._widget is not None:
                self._widget.Finalize()
        except Exception:  # noqa: BLE001
            pass
        self._widget = None
        self._renderer = None
        self._actor = None


# --------------------------------------------------------------------------------------
# Module level helpers
# --------------------------------------------------------------------------------------
def tessellate_step(path: Path, tolerance: float = TESSELLATION_TOLERANCE):
    """Convert a STEP file into ``(vertices, triangles, colors)``.

    ``colors`` holds one ``(r, g, b)`` float triple per vertex, taken from the
    per-face colours stored in the file, so the preview shows the real part instead
    of a single flat tint. When the file carries no colour information the whole
    mesh falls back to a neutral grey and is still usable.

    The coloured path reads the file through STEPCAF (which preserves the colour
    table) and triangulates face by face; CadQuery is the fallback.
    """
    coloured = _tessellate_coloured(path, tolerance)
    if coloured is not None:
        return coloured
    return _tessellate_plain(path, tolerance)


def _tessellate_coloured(path: Path, tolerance: float):
    """Per-face triangulation plus the file's colour table, or ``None`` if unavailable."""
    try:
        from OCP.BRep import BRep_Tool
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.Quantity import Quantity_ColorRGBA
        from OCP.STEPCAFControl import STEPCAFControl_Reader
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.TDF import TDF_LabelSequence
        from OCP.TDocStd import TDocStd_Document
        from OCP.TopAbs import TopAbs_ShapeEnum
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopLoc import TopLoc_Location
        from OCP.TopoDS import TopoDS
        from OCP.XCAFDoc import (
            XCAFDoc_ColorGen,
            XCAFDoc_ColorSurf,
            XCAFDoc_DocumentTool,
        )
    except Exception as exc:  # noqa: BLE001 - optional OCP modules
        log.debug("coloured tessellation unavailable: %s", exc)
        return None

    try:
        document = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
        reader = STEPCAFControl_Reader()
        reader.SetNameMode(True)
        reader.SetColorMode(True)
        reader.ReadFile(str(path))
        reader.Transfer(document)

        color_tool = XCAFDoc_DocumentTool.ColorTool_s(document.Main())
        shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
        free_shapes = TDF_LabelSequence()
        shape_tool.GetFreeShapes(free_shapes)

        default = (0.80, 0.80, 0.82)
        vertices: list[tuple[float, float, float]] = []
        triangles: list[tuple[int, int, int]] = []
        colors: list[tuple[float, float, float]] = []
        default_face = False

        for index in range(1, free_shapes.Length() + 1):
            shape = shape_tool.GetShape_s(free_shapes.Value(index))
            if shape is None:
                continue
            BRepMesh_IncrementalMesh(shape, tolerance, False, 0.5, True)

            explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
            while explorer.More():
                face = TopoDS.Face_s(explorer.Current())
                rgb = default
                for kind in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen):
                    rgba = Quantity_ColorRGBA()
                    if color_tool.GetColor(face, kind, rgba):
                        found = rgba.GetRGB()
                        rgb = (found.Red(), found.Green(), found.Blue())
                        break
                else:
                    default_face = True

                location = TopLoc_Location()
                mesh = BRep_Tool.Triangulation_s(face, location)
                if mesh is not None:
                    transform = location.Transformation()
                    offset = len(vertices)
                    for node in range(1, mesh.NbNodes() + 1):
                        point = mesh.Node(node).Transformed(transform)
                        vertices.append((point.X(), point.Y(), point.Z()))
                        colors.append(rgb)
                    for tri in range(1, mesh.NbTriangles() + 1):
                        a, b, c = mesh.Triangle(tri).Get()
                        triangles.append((offset + a - 1, offset + b - 1, offset + c - 1))
                explorer.Next()

        if not vertices:
            return None
        log.info("coloured tessellation: %d verts, %d tris, default colour used=%s",
                 len(vertices), len(triangles), default_face)
        return vertices, triangles, colors
    except Exception as exc:  # noqa: BLE001 - malformed or exotic STEP
        log.warning("coloured tessellation failed (%s); falling back", exc)
        return None


def _tessellate_plain(path: Path, tolerance: float):
    """CadQuery fallback: one mesh, one colour."""
    import cadquery as cq

    result = cq.importers.importStep(str(path))

    if isinstance(result, cq.Assembly):
        solids = [solid for child in result.children for solid in child.obj.Solids()]
    elif isinstance(result, cq.Workplane):
        solids = list(result.objects) if result.objects else [result.val()]
    elif isinstance(result, (cq.Compound, cq.Shape)):
        solids = [result]
    else:
        raise ValueError(f"Unsupported STEP payload type: {type(result).__name__}")

    if not solids:
        raise ValueError("The STEP file contains no solids")

    vertices: list = []
    triangles: list = []
    colors: list = []
    offset = 0
    tint = (0.88, 0.72, 0.32)
    for solid in solids:
        mesh_vertices, mesh_triangles = solid.tessellate(tolerance)
        vertices.extend(mesh_vertices)
        for triangle in mesh_triangles:
            triangles.append(tuple(index + offset for index in triangle))
        offset += len(mesh_vertices)
        colors.extend([tint] * len(mesh_vertices))
    return vertices, triangles, colors


def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return (
        int(value[0:2], 16) / 255.0,
        int(value[2:4], 16) / 255.0,
        int(value[4:6], 16) / 255.0,
    )
