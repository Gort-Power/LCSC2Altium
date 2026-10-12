"""STEP viewer behaviour that does not need an OpenGL context."""
import os
import subprocess
import sys
import textwrap

import pytest

PROJECT = "D:/LCSC2Altium"


def _run_snippet(body: str, **env) -> subprocess.CompletedProcess:
    """Run *body* as top-level code in a fresh interpreter."""
    inner = textwrap.dedent(body).strip()
    script = (
        "import os, sys\n"
        f"os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
        f"sys.path.insert(0, {PROJECT!r})\n"
        "from PyQt6.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        + inner
        + "\nprint('OK')\n"
    )
    environ = dict(os.environ)
    environ.update(env)
    return subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, timeout=240, env=environ)


def test_step_viewer_degrades_when_3d_is_disabled():
    result = _run_snippet(
        """
        from lcsc2altium.ui.step_viewer import StepViewer, VTK_AVAILABLE
        assert VTK_AVAILABLE is False, "3D should be off via the env var"
        viewer = StepViewer()
        assert viewer.active is False
        err = viewer.load_step(b"not a step file", "X")
        assert err, "load_step must explain why it cannot render"
        assert "disabled" in err
        viewer.show_placeholder("Select a component")
        assert viewer._placeholder.text() == "Select a component"
        viewer.close()
        """,
        LCSC2ALTIUM_DISABLE_3D="1",
    )
    assert "OK" in result.stdout, result.stderr


def test_step_viewer_reports_placeholder_when_disabled():
    result = _run_snippet(
        """
        from lcsc2altium.ui.step_viewer import StepViewer
        viewer = StepViewer()
        assert viewer.active is False
        # With no render window every load reports the reason and shows the text.
        for payload in (b"", b"garbage"):
            err = viewer.load_step(payload, "X")
            assert err and "3D preview" in err
            assert "3D preview" in viewer._placeholder.text()
        viewer.close()
        """,
        LCSC2ALTIUM_DISABLE_3D="1",
    )
    assert "OK" in result.stdout, result.stderr


def test_headless_platform_is_detected():
    result = _run_snippet(
        """
        from lcsc2altium.ui.step_viewer import gui_supports_opengl
        from PyQt6.QtWidgets import QApplication
        assert QApplication.instance().platformName().lower() == "offscreen"
        assert gui_supports_opengl() is False
        """
    )
    assert "OK" in result.stdout, result.stderr


def test_reset_camera_clipping_range_uses_the_renderer():
    """Regression: ResetCameraClippingRange lives on vtkRenderer, not the window.

    Calling it on the render window raised AttributeError, which is what stopped the
    3D body from ever appearing even though the STEP was embedded correctly.
    """
    result = _run_snippet(
        """
        import re, pathlib
        src = pathlib.Path(r'D:/LCSC2Altium/lcsc2altium/ui/step_viewer.py').read_text(encoding='utf-8')
        assert 'GetRenderWindow().ResetCameraClippingRange' not in src, (
            'ResetCameraClippingRange must be called on the renderer')
        assert src.count('self._renderer.ResetCameraClippingRange()') >= 1
        """
    )
    assert "OK" in result.stdout, result.stderr


def test_vtk_cell_array_import_survives_vtk9():
    """Regression: vtkCellArray moved to vtkCommonDataModel in VTK 9."""
    result = _run_snippet(
        """
        try:
            from vtkmodules.vtkCommonDataModel import vtkCellArray
        except ImportError:
            from vtkmodules.vtkCommonCore import vtkCellArray
        from vtkmodules.vtkCommonDataModel import vtkPolyData
        from vtkmodules.vtkCommonCore import vtkIdList, vtkPoints
        pts = vtkPoints(); pts.InsertNextPoint(0, 0, 0)
        ids = vtkIdList(); ids.InsertNextId(0)
        cells = vtkCellArray(); cells.InsertNextCell(ids)   # VTK 9.6 overload
        poly = vtkPolyData(); poly.SetPoints(pts); poly.SetPolys(cells)
        assert poly.GetNumberOfPoints() == 1
        """
    )
    assert "OK" in result.stdout, result.stderr


def test_step_tessellation_cache_reuses_mesh(monkeypatch):
    import lcsc2altium.ui.step_viewer as step_viewer

    import numpy as np

    calls = []

    def tessellate(path, tolerance):
        calls.append((path.read_bytes(), tolerance))
        return ([[0.0, 0.0, 0.0]], [[0, 0, 0]], [[1.0, 0.0, 0.0]])

    monkeypatch.setattr(step_viewer, "tessellate_step", tessellate)
    payload = b"unique-cache-regression-payload"

    first = step_viewer._cached_tessellation(payload, 0.05)
    second = step_viewer._cached_tessellation(payload, 0.05)

    assert all(left is right for left, right in zip(first, second))
    assert len(calls) == 1
    assert calls[0][0] == payload


def test_cancelled_tessellation_does_not_start(monkeypatch):
    import threading

    import lcsc2altium.ui.step_viewer as step_viewer

    def unexpected_tessellation(*args, **kwargs):
        raise AssertionError("cancelled request should not start tessellation")

    monkeypatch.setattr(step_viewer, "tessellate_step", unexpected_tessellation)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(step_viewer.TessellationCancelled):
        step_viewer._cached_tessellation(
            b"cancelled-preview-payload", 0.25, cancelled
        )


def test_tessellation_callback_accepts_numpy_arrays():
    result = _run_snippet(
        """
        import numpy as np
        from lcsc2altium.ui.step_viewer import StepViewer

        class Widget:
            def setVisible(self, visible):
                self.visible = visible

        viewer = StepViewer()
        viewer._widget = Widget()
        uploaded = []
        viewer._set_geometry = lambda *geometry: uploaded.append(geometry)
        viewer._resize_render_window = lambda: None
        vertices = np.array([[0, 0, 0]], dtype=np.float64)
        triangles = np.array([[0, 0, 0]], dtype=np.int64)
        colors = np.array([[1, 0, 0]], dtype=np.float64)
        viewer._on_tessellation_completed(0, (vertices, triangles, colors))
        assert uploaded and uploaded[0][0] is vertices
        assert viewer._placeholder.isVisible() is False
        assert viewer._widget.visible is True
        viewer.close()
        """
    )
    assert "OK" in result.stdout, result.stderr


def test_vtk_mesh_upload_handles_numpy_backed_geometry():
    result = _run_snippet(
        """
        from lcsc2altium.ui.step_viewer import StepViewer

        class Renderer:
            def __init__(self):
                self.actors = []
            def AddActor(self, actor):
                self.actors.append(actor)
            def RemoveActor(self, actor):
                self.actors.remove(actor)

        viewer = StepViewer()
        # Offscreen Qt intentionally disables the interactive widget, but the
        # non-rendering VTK mesh pipeline can still be tested with a stub renderer.
        viewer._renderer = Renderer()
        viewer._set_geometry(
            [(0, 0, 0), (1, 0, 0), (0, 1, 0)],
            [(0, 1, 2)],
            [(1, 0, 0), (0, 1, 0), (0, 0, 1)],
        )
        poly = viewer._actor.GetMapper().GetInput()
        assert poly.GetNumberOfPoints() == 3
        assert poly.GetNumberOfPolys() == 1
        assert poly.GetPointData().GetScalars().GetNumberOfComponents() == 3
        viewer.close()
        """
    )
    assert "OK" in result.stdout, result.stderr


@pytest.mark.parametrize("module", [
    "lcsc2altium.constants",
    "lcsc2altium.models",
    "lcsc2altium.easyeda_parser",
    "lcsc2altium.builders.library",
    "lcsc2altium.ui.theme",
    "lcsc2altium.ui.widgets",
    "lcsc2altium.ui.main_window",
])
def test_modules_import(module):
    result = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {PROJECT!r}); import {module}"],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr
