"""Dark / Light / System theming for the whole application."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PyQt6.QtWidgets import QApplication
from qfluentwidgets import Theme as FluentTheme, setTheme, setThemeColor

THEME_DARK = "Dark"
THEME_LIGHT = "Light"
THEME_SYSTEM = "System"
THEMES = (THEME_DARK, THEME_LIGHT, THEME_SYSTEM)


class ThemeColors:
    """Colour tokens shared by the stylesheet and the preview widgets."""

    def __init__(
        self,
        *,
        window: str,
        surface: str,
        surface_alt: str,
        border: str,
        text: str,
        text_muted: str,
        accent: str,
        accent_text: str,
        danger: str,
        canvas: str,
        viewport: str,
    ) -> None:
        self.window = window
        self.surface = surface
        self.surface_alt = surface_alt
        self.border = border
        self.text = text
        self.text_muted = text_muted
        self.accent = accent
        self.accent_text = accent_text
        self.danger = danger
        self.canvas = canvas
        self.viewport = viewport


DARK = ThemeColors(
    window="#151A21",
    surface="#1D242E",
    surface_alt="#232C38",
    border="#33404F",
    text="#E6EDF3",
    text_muted="#93A1B0",
    accent="#3D8BFD",
    accent_text="#FFFFFF",
    danger="#E5534B",
    canvas="#0F1318",
    viewport="#151A21",
)

LIGHT = ThemeColors(
    window="#F2F4F7",
    surface="#FFFFFF",
    surface_alt="#EAEEF3",
    border="#C9D1DA",
    text="#1B2129",
    text_muted="#5A6673",
    accent="#1F6FEB",
    accent_text="#FFFFFF",
    danger="#CF222E",
    canvas="#FFFFFF",
    viewport="#FAFBFC",
)

SYSTEM_FALLBACK = DARK
_FLUENT_FONT_LOADED = False
log = logging.getLogger(__name__)


def colors_for(theme: str, system_dark: bool) -> ThemeColors:
    if theme == THEME_DARK:
        return DARK
    if theme == THEME_LIGHT:
        return LIGHT
    return DARK if system_dark else LIGHT


def stylesheet(c: ThemeColors) -> str:
    """Style app-specific containers while leaving Fluent controls to the library."""
    return f"""
QMainWindow, QDialog {{ background-color: {c.window}; color: {c.text}; }}

QLabel {{ background: transparent; }}
QLabel#Hint {{ color: {c.text_muted}; }}
QLabel#SectionTitle {{
    color: {c.text};
    font-weight: 600;
    padding: 2px 0 2px 0;
}}
QLabel#ErrorText {{ color: {c.danger}; }}

QFrame#Card {{
    background-color: {c.surface};
    border: 1px solid {c.border};
    border-radius: 8px;
}}
QFrame#Toolbar {{
    background-color: {c.surface};
    border: 1px solid {c.border};
    border-radius: 8px;
}}
QFrame#ViewportFrame {{
    background-color: {c.viewport};
    border: 1px solid {c.border};
    border-radius: 8px;
}}
QDialog#DownloadIssuesDialog {{
    background-color: {c.window};
    color: {c.text};
}}
QTextBrowser#DownloadIssuesText {{
    background-color: {c.surface};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 8px;
    padding: 10px;
    selection-background-color: {c.accent};
    selection-color: {c.accent_text};
}}
QPushButton#DownloadIssuesCloseButton {{
    background-color: {c.accent};
    color: {c.accent_text};
    border: 1px solid {c.accent};
    border-radius: 6px;
    padding: 7px 18px;
}}
QPushButton#DownloadIssuesCloseButton:hover {{
    background-color: {c.accent};
}}

QGroupBox {{
    background-color: {c.surface};
    border: 1px solid {c.border};
    border-radius: 10px;
    margin-top: 16px;
    padding: 12px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    background-color: {c.window};
}}

QStatusBar {{ background-color: {c.surface}; color: {c.text_muted}; }}
QStatusBar::item {{ border: none; }}
QToolTip {{
    background-color: {c.surface};
    color: {c.text};
    border: 1px solid {c.border};
    padding: 5px;
}}

QSplitter::handle {{ background-color: {c.border}; }}
QSplitter::handle:horizontal {{ width: 4px; }}
QSplitter::handle:vertical {{ height: 4px; }}
"""


def is_system_dark(app: QApplication) -> bool:
    """Best-effort detection of the desktop colour scheme."""
    scheme = Qt.ColorScheme.Dark if hasattr(Qt, "ColorScheme") else None  # type: ignore[attr-defined]
    try:
        hint = app.styleHints().colorScheme()
        if scheme is not None and hint == scheme:
            return True
        if scheme is not None and hint == Qt.ColorScheme.Light:  # type: ignore[attr-defined]
            return False
    except Exception:  # noqa: BLE001 - older Qt builds
        pass
    window = app.palette().color(QPalette.ColorRole.Window)
    return window.lightness() < 128


def _load_fluent_font(app: QApplication) -> None:
    """Register the installed Windows Fluent UI font so Qt can resolve it."""
    global _FLUENT_FONT_LOADED
    if _FLUENT_FONT_LOADED:
        return

    family = "Segoe UI"
    if family not in QFontDatabase.families():
        font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeui.ttf"
        if font_path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            families = QFontDatabase.applicationFontFamilies(font_id)
            if font_id < 0 or family not in families:
                log.warning("Could not register the installed Segoe UI font")
            else:
                _FLUENT_FONT_LOADED = True

    if family in QFontDatabase.families():
        app.setFont(QFont(family, 9))


def apply(app: QApplication, theme: str) -> ThemeColors:
    """Apply *theme* to *app* and return the colour tokens in use."""
    _load_fluent_font(app)
    system_dark = is_system_dark(app)
    colors = colors_for(theme, system_dark)
    fluent_theme = {
        THEME_DARK: FluentTheme.DARK,
        THEME_LIGHT: FluentTheme.LIGHT,
        THEME_SYSTEM: FluentTheme.AUTO,
    }.get(theme, FluentTheme.DARK)
    setTheme(fluent_theme)
    setThemeColor(colors.accent)

    palette = QPalette()
    if colors is DARK:
        palette.setColor(QPalette.ColorRole.Window, QColor(colors.window))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(colors.text))
        palette.setColor(QPalette.ColorRole.Base, QColor(colors.surface))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(colors.surface_alt))
        palette.setColor(QPalette.ColorRole.Text, QColor(colors.text))
        palette.setColor(QPalette.ColorRole.Button, QColor(colors.surface_alt))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(colors.text))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(colors.accent))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(colors.accent_text))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(colors.surface))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor(colors.text))
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(colors.text_muted))
    else:
        palette.setColor(QPalette.ColorRole.Window, QColor(colors.window))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(colors.text))
        palette.setColor(QPalette.ColorRole.Base, QColor(colors.surface))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(colors.surface_alt))
        palette.setColor(QPalette.ColorRole.Text, QColor(colors.text))
        palette.setColor(QPalette.ColorRole.Button, QColor(colors.surface_alt))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(colors.text))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(colors.accent))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(colors.accent_text))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(colors.surface))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor(colors.text))
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(colors.text_muted))

    app.setPalette(palette)
    app.setStyleSheet(stylesheet(colors))
    return colors
