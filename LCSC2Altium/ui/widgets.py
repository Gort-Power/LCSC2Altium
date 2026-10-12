"""Reusable widgets: an SVG preview viewport and the queued-component list."""

from __future__ import annotations

import re
from pathlib import Path

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from qfluentwidgets import ListWidget, RoundMenu
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme as theme_mod

_VIEWBOX_RE = re.compile(
    r'viewBox="([-\d.]+)[ ,]+([-\d.]+)[ ,]+([-\d.]+)[ ,]+([-\d.]+)"'
)
_WIDTH_RE = re.compile(r'\bwidth="([-\d.]+)([a-z%]*)"')
_HEIGHT_RE = re.compile(r'\bheight="([-\d.]+)(\w*)"')

def _svg_intrinsic_size(renderer: QSvgRenderer, markup: str) -> tuple[float, float]:
    """Resolve the natural size of an SVG document, falling back to its viewBox."""
    size = renderer.defaultSize()
    width, height = float(size.width()), float(size.height())
    if width >= 2 and height >= 2:
        return width, height

    match = _WIDTH_RE.search(markup[:2048])
    if match and not match.group(2).endswith("%"):
        width = float(match.group(1))
    match = _HEIGHT_RE.search(markup[:2048])
    if match and not match.group(1).endswith("%"):
        height = float(match.group(1))
    if width >= 2 and height >= 2:
        return width, height

    match = _VIEWBOX_RE.search(markup[:2048])
    if match:
        return float(match.group(3)), float(match.group(4))
    return 400.0, 300.0


class SvgViewport(QFrame):
    """Scales an SVG document to fit while preserving its aspect ratio."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ViewportFrame")
        self._markup: str = ""
        self._renderer: QSvgRenderer | None = None
        self._background: str = "#FFFFFF"
        self._placeholder = "No data"
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._drag_pos = None

        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setWordWrap(True)
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._label.setStyleSheet("background: transparent; border: none;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self._label)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(120)

    # ------------------------------------------------------------------ content
    def set_markup(self, markup: str | None) -> None:
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._drag_pos = None
        if not markup:
            self._markup = ""
            self._renderer = None
            self._update_label("No data")
            self.update()
            return
        self._markup = markup
        self._renderer = QSvgRenderer(markup.encode("utf-8"))
        if not self._renderer.isValid():
            self._renderer = None
            self._markup = ""
            self._update_label("Preview could not be rendered")
        self.update()

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        if self._renderer is None:
            self._update_label(text)

    def set_background(self, color: str) -> None:
        self._background = color
        self.update()

    def clear(self, placeholder: str = "No data") -> None:
        self.reset_view()
        self._markup = ""
        self._renderer = None
        self._placeholder = placeholder
        self._update_label(placeholder)
        self.update()

    def reset_view(self) -> None:
        """Restore the SVG preview's default zoom and pan position."""
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._drag_pos = None
        self.unsetCursor()
        self.update()

    def _update_label(self, text: str) -> None:
        self._label.setText(text)
        self._label.setVisible(True)

    # ------------------------------------------------------------------ painting
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(self._background))

        if self._renderer is None or self._renderer.defaultSize().isEmpty():
            painter.end()
            super().paintEvent(event)
            return

        margin = 8.0
        available = QRectF(
            self.rect().adjusted(int(margin), int(margin), -int(margin), -int(margin))
        )
        if available.width() <= 0 or available.height() <= 0:
            painter.end()
            super().paintEvent(event)
            return

        nat_w, nat_h = _svg_intrinsic_size(self._renderer, self._markup)
        if nat_w <= 0 or nat_h <= 0:
            nat_w, nat_h = 400.0, 300.0

        fit_scale = min(available.width() / nat_w, available.height() / nat_h)
        draw_w = nat_w * fit_scale * self._zoom
        draw_h = nat_h * fit_scale * self._zoom
        target = QRectF(
            available.x() + (available.width() - draw_w) / 2.0 + self._pan_x,
            available.y() + (available.height() - draw_h) / 2.0 + self._pan_y,
            draw_w,
            draw_h,
        )
        self._label.setVisible(False)
        self._renderer.render(painter, target)
        painter.end()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self.update()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self._renderer:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = 1.15 if delta > 0 else 1 / 1.15
        self._zoom = max(0.2, min(8.0, self._zoom * factor))
        event.accept()
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.RightButton:
            self.reset_view()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._renderer is not None:
            self._drag_pos = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._drag_pos is not None:
            delta = event.position() - self._drag_pos
            self._pan_x += delta.x()
            self._pan_y += delta.y()
            self._drag_pos = event.position()
            self.update()
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._drag_pos is not None:
            self._drag_pos = None
            self.unsetCursor()
            event.accept()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(320, 220)


class ComponentListWidget(ListWidget):
    """Scrollable list of queued components with a right-click delete action."""

    removeRequested = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.setMinimumHeight(120)

    def _show_menu(self, pos) -> None:
        item = self.itemAt(pos)
        if item is None:
            return
        menu = RoundMenu(parent=self)
        remove_action = menu.addAction("Remove")
        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen is remove_action:
            # Emit the row, not item data: rows shift as items are removed and a
            # stored row would go stale.
            self.removeRequested.emit(self.row(item))


def svg_to_png(markup: str, size: QSize, background: str = "#151A21") -> QPixmap | None:
    """Render *markup* into a pixmap (used for list thumbnails if needed)."""
    renderer = QSvgRenderer(markup.encode("utf-8"))
    if not renderer.isValid():
        return None
    image = QImage(size, QImage.Format.Format_ARGB32)
    image.fill(QColor(background))
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, float(size.width()), float(size.height())))
    painter.end()
    return QPixmap.fromImage(image)


def write_step_file(data: bytes, directory: Path, stem: str) -> Path:
    """Persist a STEP payload to *directory* so the 3D viewer can open it by path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.step"
    if not path.exists() or path.stat().st_size != len(data):
        path.write_bytes(data)
    return path


class LabeledField(QWidget):
    """A caption + input row used by the form."""

    def __init__(self, label: str, widget: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        caption = QLabel(label, self)
        caption.setMinimumWidth(120)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(caption)
        layout.addWidget(widget, 1)
        self.caption = caption
        self.widget = widget


def make_placeholder(text: str, color: str) -> QLabel:
    label = QLabel(text)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {color}; background: transparent; border: none;")
    return label


__all__ = [
    "ComponentListWidget",
    "LabeledField",
    "SvgViewport",
    "make_placeholder",
    "svg_to_png",
    "write_step_file",
    "theme_mod",
    "QListWidgetItem",
]
