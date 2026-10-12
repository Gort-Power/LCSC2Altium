"""Application entry point.

Usage:
    python main.py
"""

from __future__ import annotations

import ctypes
import logging
import sys
from pathlib import Path

# Allow running the script directly from a checkout without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QIcon  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from lcsc2altium.ui import theme  # noqa: E402
from lcsc2altium.ui.main_window import MainWindow  # noqa: E402
from lcsc2altium.ui.step_viewer import VTK_AVAILABLE, VTK_REASON  # noqa: E402


def _configure_windows_taskbar_identity() -> None:
    """Give Windows a stable taskbar identity so it uses the app's icon."""
    if sys.platform != "win32":
        return
    app_id = "LCSC2Altium.Application"
    result = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    if result != 0:
        logging.getLogger(__name__).warning(
            "Could not set Windows taskbar application ID (HRESULT 0x%08X)",
            result & 0xFFFFFFFF,
        )


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # These are chatty at INFO and only useful when debugging a conversion.
    for noisy in ("altium_monkey", "cadquery", "OCP", "vtkmodules"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    _configure_logging()
    _configure_windows_taskbar_identity()

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("LCSC2Altium")
    app.setOrganizationName("LCSC2Altium")
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / "logo.png")))

    theme.apply(app, theme.THEME_DARK)

    if not VTK_AVAILABLE:
        logging.getLogger(__name__).info("3D preview disabled — %s", VTK_REASON)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
