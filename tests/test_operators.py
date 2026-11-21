"""Shared operator semantics (规则总则 §3) — rule-ID independent."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from o2_audit.operators.dates import (
    DateParseError,
    add_days,
    date_in_closed_interval,
    interval_is_ordered,
    parse_date,
)
from o2_audit.operators.geometry import bbox_ok, iou
from o2_audit.operators.numbers import (
    decimal_of,
    numbers_equal,
    number_strictly_less,
)
from o2_audit.operators.sets import (
    key_match,
    list_contains_model,
    set_equal_ignoring_order,
)
from o2_audit.operators.text import (
    annotation_nfkc_strip,
    material_number_equal,
    matches_pattern,
    model_equal,
    model_normalize,
    text_equal,
)

B001_NUMBER_PATTERN = r"^QLAB-[0-9]{6}$"  # plan-owned pattern used through the generic helper


# -- annotation normalization vs model normalization ---------------------------


def test_annotation_nfkc_strip_fullwidth_and_spaces():
    assert annotation_nfkc_strip("ＡＢＣ１２３") == "ABC123"
    assert annotation_nfkc_strip("  hello　") == "hello"
    # inner characters are preserved (no internal trimming/case changes)
    assert annotation_nfkc_strip("a b-c") == "a b-c"


def test_model_normalize_nfkc_trim_case_space_hyphen():
    assert model_normalize("ＡＢ－１２３") == "ab123"
    assert model_normalize("  AB c-D  ") == "abcd"
    assert model_normalize("LED-100") == "led100"
    assert model_normalize("led 100") == "led100"


def test_model_normalize_preserves_non_ascii_sharp_s():
    # casefold() would turn ß into ss; the policy only ignores ASCII Latin case
    assert model_normalize("STRAßE-1") == "straße1"
    assert model_equal("STRAßE-1", "straße-1")
    assert not model_equal("STRAßE-1", "STRASSE-1")


def test_model_o_vs_0_and_i_vs_1_stay_distinct():
    assert not model_equal("O100", "0100")
    assert not model_equal("I2", "12")
    assert model_equal("LED-100", "led 100")


def test_material_number_equal_nfkc_strip_only():
    assert material_number_equal("QLAB-123456", "QLAB-123456")
    assert material_number_equal("  QLAB-123456 ", "QLAB-123456")
    assert material_number_equal("QLAB－123456", "QLAB-123456")  # full-width hyphen via NFKC
    assert not material_number_equal("QLAB-123456", "QLAB-123457")
    assert not material_number_equal("QLAB-O23456", "QLAB-023456")  # inner O != 0
    assert not text_equal("QLAB-123456", "QLAB－123457")


def test_matches_pattern():
    assert matches_pattern("QLAB-123456", B001_NUMBER_PATTERN)
    assert matches_pattern("  QLAB-123456 ", B001_NUMBER_PATTERN)
    assert not matches_pattern("qlab-123456", B001_NUMBER_PATTERN)  # prefix must be uppercase
    assert not matches_pattern("QLAB-12345", B001_NUMBER_PATTERN)
    assert not matches_pattern("QLAB-1234567", B001_NUMBER_PATTERN)
    assert not matches_pattern("QLAB-12345A", B001_NUMBER_PATTERN)
    assert matches_pattern("A123", r"^A[0-9]{3}$")
    assert not matches_pattern("ABC123", r"^A[0-9]{3}$")


# -- dates ----------------------------------------------------------------------


def test_parse_date_strict():
    assert parse_date("2026-06-15") == date(2026, 6, 15)
    assert parse_date("2024-02-29") == date(2024, 2, 29)  # real leap day
    for bad in ("2026-2-15", "2026-02-30", "2026-13-01", "20260615",
                "2026-06-15T00:00:00", "", "2026-02-29"):
        with pytest.raises(DateParseError):
            parse_date(bad)
    for obj in (True, None, 15, date(2026, 6, 15)):
        with pytest.raises(DateParseError):
            parse_date(obj)


def test_closed_interval_and_ordering_and_day_addition():
    d = date(2026, 6, 15)
    assert date_in_closed_interval(d, date(2026, 6, 15), date(2026, 12, 31))
    assert date_in_closed_interval(d, date(2026, 1, 1), date(2026, 6, 15))
    assert not date_in_closed_interval(d, date(2026, 6, 16), date(2026, 12, 31))
    assert interval_is_ordered(date(2026, 1, 1), date(2026, 1, 1))
    assert not interval_is_ordered(date(2026, 2, 1), date(2026, 1, 1))
    assert add_days(date(2026, 6, 15), 1) == date(2026, 6, 16)
    assert add_days(date(2026, 6, 15), -1) == date(2026, 6, 14)
    with pytest.raises(DateParseError):
        add_days(d, True)


# -- exact numbers --------------------------------------------------------------


def test_decimal_of_accepts_decimal_and_rejects_bool_nonfinite():
    assert decimal_of(Decimal("1.5")) == Decimal("1.5")
    assert decimal_of(100) == Decimal(100)
    assert decimal_of("99.999") == Decimal("99.999")
    assert decimal_of(0.1) == Decimal("0.1")  # via str(), not binary float
    for bad in (True, False, Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"),
                float("nan"), float("inf"), None, [1]):
        with pytest.raises(ValueError):
            decimal_of(bad)


def test_number_strictly_less_exact_thresholds():
    # B001 small-package thresholds: the endpoint itself is NOT small
    assert number_strictly_less(Decimal("99.999"), Decimal("100"))
    assert number_strictly_less("99.999", 100)
    assert not number_strictly_less(100, 100)
    assert not number_strictly_less(Decimal("100.0"), Decimal("100"))
    assert not number_strictly_less(80, 80)
    assert number_strictly_less(Decimal("79.9"), Decimal("80"))
    assert numbers_equal(Decimal("0.3"), "0.3")
    assert not numbers_equal("0.1", "0.2")


# -- sets / keys ------------------------------------------------------------------


def test_list_contains_model_exact_per_item():
    assert list_contains_model(["led-100", "PRO X"], "LED-100")
    assert list_contains_model(["ＬＥＤ－１００"], "LED-100")
    assert not list_contains_model(["LED-*"], "LED-100")  # wildcard not accepted
    assert not list_contains_model(["LED-1"], "LED-100")  # series prefix not accepted
    assert not list_contains_model(["LED-lOO"], "LED-100")  # O/0 not interchangeable


def test_key_match_and_set_equality():
    assert key_match("材料编号", "材料编号")
    assert not key_match("材料编号", "材料  编号")
    assert set_equal_ignoring_order(["a", "b"], ["b ", "a"])
    assert set_equal_ignoring_order(["a"], ["a", "a"])  # set semantics dedupe
    assert not set_equal_ignoring_order(["a"], ["a", "b"])


# -- geometry ---------------------------------------------------------------------


def test_bbox_ok_strict():
    assert bbox_ok([0.0, 0.0, 1.0, 1.0])
    assert bbox_ok((0.1, 0.2, 0.3, 0.4))
    assert not bbox_ok([0.5, 0.5, 0.5, 0.5])  # not ordered
    assert not bbox_ok([True, 0.2, 0.5, 0.9])  # bool coordinate
    assert not bbox_ok([0.1, 0.2, 1.5, 0.9])  # out of range
    assert not bbox_ok([0.1, float("nan"), 0.5, 0.9])
    assert not bbox_ok([0.1, 0.2])
    assert not bbox_ok("0.1,0.2,0.5,0.9")


def test_iou():
    a = [0.0, 0.0, 1.0, 1.0]
    assert iou(a, a) == 1.0
    assert iou(a, [0.0, 0.0, 0.5, 0.5]) == pytest.approx(0.25)
    assert iou(a, [2.0, 2.0, 3.0, 3.0]) == 0.0
    assert iou(a, [1, 0, 0, 1]) == 0.0  # invalid bbox -> 0.0
