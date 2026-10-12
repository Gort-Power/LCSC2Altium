"""Unit conversion and LCSC part-number parsing."""

import pytest

from lcsc2altium import constants
from lcsc2altium.constants import normalize_lcsc_list, is_valid_lcsc


def test_easyeda_unit_is_ten_mils():
    # Verified against a SOP-16: a 5.0 pad pitch must become 50 Altium mils.
    assert constants.EASYEDA_UNIT_MILS == 10.0


def test_mil_mm_roundtrip():
    # 1 inch = 25.4 mm = 1000 mils
    assert constants.mm_to_mil(25.4) == pytest.approx(1000.0)
    assert constants.mil_to_mm(1000.0) == pytest.approx(25.4)
    assert constants.mm_to_mil(1.0) == pytest.approx(39.3700787, rel=1e-6)
    assert constants.mil_to_mm(50.0) == pytest.approx(1.27)


def test_normalize_accepts_separators_and_case():
    assert normalize_lcsc_list("C25804, C84681 C1525") == ["C25804", "C84681", "C1525"]
    assert normalize_lcsc_list("c25804") == ["C25804"]


def test_normalize_drops_duplicates_and_junk():
    assert normalize_lcsc_list("C25804 C25804") == ["C25804"]
    assert normalize_lcsc_list("junk") == []
    assert normalize_lcsc_list("") == []


def test_normalize_repairs_inner_spaces():
    # A user typing "C 25804" still gets a usable part number.
    assert normalize_lcsc_list("C 25804") == ["C25804"]


def test_normalize_accepts_semicolons():
    assert normalize_lcsc_list("C1;C2; C3") == ["C1", "C2", "C3"]


def test_is_valid_lcsc():
    assert is_valid_lcsc("c25804")
    assert not is_valid_lcsc("25804")
    assert not is_valid_lcsc("XC25804")
