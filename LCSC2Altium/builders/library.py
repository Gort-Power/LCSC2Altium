"""High level orchestration: fetch a part, author the Altium libraries, write files."""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..constants import LIBPKG_SUFFIX, PCBLIB_SUFFIX, SCHLIB_SUFFIX
from ..easyeda_client import EasyedaClient, EasyedaError
from ..easyeda_parser import parse_component_info, parse_footprint, parse_symbol
from ..models import ComponentInfo, FootprintData, SymbolData
from .libpkg import write_libpkg
from .pcblib_builder import PcbLibBuilder
from .schlib_builder import SchLibBuilder

log = logging.getLogger(__name__)


@dataclass(slots=True)
class LibraryResult:
    """Outcome of writing one library."""

    schlib: Path
    pcblib: Path
    libpkg: Path
    symbols: list[str] = field(default_factory=list)
    footprints: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ComponentEntry:
    """A component queued for insertion into the library.

    Holds everything the UI needs for previews plus the user-editable names that get
    applied on save.
    """

    info: ComponentInfo
    symbol: SymbolData
    footprint: FootprintData | None = None
    symbol_name: str = ""
    footprint_name: str = ""
    include_symbol: bool = True
    include_footprint: bool = True
    include_model: bool = False
    step_data: bytes | None = None
    model_error: str = ""
    error: str = ""

    @property
    def lcsc(self) -> str:
        return self.info.lcsc

    def display_text(self) -> str:
        label = self.info.mpn or self.info.lcsc
        extra = self.info.package or self.info.value
        return f"{self.info.lcsc} — {label}" + (f" ({extra})" if extra else "")


def load_component(
    client: EasyedaClient,
    lcsc: str,
    *,
    want_footprint: bool = True,
    want_step: bool = False,
) -> ComponentEntry:
    """Fetch and parse one LCSC part. Raises :class:`EasyedaError` on failure."""
    result = client.fetch_component(lcsc)

    # Enrich with manufacturer / stock data when the search endpoint has it.
    try:
        result["__search__"] = client.find_exact(lcsc, lcsc) or {}
    except EasyedaError:
        result["__search__"] = {}

    info = parse_component_info(result)
    if not info.lcsc:
        info.lcsc = lcsc.upper()

    symbol = parse_symbol(result, info.default_symbol_name)
    footprint = parse_footprint(result, info.default_footprint_name) if want_footprint else None

    entry = ComponentEntry(
        info=info,
        symbol=symbol,
        footprint=footprint,
        symbol_name=info.default_symbol_name,
        footprint_name=info.default_footprint_name if footprint else "",
    )

    if want_step and footprint is not None and footprint.step is not None:
        try:
            entry.step_data = client.download_step(footprint.step.uuid)
        except EasyedaError as exc:
            log.warning("Skipping STEP download for %s: %s", lcsc, exc)
            entry.step_data = None
            entry.model_error = str(exc)
    return entry


def build_library(
    entries: list[ComponentEntry],
    target_dir: Path,
    library_name: str,
    *,
    include_symbols: bool = True,
    include_footprints: bool = True,
    include_models: bool = False,
    progress_callback: callable | None = None,
) -> LibraryResult:
    """Author and write ``<name>.SchLib``, ``<name>.PcbLib`` and ``<name>.LibPkg``."""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    safe = "".join(c if (c.isalnum() or c in "_-") else "_" for c in library_name).strip("_")
    safe = safe or "Library"

    schlib_path = target_dir / f"{safe}{SCHLIB_SUFFIX}"
    pcblib_path = target_dir / f"{safe}{PCBLIB_SUFFIX}"
    libpkg_path = target_dir / f"{safe}{LIBPKG_SUFFIX}"

    sch = SchLibBuilder()
    pcb = PcbLibBuilder()
    symbols: list[str] = []
    footprints: list[str] = []
    total = max(len(entries), 1)

    assemble_started = time.perf_counter()
    for index, entry in enumerate(entries, start=1):
        if entry.error:
            log.warning("skipping %s: %s", entry.lcsc, entry.error)
            if progress_callback is not None:
                progress_callback(index, total)
            continue

        written_symbol = ""
        if include_symbols:
            written_symbol = sch.add_symbol(
                entry.symbol,
                lcsc=entry.info.lcsc,
                mpn=entry.info.mpn,
            )
            # Honour a user-edited symbol name.
            if entry.symbol_name and entry.symbol_name != written_symbol:
                written_symbol = sch.rename_symbol(written_symbol, entry.symbol_name)
            symbols.append(written_symbol)

        if include_footprints and entry.footprint is not None:
            written_fp = pcb.add_footprint(
                entry.footprint,
                step_data=entry.step_data if include_models else None,
                lcsc=entry.info.lcsc,
            )
            if entry.footprint_name and entry.footprint_name != written_fp:
                written_fp = pcb.rename_footprint(written_fp, entry.footprint_name)
            footprints.append(written_fp)

            if include_symbols and written_symbol and written_fp:
                sch.link_footprint(
                    written_symbol, written_fp,
                    description=entry.footprint.description or "",
                )
        if progress_callback is not None:
            progress_callback(index, total)

    assemble_seconds = time.perf_counter() - assemble_started
    save_started = time.perf_counter()
    sch.save(schlib_path)
    sch_save_seconds = time.perf_counter() - save_started
    save_started = time.perf_counter()
    pcb.save(pcblib_path)
    pcb_save_seconds = time.perf_counter() - save_started
    save_started = time.perf_counter()
    write_libpkg(libpkg_path, schlib_path, pcblib_path)
    package_save_seconds = time.perf_counter() - save_started
    log.info(
        "Library build timings: assemble=%.3fs, SchLib save=%.3fs, "
        "PcbLib save=%.3fs, LibPkg save=%.3fs",
        assemble_seconds,
        sch_save_seconds,
        pcb_save_seconds,
        package_save_seconds,
    )

    return LibraryResult(
        schlib=schlib_path,
        pcblib=pcblib_path,
        libpkg=libpkg_path,
        symbols=symbols,
        footprints=footprints,
    )


def verify_library(result: LibraryResult) -> list[str]:
    """Re-open the written files and report anything that failed to round-trip."""
    import altium_monkey as am

    problems: list[str] = []
    try:
        names = am.AltiumSchLib.get_symbol_names(result.schlib)
        missing = set(result.symbols) - set(names)
        if missing:
            problems.append(f"SchLib missing symbols: {sorted(missing)}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"SchLib unreadable: {exc}")

    try:
        names = am.AltiumPcbLib.get_footprint_names(result.pcblib)
        missing = set(result.footprints) - set(names)
        if missing:
            problems.append(f"PcbLib missing footprints: {sorted(missing)}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"PcbLib unreadable: {exc}")

    return problems


def scratch_dir(prefix: str = "lcsc2altium-") -> Path:
    """Temporary directory used while staging STEP payloads."""
    return Path(tempfile.mkdtemp(prefix=prefix))


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
