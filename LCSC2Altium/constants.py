"""Project-wide constants: API endpoints, unit helpers and layer translation tables."""

from __future__ import annotations

import re

import altium_monkey as am

# --------------------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------------------
# The EasyEDA/JLCEDA official library ships a licence notice inside every payload that
# explicitly covers AI tooling. It requires the produced libraries to state the source.
# ``ATTRIBUTION`` is written into the Altium libraries and shown in the UI.
ATTRIBUTION = "Data source: JLCEDA/EasyEDA Official Library (lceda.cn / easyeda.com)"

# --------------------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------------------
# Every EasyEDA primitive is expressed in mils (1/1000 inch). Altium schematic and PCB
# authoring APIs used by altium-monkey also speak mils, so the pipeline stays in mils and
# only converts to millimetres where a public helper insists on it.
MIL_PER_INCH = 1000.0
MM_PER_INCH = 25.4
MIL_PER_MM = MIL_PER_INCH / MM_PER_INCH  # 39.3700787...

# 1 inch = 25.4 mm = 1000 mils.
MM2MIL = MIL_PER_INCH / MM_PER_INCH  # ~39.3700787 mils per mm
MIL2MM = 1.0 / MM2MIL                # ~0.0254 mm per mil

# EasyEDA stores schematic and footprint geometry in its own internal unit that is
# exactly 0.254 mm (1/100 inch) wide, i.e. 10 Altium mils. Verified against a SOP-16
# package: the source reports a 5.0 pad pitch which must come out as 50 mils.
EASYEDA_UNIT_MILS = 10.0


def mil_to_mm(value: float) -> float:
    """Convert mils to millimetres."""
    return value * MIL2MM


def mm_to_mil(value: float) -> float:
    """Convert millimetres to mils."""
    return value * MM2MIL


# --------------------------------------------------------------------------------------
# Remote endpoints
# --------------------------------------------------------------------------------------
EASYEDA_API_VERSION = "6.4.19.5"
EASYEDA_BASE = "https://easyeda.com"
COMPONENT_URL = EASYEDA_BASE + "/api/products/{lcsc}/components?version=" + EASYEDA_API_VERSION
SVG_URL = EASYEDA_BASE + "/api/products/{lcsc}/svgs?version=" + EASYEDA_API_VERSION
SEARCH_URL = EASYEDA_BASE + "/api/eda/product/search?keyword={lcsc}&version=" + EASYEDA_API_VERSION + "&page=1&pageSize=25"

# Bucket constant taken from smt-gl-engine (see JLC2KiCad_lib) that serves the STEP files.
STEP_BASE_URL = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{uuid}"
WRL_BASE_URL = EASYEDA_BASE + "/analyzer/api/3dmodel/{uuid}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------------------------------
# LCSC part number parsing
# --------------------------------------------------------------------------------------
_LCSC_RE = re.compile(r"^C\d+$", re.IGNORECASE)


def normalize_lcsc_list(raw: str) -> list[str]:
    """Split user input into a clean, de-duplicated list of LCSC part numbers.

    Commas, semicolons and whitespace all act as separators, so
    ``"C25804, C84681 C1525"`` yields three part numbers. A stray space inside a part
    number (``"C 25804"``) is repaired rather than rejected, since users type it often.
    """
    if not raw:
        return []
    tokens = [t for t in re.split(r"[\s,;]+", raw.strip()) if t]

    # Re-join a bare "C" with the number that follows it.
    merged: list[str] = []
    for token in tokens:
        if (merged and merged[-1].upper() == "C"
                and token.isdigit()):
            merged[-1] += token
        else:
            merged.append(token)

    seen: list[str] = []
    for token in merged:
        upper = token.upper()
        if _LCSC_RE.match(upper) and upper not in seen:
            seen.append(upper)
    return seen


def is_valid_lcsc(value: str) -> bool:
    return bool(_LCSC_RE.match(value.strip().upper()))


# --------------------------------------------------------------------------------------
# EasyEDA -> Altium layer translation (footprint)
# --------------------------------------------------------------------------------------
# EasyEDA numeric layer ids as used by footprint primitives.
EASYEDA_LAYERS: dict[str, am.PcbLayer] = {
    "1": am.PcbLayer.TOP,                    # Top copper
    "2": am.PcbLayer.BOTTOM,                 # Bottom copper
    "3": am.PcbLayer.TOP_OVERLAY,            # Top silkscreen
    "4": am.PcbLayer.BOTTOM_OVERLAY,         # Bottom silkscreen
    "5": am.PcbLayer.TOP_PASTE,              # Top paste
    "6": am.PcbLayer.BOTTOM_PASTE,           # Bottom paste
    "7": am.PcbLayer.TOP_SOLDER,             # Top solder mask
    "8": am.PcbLayer.BOTTOM_SOLDER,          # Bottom solder mask
    "10": am.PcbLayer.MECHANICAL_1,          # Board outline
    "11": am.PcbLayer.MULTI_LAYER,           # Through-hole copper
    "12": am.PcbLayer.MECHANICAL_13,         # Fabrication / component outline
    "19": am.PcbLayer.MECHANICAL_15,         # 3D body outline
    "20": am.PcbLayer.MECHANICAL_16,
    "99": am.PcbLayer.MECHANICAL_2,          # Component shape
    "100": am.PcbLayer.MECHANICAL_3,         # Pin soldering layer
    "101": am.PcbLayer.MECHANICAL_4,         # Component marking layer
}

FALLBACK_LAYER = am.PcbLayer.TOP_OVERLAY


def to_altium_layer(easyeda_layer: str) -> am.PcbLayer:
    """Translate an EasyEDA layer id to the matching Altium layer."""
    return EASYEDA_LAYERS.get(str(easyeda_layer).strip(), FALLBACK_LAYER)


# --------------------------------------------------------------------------------------
# Pins
# --------------------------------------------------------------------------------------
# Every imported pin is emitted as passive. EasyEDA stores "Input"/"Output"/
# "Undefined" inconsistently for passive parts (a chip resistor comes back as an
# input), so honouring the source value produced meaningless ERC results.
PIN_ELECTRICAL = am.PinElectrical.PASSIVE

# Schematic grid the converted symbols are aligned to. Altium's default snap is
# 50 mil and EasyEDA symbols mostly use a 50/100 mil pin pitch, so snapping keeps
# pins on the grid instead of leaving them a few mils off it.
SCHEMATIC_GRID_MILS = 50.0


def snap_to_grid(value: float, grid: float = SCHEMATIC_GRID_MILS) -> float:
    """Round *value* to the nearest multiple of *grid*."""
    if grid <= 0:
        return value
    return round(value / grid) * grid


# --------------------------------------------------------------------------------------
# EasyEDA pin electrical types -> Altium
# --------------------------------------------------------------------------------------
EASYEDA_PIN_ELECTRICAL: dict[str, am.PinElectrical] = {
    # EasyEDA 0 == "Undefined", which is what most passive parts use.
    "0": am.PinElectrical.PASSIVE,
    "1": am.PinElectrical.INPUT,
    "2": am.PinElectrical.OUTPUT,
    "3": am.PinElectrical.IO,
    "4": am.PinElectrical.POWER,
    "5": am.PinElectrical.HIZ,
    "6": am.PinElectrical.OPEN_EMITTER,
    "7": am.PinElectrical.OPEN_COLLECTOR,
}
DEFAULT_PIN_ELECTRICAL = am.PinElectrical.PASSIVE

# --------------------------------------------------------------------------------------
# Library file suffixes
# --------------------------------------------------------------------------------------
SCHLIB_SUFFIX = ".SchLib"
PCBLIB_SUFFIX = ".PcbLib"
LIBPKG_SUFFIX = ".LibPkg"

PARAM_MPN = "MPN"
PARAM_LCSC = "LCSC Part Number"
