import os

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QPushButton, QTextBrowser
from qfluentwidgets import InfoBar

from lcsc2altium.ui.main_window import (
    DownloadIssuesDialog,
    MainWindow,
    PROXY_MANUAL,
    PROXY_SYSTEM,
    SettingsDialog,
)
from lcsc2altium.ui import theme
from lcsc2altium.ui.widgets import SvgViewport


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_fluent_proxy_combo_preserves_item_data(qapp):
    theme.apply(qapp, theme.THEME_LIGHT)

    dialog = SettingsDialog(theme=theme.THEME_LIGHT)
    try:
        assert dialog._proxy_mode.currentData() == PROXY_SYSTEM

        manual_index = dialog._proxy_mode.findData(PROXY_MANUAL)
        assert manual_index >= 0
        dialog._proxy_mode.setCurrentIndex(manual_index)
        assert dialog._proxy_address.isEnabled()
        assert dialog._proxy_port.isEnabled()

        dialog._proxy_address.setText("localhost")
        dialog._proxy_port.setValue(3128)
        assert dialog.values() == (theme.THEME_LIGHT, PROXY_MANUAL, "localhost", 3128)
    finally:
        dialog.close()


def test_right_click_resets_svg_view(qapp):
    view = SvgViewport()
    view.resize(400, 300)
    view.set_markup(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<rect width="100" height="100" fill="red"/></svg>'
    )
    view.show()
    qapp.processEvents()

    position = QPointF(200, 150)
    wheel = QWheelEvent(
        position,
        QPointF(view.mapToGlobal(position.toPoint())),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )
    qapp.sendEvent(view, wheel)
    assert view._zoom > 1.0

    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
    QTest.mouseMove(view, QPoint(140, 125))
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(140, 125))
    assert view._pan_x != 0.0 or view._pan_y != 0.0

    QTest.mouseClick(view, Qt.MouseButton.RightButton, pos=QPoint(200, 150))
    assert view._zoom == 1.0
    assert view._pan_x == 0.0
    assert view._pan_y == 0.0
    view.close()


def test_missing_lcsc_part_shows_closable_nonmodal_warning(qapp):
    theme.apply(qapp, theme.THEME_DARK)
    window = MainWindow()
    window.show()
    qapp.processEvents()
    try:
        window._on_download()
        qapp.processEvents()

        notifications = window.findChildren(InfoBar)
        assert notifications
        assert not notifications[-1].isModal()
        assert notifications[-1].isClosable
        assert window.isEnabled()
    finally:
        window.close()


def test_download_issues_open_in_dismissible_nonmodal_dialog(qapp):
    theme.apply(qapp, theme.THEME_DARK)
    window = MainWindow()
    window.show()
    qapp.processEvents()
    try:
        window._on_downloaded([], ["C123: source unavailable", "C456: model missing"])
        qapp.processEvents()

        dialogs = window.findChildren(DownloadIssuesDialog)
        assert dialogs
        dialog = dialogs[-1]
        assert dialog.isVisible()
        assert not dialog.isModal()
        details = dialog.findChild(QTextBrowser)
        assert details is not None
        assert "C123: source unavailable" in details.toPlainText()
        close_button = next(
            button for button in dialog.findChildren(QPushButton)
            if button.text() == "Close"
        )
        close_button.click()
        qapp.processEvents()
        assert not dialog.isVisible()
    finally:
        window.close()
