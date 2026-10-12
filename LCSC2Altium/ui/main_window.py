"""Main application window."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QAction, QIcon, QKeySequence, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CheckBox as FluentCheckBox,
    ComboBox as FluentComboBox,
    InfoBar as FluentInfoBar,
    InfoBarPosition,
    LineEdit as FluentLineEdit,
    MessageBox as FluentMessageBox,
    PrimaryPushButton as FluentPrimaryPushButton,
    ProgressBar as FluentProgressBar,
    PushButton as FluentPushButton,
    RadioButton as FluentRadioButton,
    SpinBox as FluentSpinBox,
)

from ..builders.library import (
    ComponentEntry,
    LibraryResult,
    build_library,
    load_component,
    verify_library,
)
from ..constants import ATTRIBUTION, normalize_lcsc_list
from ..easyeda_client import EasyedaAccessDeniedError, EasyedaClient, EasyedaError
from ..preview import render_footprint_data, render_symbol_data
from . import theme as theme_mod
from .step_viewer import VTK_AVAILABLE, StepViewer
from .widgets import ComponentListWidget, LabeledField, SvgViewport

log = logging.getLogger(__name__)

VIEW_FOOTPRINT = "footprint"
VIEW_MODEL = "3dmodel"

# Content selection uses check boxes so several kinds of content can be written at
# once. Keys are stable identifiers; the labels are what the user sees.
CONTENT_SYMBOLS = "symbols"
CONTENT_FOOTPRINTS = "footprints"
CONTENT_MODELS = "models"

CONTENT_ITEMS = (
    (CONTENT_SYMBOLS, "Symbols"),
    (CONTENT_FOOTPRINTS, "Footprints"),
    (CONTENT_MODELS, "3D models"),
)
DEFAULT_CONTENT = (CONTENT_SYMBOLS, CONTENT_FOOTPRINTS)

CONTENT_HINTS = {
    CONTENT_SYMBOLS: "Write the schematic symbols into the .SchLib",
    CONTENT_FOOTPRINTS: "Write the PCB footprints into the .PcbLib",
    CONTENT_MODELS: "Download the STEP bodies and embed them in the footprints",
}

PROXY_DIRECT = "direct"
PROXY_SYSTEM = "system"
PROXY_MANUAL = "manual"


def _show_message(parent: QWidget, title: str, message: str) -> None:
    FluentMessageBox(title, message, parent).exec()


def _show_warning(parent: QWidget, title: str, message: str) -> None:
    FluentInfoBar.warning(
        title,
        message,
        isClosable=True,
        duration=6000,
        position=InfoBarPosition.TOP_RIGHT,
        parent=parent,
    )


class DownloadIssuesDialog(QDialog):
    """Dismissible, non-modal details window for component download issues."""

    def __init__(self, issues: list[str], parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("DownloadIssuesDialog")
        self.setWindowTitle("Download issues")
        self.resize(620, 420)
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        heading = QLabel("Some downloads need attention", self)
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)

        details = QTextBrowser(self)
        details.setObjectName("DownloadIssuesText")
        details.setOpenExternalLinks(False)
        details.setPlainText(
            "Some components or optional 3D models could not be downloaded:\n\n"
            + "\n".join(issues)
        )
        layout.addWidget(details, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_button = QPushButton("Close", self)
        close_button.setObjectName("DownloadIssuesCloseButton")
        close_button.clicked.connect(self.close)
        footer.addWidget(close_button)
        layout.addLayout(footer)


def content_selection(symbols: bool, footprints: bool, models: bool) -> set[str]:
    """Normalise the three check boxes into a set of keys."""
    selected = set()
    if symbols:
        selected.add(CONTENT_SYMBOLS)
    if footprints or models:
        # A 3D body cannot exist without the footprint that carries it.
        selected.add(CONTENT_FOOTPRINTS)
    if models:
        selected.add(CONTENT_MODELS)
    return selected


def merge_component_update(existing: ComponentEntry, update: ComponentEntry) -> None:
    """Add newly requested content without discarding previously fetched assets."""
    existing.info = update.info
    if update.include_symbol:
        existing.symbol = update.symbol
    if update.include_footprint and update.footprint is not None:
        existing.footprint = update.footprint
    if update.include_model and update.step_data is not None:
        existing.step_data = update.step_data
    if update.include_model:
        existing.model_error = update.model_error
    existing.error = update.error
    existing.symbol_name = existing.symbol_name or update.symbol_name
    existing.footprint_name = existing.footprint_name or update.footprint_name
    existing.include_symbol = existing.include_symbol or update.include_symbol
    existing.include_footprint = existing.include_footprint or update.include_footprint
    existing.include_model = existing.include_model or update.include_model


class SettingsDialog(QDialog):
    """Application settings: proxy and visual theme."""

    def __init__(self, parent: QWidget | None = None, *, theme: str = theme_mod.THEME_DARK,
                 proxy_mode: str = PROXY_SYSTEM, proxy_address: str = "",
                 proxy_port: int = 8080) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(460, 300)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        form = QWidget(self)
        form_layout = QFormLayout(form)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)
        theme_row = QWidget(self)
        theme_layout = QHBoxLayout(theme_row)
        theme_layout.setContentsMargins(0, 0, 0, 0)
        for name in theme_mod.THEMES:
            button = FluentRadioButton(name, theme_row)
            button.setTextColor(theme_mod.LIGHT.text, theme_mod.DARK.text)
            button.setChecked(name == theme)
            self._theme_group.addButton(button)
            theme_layout.addWidget(button)
        form_layout.addRow("Theme", theme_row)

        self._proxy_mode = FluentComboBox(self)
        self._proxy_mode.addItem("Direct connection", userData=PROXY_DIRECT)
        self._proxy_mode.addItem("System proxy", userData=PROXY_SYSTEM)
        self._proxy_mode.addItem("Configure proxy", userData=PROXY_MANUAL)
        mode_index = self._proxy_mode.findData(proxy_mode)
        self._proxy_mode.setCurrentIndex(mode_index if mode_index >= 0 else 1)
        form_layout.addRow("Connection", self._proxy_mode)

        self._proxy_address = FluentLineEdit(self)
        self._proxy_address.setText(proxy_address)
        self._proxy_address.setPlaceholderText("proxy.example or 192.168.1.10")
        form_layout.addRow("Proxy address", self._proxy_address)
        self._proxy_port = FluentSpinBox(self)
        self._proxy_port.setRange(1, 65535)
        self._proxy_port.setValue(proxy_port if 1 <= proxy_port <= 65535 else 8080)
        form_layout.addRow("Proxy port", self._proxy_port)
        self._proxy_mode.currentIndexChanged.connect(self._update_proxy_fields)
        self._update_proxy_fields()

        layout.addWidget(form)

        self._check_connection_button = FluentPushButton("Check connection", self)
        self._check_connection_button.clicked.connect(self._check_connection)
        self._dialog_buttons = QWidget(self)
        footer = QHBoxLayout()
        footer.addWidget(self._check_connection_button)
        footer.addStretch(1)
        button_layout = QHBoxLayout(self._dialog_buttons)
        button_layout.setContentsMargins(0, 0, 0, 0)
        cancel_button = FluentPushButton("Cancel", self._dialog_buttons)
        cancel_button.clicked.connect(self.reject)
        ok_button = FluentPrimaryPushButton("OK", self._dialog_buttons)
        ok_button.setDefault(True)
        ok_button.clicked.connect(lambda: self._accept_settings())
        button_layout.addWidget(cancel_button)
        button_layout.addWidget(ok_button)
        footer.addWidget(self._dialog_buttons)
        layout.addLayout(footer)
        self._connection_thread: QThread | None = None
        self._connection_worker: ConnectionTestWorker | None = None

    def _update_proxy_fields(self) -> None:
        manual = self._proxy_mode.currentData() == PROXY_MANUAL
        self._proxy_address.setEnabled(manual)
        self._proxy_port.setEnabled(manual)

    def _accept_settings(self) -> None:
        if self._proxy_mode.currentData() == PROXY_MANUAL:
            address = self._proxy_address.text().strip()
            try:
                parsed = urlsplit(f"//{address}")
                invalid = (
                    not address
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.port is not None
                    or parsed.path
                    or parsed.query
                    or parsed.fragment
                    or any(char.isspace() for char in address)
                )
            except ValueError:
                invalid = True
            if invalid:
                _show_message(
                    self,
                    "Invalid proxy address",
                    "Enter a hostname or IP address only. Set the port in the separate port field.",
                )
                return
        self.accept()

    def _check_connection(self) -> None:
        if self._connection_thread is not None:
            return
        self._check_connection_button.setEnabled(False)
        self._dialog_buttons.setEnabled(False)
        self._proxy_mode.setEnabled(False)
        self._proxy_address.setEnabled(False)
        self._proxy_port.setEnabled(False)

        worker = ConnectionTestWorker(
            proxy_mode=str(self._proxy_mode.currentData()),
            proxy_address=self._proxy_address.text().strip(),
            proxy_port=self._proxy_port.value(),
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_connection_tested)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_connection_thread_finished)
        self._connection_thread = thread
        self._connection_worker = worker
        thread.start()

    def _on_connection_tested(self, succeeded: bool, message: str) -> None:
        title = "Connection successful" if succeeded else "Connection failed"
        if succeeded:
            _show_message(self, title, message)
        else:
            _show_message(self, title, message)

    def _on_connection_thread_finished(self) -> None:
        self._connection_thread = None
        self._connection_worker = None
        self._check_connection_button.setEnabled(True)
        self._dialog_buttons.setEnabled(True)
        self._proxy_mode.setEnabled(True)
        self._update_proxy_fields()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._connection_thread is not None:
            event.ignore()
            return
        super().closeEvent(event)

    def values(self) -> tuple[str, str, str, int]:
        checked = self._theme_group.checkedButton()
        theme = checked.text() if checked is not None else theme_mod.THEME_DARK
        return (
            theme,
            str(self._proxy_mode.currentData()),
            self._proxy_address.text().strip(),
            self._proxy_port.value(),
        )


# ======================================================================================
# Workers
# ======================================================================================
class ConnectionTestWorker(QObject):
    """Tests the configured route to the EasyEDA component API."""

    finished = pyqtSignal(bool, str)

    def __init__(self, *, proxy_mode: str, proxy_address: str, proxy_port: int) -> None:
        super().__init__()
        self._proxy_mode = proxy_mode
        self._proxy_address = proxy_address
        self._proxy_port = proxy_port

    @pyqtSlot()
    def run(self) -> None:
        try:
            with EasyedaClient(
                timeout=10,
                retries=0,
                proxy_mode=self._proxy_mode,
                proxy_address=self._proxy_address,
                proxy_port=self._proxy_port,
            ) as client:
                client.check_connection()
        except (EasyedaError, ValueError) as exc:
            self.finished.emit(False, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("connection test failed unexpectedly")
            self.finished.emit(False, f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(
            True,
            "Connection to the EasyEDA component API is working with the selected settings.",
        )


class DownloadWorker(QObject):
    """Fetches parts off the GUI thread."""

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(list, list)   # entries, errors
    failed = pyqtSignal(str)

    def __init__(self, parts: list[str], content: set[str], *,
                 proxy_mode: str = PROXY_SYSTEM, proxy_address: str = "",
                 proxy_port: int = 8080,
                 existing_entries: dict[str, ComponentEntry] | None = None) -> None:
        super().__init__()
        self._parts = parts
        self._content = set(content)
        self._proxy_mode = proxy_mode
        self._proxy_address = proxy_address.strip()
        self._proxy_port = proxy_port
        self._existing_entries = {
            lcsc.upper(): entry
            for lcsc, entry in (existing_entries or {}).items()
        }

    @pyqtSlot()
    def run(self) -> None:
        entries: list[ComponentEntry] = []
        errors: list[str] = []
        want_symbol = CONTENT_SYMBOLS in self._content
        want_step = CONTENT_MODELS in self._content
        # The STEP model UUID is part of the footprint payload. Always fetch and
        # parse the footprint when models are requested, even if a caller passes
        # an un-normalized selection set.
        want_fp = CONTENT_FOOTPRINTS in self._content or want_step

        try:
            with EasyedaClient(
                proxy_mode=self._proxy_mode,
                proxy_address=self._proxy_address,
                proxy_port=self._proxy_port,
            ) as client:
                total = len(self._parts)
                for index, lcsc in enumerate(self._parts, start=1):
                    self.progress.emit(index, total, lcsc)
                    try:
                        existing = self._existing_entries.get(lcsc.upper())
                        if want_step and existing is not None and existing.footprint is not None:
                            entry = ComponentEntry(
                                info=existing.info,
                                symbol=existing.symbol,
                                footprint=existing.footprint,
                                symbol_name=existing.symbol_name,
                                footprint_name=existing.footprint_name,
                                include_symbol=want_symbol,
                                include_footprint=want_fp,
                                include_model=True,
                                step_data=existing.step_data,
                            )
                            model_ref = existing.footprint.step
                            access_denied = False
                            if entry.step_data is None and model_ref is not None:
                                try:
                                    entry.step_data = client.download_step(model_ref.uuid)
                                except EasyedaAccessDeniedError as exc:
                                    entry.model_error = str(exc)
                                    access_denied = True
                                except EasyedaError as exc:
                                    entry.model_error = str(exc)
                            elif entry.step_data is None:
                                entry.model_error = "the cached footprint has no 3D model reference"
                            if entry.step_data is None:
                                errors.append(
                                    f"{lcsc} (3D model): "
                                    f"{entry.model_error or 'the STEP model was not downloaded'}"
                                )
                            entries.append(entry)
                            if access_denied:
                                self._append_skipped_components(errors, index, total)
                                break
                            continue

                        entry = load_component(
                            client, lcsc,
                            want_footprint=want_fp,
                            want_step=want_step,
                        )
                        entry.include_symbol = want_symbol
                        entry.include_footprint = want_fp
                        entry.include_model = want_step
                        if want_step and entry.step_data is None:
                            if entry.model_error:
                                reason = entry.model_error
                            elif entry.footprint is None:
                                reason = "no footprint was returned"
                            elif entry.footprint.step is None:
                                reason = "the footprint has no 3D model reference"
                            else:
                                reason = "the STEP model was not downloaded"
                            entry.model_error = reason
                            errors.append(f"{lcsc} (3D model): {reason}")
                        entries.append(entry)
                    except EasyedaAccessDeniedError as exc:
                        errors.append(f"{lcsc}: {exc}")
                        self._append_skipped_components(errors, index, total)
                        break
                    except EasyedaError as exc:
                        errors.append(f"{lcsc}: {exc}")
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{lcsc}: {type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return

        self.finished.emit(entries, errors)

    @staticmethod
    def _append_skipped_components(errors: list[str], current: int, total: int) -> None:
        skipped = total - current
        if skipped:
            errors.append(
                f"Stopped after EasyEDA denied access; {skipped} remaining "
                "component(s) were not requested. Enable your VPN or configure "
                "a working proxy in Settings, then retry."
            )


class SaveWorker(QObject):
    """Writes the library off the GUI thread."""

    progress = pyqtSignal(int, int)
    phase = pyqtSignal(str)
    finished = pyqtSignal(object, list)   # LibraryResult, problems
    failed = pyqtSignal(str)

    def __init__(self, entries: list[ComponentEntry], target: Path, name: str,
                 include_symbols: bool, include_footprints: bool,
                 include_models: bool) -> None:
        super().__init__()
        self._entries = entries
        self._target = target
        self._name = name
        self._symbols = include_symbols
        self._footprints = include_footprints
        self._models = include_models

    @pyqtSlot()
    def run(self) -> None:
        try:
            total = max(len(self._entries), 1)
            def on_progress(current: int, total_items: int) -> None:
                self.progress.emit(current, total_items)
            started = time.perf_counter()
            result = build_library(
                self._entries, self._target, self._name,
                include_symbols=self._symbols,
                include_footprints=self._footprints,
                include_models=self._models,
                progress_callback=on_progress,
            )
            build_seconds = time.perf_counter() - started
            self.phase.emit("Verifying generated libraries…")
            verify_started = time.perf_counter()
            problems = verify_library(result)
            verify_seconds = time.perf_counter() - verify_started
            log.info(
                "Library save timings: build=%.3fs, verification=%.3fs, total=%.3fs",
                build_seconds,
                verify_seconds,
                build_seconds + verify_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("library build failed")
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(result, problems)


# ======================================================================================
# Window
# ======================================================================================
class MainWindow(QMainWindow):
    def _library_file(self, *, symbol: bool) -> Path | None:
        target = self._path_input.text().strip()
        name = self._name_input.text().strip()
        if not target or not name:
            return None
        safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in name).strip("_")
        safe = safe or "Library"
        suffix = ".SchLib" if symbol else ".PcbLib"
        return Path(target) / f"{safe}{suffix}"

    def _open_in_altium(self, path: Path | None, *, what: str) -> None:
        if path is None:
            _show_message(self, "Open in AD", f"Save the library first to open the {what} file in Altium Designer.")
            return
        if not path.exists():
            _show_message(self, "Open in AD", f"{path.name} has not been written yet. Save the library first.")
            return
        try:
            os.startfile(path)
        except Exception as exc:  # noqa: BLE001
            _show_message(self, "Open in AD", f"Could not open {path.name} in Altium Designer:\n{exc}")

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("LCSC2Altium — LCSC to Altium component library")
        self.setWindowIcon(QIcon(str(Path(__file__).resolve().parents[2] / "logo.png")))
        self.resize(1380, 920)

        self._settings_path = Path.cwd() / "settings.json"
        self._legacy_settings_path = Path.home() / ".lcsc2altium" / "settings.json"
        self._settings = self._load_settings()
        if "proxy_mode" not in self._settings:
            legacy_proxy = self._settings.get("proxy_http") or self._settings.get("proxy_https")
            if legacy_proxy:
                try:
                    legacy_parts = urlsplit(legacy_proxy)
                    if legacy_parts.hostname:
                        self._settings["proxy_mode"] = PROXY_MANUAL
                        self._settings["proxy_address"] = legacy_parts.hostname
                        self._settings["proxy_port"] = str(
                            legacy_parts.port or (443 if legacy_parts.scheme == "https" else 8080)
                        )
                except ValueError:
                    log.warning("Ignoring invalid proxy URL in previous settings")
        self._entries: list[ComponentEntry] = []
        self._selected: ComponentEntry | None = None
        self._colors = theme_mod.DARK
        self._theme = self._settings.get("theme", theme_mod.THEME_DARK)
        self._proxy_mode = self._settings.get("proxy_mode", PROXY_SYSTEM)
        if self._proxy_mode not in (PROXY_DIRECT, PROXY_SYSTEM, PROXY_MANUAL):
            self._proxy_mode = PROXY_SYSTEM
        self._proxy_address = self._settings.get("proxy_address", "")
        try:
            self._proxy_port = int(self._settings.get("proxy_port", "8080"))
        except ValueError:
            self._proxy_port = 8080
        self._thread: QThread | None = None
        self._save_thread: QThread | None = None
        self._worker: object | None = None
        self._save_worker: object | None = None

        self._build_ui()
        self._apply_theme(self._theme)

    # ================================================================== construction
    def _build_ui(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(10)
        outer.addWidget(self._build_theme_bar())

        splitter = QSplitter(Qt.Orientation.Horizontal, central)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([460, 900])
        outer.addWidget(splitter, 1)

        self.setCentralWidget(central)
        self._status = self.statusBar()
        self._status.showMessage(ATTRIBUTION)

        save_action = QAction(self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self._on_save)
        self.addAction(save_action)

    def _build_theme_bar(self) -> QWidget:
        bar = QFrame(self)
        bar.setObjectName("Toolbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.setSpacing(14)

        logo_path = Path(__file__).resolve().parents[2] / "logo.png"
        logo = QLabel(bar)
        logo.setObjectName("AppLogo")
        logo.setFixedSize(40, 40)
        pixmap = QPixmap(str(logo_path))
        if pixmap.isNull():
            log.warning("Could not load application logo from %s", logo_path)
        else:
            logo.setPixmap(
                pixmap.scaled(
                    logo.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            logo.setAccessibleName("LCSC2Altium logo")
        layout.addWidget(logo)

        title = QLabel("LCSC2Altium", bar)
        title.setObjectName("SectionTitle")
        layout.addWidget(title)
        layout.addStretch(1)

        self._settings_button = FluentPushButton("Settings", bar)
        self._settings_button.clicked.connect(self._open_settings)
        layout.addWidget(self._settings_button)

        hint = QLabel("EasyEDA / LCSC data", bar)
        hint.setObjectName("Hint")
        layout.addWidget(hint)
        return bar

    # ------------------------------------------------------------------ left panel
    def _build_left_panel(self) -> QWidget:
        panel = QWidget(self)
        panel.setMinimumWidth(390)
        panel.setMaximumWidth(580)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        form = QGroupBox("Download", panel)
        form_layout = QVBoxLayout(form)
        form_layout.setSpacing(9)

        # LCSC part numbers
        self._lcsc_input = FluentLineEdit(form)
        self._lcsc_input.setPlaceholderText("C25804, C84681, C1525 …")
        self._lcsc_input.setToolTip(
            "One or more LCSC part numbers, separated by commas or spaces.\n"
            "Example:  C25804, C84681"
        )
        self._lcsc_input.returnPressed.connect(self._on_download)
        form_layout.addWidget(LabeledField("LCSC part number", self._lcsc_input, form))

        # Destination folder
        path_row = QWidget(form)
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(8)
        self._path_input = FluentLineEdit(path_row)
        self._path_input.setPlaceholderText("Folder for the generated library")
        self._path_input.setReadOnly(True)
        browse = FluentPushButton("Browse…", path_row)
        browse.clicked.connect(self._on_browse)
        path_layout.addWidget(self._path_input, 1)
        path_layout.addWidget(browse)
        form_layout.addWidget(LabeledField("Library path", path_row, form))

        # Library name
        self._name_input = FluentLineEdit(form)
        self._name_input.setText("LCSC_Library")
        form_layout.addWidget(LabeledField("Library name", self._name_input, form))

        # Content selection (check boxes: several kinds can be written at once)
        content_row = QWidget(form)
        content_layout = QHBoxLayout(content_row)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)
        self._content_boxes: dict[str, FluentCheckBox] = {}
        for key, label in CONTENT_ITEMS:
            box = FluentCheckBox(label, content_row)
            box.setChecked(key in DEFAULT_CONTENT)
            box.setToolTip(CONTENT_HINTS[key])
            self._content_boxes[key] = box
            content_layout.addWidget(box)
        content_layout.addStretch(1)
        form_layout.addWidget(LabeledField("Content", content_row, form))

        self._download_button = FluentPrimaryPushButton("Download in library", form)
        self._download_button.clicked.connect(self._on_download)
        form_layout.addWidget(self._download_button)

        self._progress = FluentProgressBar(form)
        self._progress.setVisible(False)
        self._progress.setTextVisible(False)
        form_layout.addWidget(self._progress)

        layout.addWidget(form)

        # Queued components
        list_box = QGroupBox("Components in library", panel)
        list_layout = QVBoxLayout(list_box)
        list_layout.setSpacing(6)
        self._list = ComponentListWidget(list_box)
        self._list.currentItemChanged.connect(self._on_selection_changed)
        self._list.removeRequested.connect(self._on_remove_requested)
        list_layout.addWidget(self._list, 1)

        self._list_hint = QLabel("Right-click a component to remove it.", list_box)
        self._list_hint.setObjectName("Hint")
        list_layout.addWidget(self._list_hint)

        layout.addWidget(list_box, 1)
        return panel

    # ------------------------------------------------------------------ right panel
    def _build_right_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # ---- Symbol block
        symbol_box = QGroupBox("Symbol", panel)
        symbol_layout = QVBoxLayout(symbol_box)
        symbol_layout.setSpacing(8)
        self._symbol_view = SvgViewport(symbol_box)
        symbol_layout.addWidget(self._symbol_view, 1)

        self._symbol_name = FluentLineEdit(symbol_box)
        self._symbol_name.setPlaceholderText("Symbol name")
        self._symbol_name.editingFinished.connect(self._on_symbol_name_edited)
        self._symbol_open_button = FluentPushButton("Open in AD", symbol_box)
        self._symbol_open_button.clicked.connect(lambda: self._open_in_altium(self._library_file(symbol=True), what="symbol"))
        name_row = QWidget(symbol_box)
        name_layout = QHBoxLayout(name_row)
        name_layout.setContentsMargins(0, 0, 0, 0)
        name_layout.setSpacing(8)
        name_layout.addWidget(self._symbol_name, 1)
        name_layout.addWidget(self._symbol_open_button)
        symbol_layout.addWidget(LabeledField("Symbol name", name_row, symbol_box))
        layout.addWidget(symbol_box, 3)

        # ---- Footprint block
        fp_box = QGroupBox("Footprint", panel)
        fp_layout = QVBoxLayout(fp_box)
        fp_layout.setSpacing(8)

        mode_row = QWidget(fp_box)
        mode_layout = QHBoxLayout(mode_row)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(12)
        caption = QLabel("View", mode_row)
        caption.setMinimumWidth(80)
        mode_layout.addWidget(caption)

        self._view_group = QButtonGroup(mode_row)
        self._view_group.setExclusive(True)
        self._view_keys: dict[FluentRadioButton, str] = {}
        for key, label in ((VIEW_FOOTPRINT, "Footprint"), (VIEW_MODEL, "3D model")):
            button = FluentRadioButton(label, mode_row)
            button.setTextColor(theme_mod.LIGHT.text, theme_mod.DARK.text)
            button.setChecked(key == VIEW_FOOTPRINT)
            self._view_group.addButton(button)
            self._view_keys[button] = key
            mode_layout.addWidget(button)
            button.toggled.connect(
                lambda checked, k=key: checked and self._set_footprint_view(k)
            )
        mode_layout.addStretch(1)
        if not VTK_AVAILABLE:
            note = QLabel("(install vtk + cadquery for 3D)", mode_row)
            note.setObjectName("Hint")
            mode_layout.addWidget(note)
        fp_layout.addWidget(mode_row)

        self._stack_holder = QWidget(fp_box)
        stack_layout = QVBoxLayout(self._stack_holder)
        stack_layout.setContentsMargins(0, 0, 0, 0)
        stack_layout.setSpacing(0)
        self._footprint_view = SvgViewport(self._stack_holder)
        self._step_view = StepViewer(self._stack_holder)
        stack_layout.addWidget(self._footprint_view)
        stack_layout.addWidget(self._step_view)
        self._step_view.setVisible(False)
        fp_layout.addWidget(self._stack_holder, 1)

        self._footprint_name = FluentLineEdit(fp_box)
        self._footprint_name.setPlaceholderText("Footprint name")
        self._footprint_name.editingFinished.connect(self._on_footprint_name_edited)
        self._footprint_open_button = FluentPushButton("Open in AD", fp_box)
        self._footprint_open_button.clicked.connect(lambda: self._open_in_altium(self._library_file(symbol=False), what="footprint"))
        fp_name_row = QWidget(fp_box)
        fp_name_layout = QHBoxLayout(fp_name_row)
        fp_name_layout.setContentsMargins(0, 0, 0, 0)
        fp_name_layout.setSpacing(8)
        fp_name_layout.addWidget(self._footprint_name, 1)
        fp_name_layout.addWidget(self._footprint_open_button)
        fp_layout.addWidget(LabeledField("Footprint name", fp_name_row, fp_box))
        layout.addWidget(fp_box, 3)

        # ---- Save
        self._save_button = FluentPrimaryPushButton("Save", panel)
        self._save_button.clicked.connect(self._on_save)
        self._save_progress = FluentProgressBar(panel)
        self._save_progress.setVisible(False)
        self._save_progress.setTextVisible(False)
        layout.addWidget(self._save_button, 0)
        layout.addWidget(self._save_progress)

        attribution = QLabel(ATTRIBUTION, panel)
        attribution.setObjectName("Hint")
        attribution.setWordWrap(True)
        layout.addWidget(attribution)
        return panel

    # ================================================================== theming
    def _load_settings(self) -> dict[str, str]:
        for path in (self._settings_path, self._legacy_settings_path):
            try:
                if not path.exists():
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return {str(key): str(value) for key, value in payload.items()}
                log.warning("Ignoring invalid settings format in %s", path)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not load settings from %s: %s", path, exc)
        return {}

    def _save_settings(self) -> None:
        try:
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            self._settings_path.write_text(
                json.dumps(self._settings, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not save settings: %s", exc)

    def _open_settings(self) -> None:
        dialog = SettingsDialog(
            self,
            theme=self._theme,
            proxy_mode=self._proxy_mode,
            proxy_address=self._proxy_address,
            proxy_port=self._proxy_port,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        theme, proxy_mode, proxy_address, proxy_port = dialog.values()
        self._theme = theme
        self._proxy_mode = proxy_mode
        self._proxy_address = proxy_address
        self._proxy_port = proxy_port
        self._settings = {
            "theme": self._theme,
            "proxy_mode": self._proxy_mode,
            "proxy_address": self._proxy_address,
            "proxy_port": str(self._proxy_port),
        }
        self._save_settings()
        self._apply_theme(self._theme)
        self._status.showMessage("Settings saved")

    def _apply_theme(self, name: str) -> None:
        self._theme = name
        colors = theme_mod.apply(QApplication.instance(), name)
        self._colors = colors
        self._symbol_view.set_background("#FFFFFF")
        self._footprint_view.set_background("#FFFFFF")
        self._step_view.set_background("#FFFFFF")
        self._refresh_previews()

    # ================================================================== input events
    def _on_browse(self) -> None:
        start = self._path_input.text() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Library folder", start)
        if folder:
            self._path_input.setText(folder)

    def _content_selection(self) -> set[str]:
        """Current content check-box state as a normalised set of keys."""
        return content_selection(
            symbols=self._content_boxes[CONTENT_SYMBOLS].isChecked(),
            footprints=self._content_boxes[CONTENT_FOOTPRINTS].isChecked(),
            models=self._content_boxes[CONTENT_MODELS].isChecked(),
        )

    def _content_summary(self) -> str:
        """Human-readable list of what is currently ticked."""
        chosen = self._content_selection()
        if not chosen:
            return "nothing selected"
        order = [label for key, label in CONTENT_ITEMS if key in chosen]
        return ", ".join(order)

    def _footprint_view_mode(self) -> str:
        button = self._view_group.checkedButton()
        if button is None:
            return VIEW_FOOTPRINT
        return self._view_keys.get(button, VIEW_FOOTPRINT)

    # ================================================================== download
    def _on_download(self) -> None:
        parts = normalize_lcsc_list(self._lcsc_input.text())
        if not parts:
            _show_warning(
                self, "No parts",
                "Enter at least one LCSC part number, for example C25804.\n"
                "Several numbers can be separated by commas or spaces.",
            )
            return
        if not self._path_input.text().strip():
            _show_message(
                self, "No destination",
                "Choose the folder the library will be written to.",
            )
            return
        content = self._content_selection()
        if not content:
            _show_message(
                self, "Nothing selected",
                "Tick at least one of Symbols, Footprints or 3D models.",
            )
            return

        self._set_busy(True)
        self._progress.setRange(0, len(parts))
        self._progress.setValue(0)
        self._status.showMessage(f"Downloading {len(parts)} component(s)…")

        self._thread = QThread(self)
        self._worker = DownloadWorker(
            parts,
            content,
            proxy_mode=self._proxy_mode,
            proxy_address=self._proxy_address,
            proxy_port=self._proxy_port,
            existing_entries={entry.lcsc: entry for entry in self._entries},
        )
        # Keep a Python reference: without it the worker can be collected and the
        # thread would never run.
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_downloaded)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

    def _on_progress(self, current: int, total: int, lcsc: str) -> None:
        self._progress.setRange(0, total)
        self._progress.setValue(current)
        self._status.showMessage(f"Fetching {lcsc} ({current}/{total})…")

    def _on_downloaded(self, entries: list, errors: list) -> None:
        self._stop_thread("_thread")

        existing_by_lcsc = {entry.lcsc: entry for entry in self._entries}
        added = 0
        for entry in entries:
            existing = existing_by_lcsc.get(entry.lcsc)
            if existing is None:
                self._entries.append(entry)
                self._list.addItem(QListWidgetItem(entry.display_text()))
                existing_by_lcsc[entry.lcsc] = entry
                added += 1
                continue

            merge_component_update(existing, entry)

            for row in range(self._list.count()):
                item = self._list.item(row)
                if item is not None and self._entries[row].lcsc == entry.lcsc:
                    item.setText(existing.display_text())
                    break

        self._set_busy(False)
        message = f"Added {added} component(s)"
        if errors:
            message += f" — {len(errors)} failed"
        self._status.showMessage(message)

        if errors:
            self._download_issues_dialog = DownloadIssuesDialog(errors, self)
            self._download_issues_dialog.show()
            self._download_issues_dialog.raise_()

        if self._list.count():
            self._list.setCurrentRow(0)
        else:
            self._refresh_previews()

        if self._selected is not None:
            self._refresh_previews()

    def _on_failed(self, message: str) -> None:
        self._stop_thread("_thread")
        self._set_busy(False)
        _show_message(self, "Download failed", message)

    def _set_busy(self, busy: bool) -> None:
        self._download_button.setEnabled(not busy)
        self._progress.setVisible(busy)
        for widget in (self._lcsc_input, self._path_input, self._name_input):
            widget.setEnabled(not busy)

    def _stop_thread(self, attr: str) -> None:
        thread = getattr(self, attr, None)
        if thread is not None:
            thread.quit()
            thread.wait(5000)
            setattr(self, attr, None)
        worker_attr = attr.replace("_thread", "_worker")
        if hasattr(self, worker_attr):
            setattr(self, worker_attr, None)

    # ================================================================== list
    def _on_selection_changed(self, current: QListWidgetItem | None, _previous) -> None:
        index = self._list.row(current) if current is not None else -1
        self._selected = self._entries[index] if 0 <= index < len(self._entries) else None
        self._refresh_previews()

    def _on_remove_requested(self, payload) -> None:
        index = payload if isinstance(payload, int) else -1
        if not (0 <= index < len(self._entries)):
            return
        entry = self._entries.pop(index)
        self._list.takeItem(index)
        if self._selected is entry:
            self._selected = None

        if self._list.count():
            self._list.setCurrentRow(min(index, self._list.count() - 1))
        else:
            self._selected = None
            self._refresh_previews()

        self._status.showMessage(f"Removed {entry.lcsc}")

    # ================================================================== names
    def _on_symbol_name_edited(self) -> None:
        if self._selected is not None:
            self._selected.symbol_name = self._symbol_name.text().strip()

    def _on_footprint_name_edited(self) -> None:
        if self._selected is not None:
            self._selected.footprint_name = self._footprint_name.text().strip()

    # ================================================================== previews
    def _refresh_previews(self) -> None:
        entry = self._selected
        if entry is None:
            self._symbol_view.clear("Select a component")
            self._footprint_view.clear("Select a component")
            self._step_view.show_placeholder("Select a component")
            self._symbol_name.clear()
            self._footprint_name.clear()
            self._symbol_name.setEnabled(False)
            self._footprint_name.setEnabled(False)
            return

        self._symbol_name.setEnabled(True)
        self._symbol_name.setText(entry.symbol_name)

        self._footprint_name.setEnabled(entry.footprint is not None)
        self._footprint_name.setText(entry.footprint_name or "")

        self._symbol_view.set_markup(render_symbol_data(
            entry.symbol,
            background="#FFFFFF",
            lcsc=entry.info.lcsc,
            mpn=entry.info.mpn,
        ))

        if entry.footprint is None:
            self._footprint_view.set_placeholder("This part has no footprint")
            self._step_view.show_placeholder("This part has no 3D model")
            return

        if self._footprint_view_mode() == VIEW_FOOTPRINT:
            self._footprint_view.set_markup(render_footprint_data(
                entry.footprint, background="#FFFFFF",
            ))
        else:
            self._footprint_view.set_markup(None)
            self._refresh_model(entry)

    def _refresh_model(self, entry: ComponentEntry) -> None:
        if entry.step_data:
            name = entry.footprint_name or entry.info.lcsc
            self._step_view.load_step(entry.step_data, name)
        elif entry.model_error:
            self._step_view.show_placeholder(f"3D model unavailable: {entry.model_error}")
        elif entry.footprint is not None and entry.footprint.step is not None:
            self._step_view.show_placeholder(
                'Download with "3D models" selected to fetch the STEP body.'
            )
        else:
            self._step_view.show_placeholder("This part has no 3D model")

    def _set_footprint_view(self, mode: str) -> None:
        show_fp = mode == VIEW_FOOTPRINT
        self._footprint_view.setVisible(show_fp)
        self._step_view.setVisible(not show_fp)
        if self._selected is None:
            return
        if show_fp and self._selected.footprint is not None:
            self._footprint_view.set_markup(render_footprint_data(
                self._selected.footprint, background="#FFFFFF",
            ))
        elif not show_fp:
            self._refresh_model(self._selected)

    # ================================================================== save
    def _on_save(self) -> None:
        if not self._entries:
            _show_message(self, "Nothing to save",
                          "Download at least one component first.")
            return
        target = self._path_input.text().strip()
        if not target:
            _show_message(self, "No destination",
                          "Choose the folder the library will be written to.")
            return
        name = self._name_input.text().strip()
        if not name:
            _show_message(self, "No library name", "Enter a name for the library.")
            return
        content = self._content_selection()
        if not content:
            _show_message(
                self, "Nothing selected",
                "Tick at least one of Symbols, Footprints or 3D models.",
            )
            return

        self._save_button.setEnabled(False)
        self._status.showMessage(f"Writing library ({self._content_summary()})…")

        self._save_thread = QThread(self)
        self._save_worker = SaveWorker(
            self._entries, Path(target), name,
            include_symbols=(CONTENT_SYMBOLS in content),
            include_footprints=(CONTENT_FOOTPRINTS in content),
            include_models=(CONTENT_MODELS in content),
        )
        self._save_worker.moveToThread(self._save_thread)
        self._save_thread.started.connect(self._save_worker.run)
        self._save_worker.progress.connect(self._on_save_progress)
        self._save_worker.phase.connect(self._status.showMessage)
        self._save_worker.finished.connect(self._on_saved)
        self._save_worker.failed.connect(self._on_save_failed)
        self._save_progress.setVisible(True)
        self._save_progress.setRange(0, len(self._entries))
        self._save_progress.setValue(0)
        self._save_progress.setFormat("Saving…")
        self._save_thread.start()

    def _on_save_progress(self, current: int, total: int) -> None:
        self._save_progress.setRange(0, max(total, 1))
        self._save_progress.setValue(current)
        self._status.showMessage(f"Saving library ({current}/{total})…")

    def _on_saved(self, result: LibraryResult, problems: list) -> None:
        self._stop_thread("_save_thread")
        self._save_button.setEnabled(True)
        self._save_progress.setVisible(False)
        self._save_progress.setValue(0)

        lines = [
            f"Library written to:\n{result.libpkg.parent}",
            "",
            f"  {result.libpkg.name}",
            f"  {result.schlib.name}  ({len(result.symbols)} symbol(s))",
            f"  {result.pcblib.name}  ({len(result.footprints)} footprint(s))",
            "",
            f"Content: {self._content_summary()}",
        ]
        if CONTENT_MODELS in self._content_selection():
            embedded = sum(1 for e in self._entries if e.step_data)
            lines.append(f"3D bodies embedded: {embedded}")
        summary = "\n".join(lines)
        if problems:
            summary += "\n\nWarnings:\n" + "\n".join(problems)
        self._status.showMessage(f"Saved {result.libpkg.name}")
        _show_message(self, "Library saved", summary)

    def _on_save_failed(self, message: str) -> None:
        self._stop_thread("_save_thread")
        self._save_button.setEnabled(True)
        self._save_progress.setVisible(False)
        self._save_progress.setValue(0)
        _show_message(self, "Save failed", message)

    # ================================================================== teardown
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._step_view.close()
        self._stop_thread("_thread")
        self._stop_thread("_save_thread")
        super().closeEvent(event)
