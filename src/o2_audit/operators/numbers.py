"""Exact numeric comparisons.

Numbers are compared exactly via :class:`decimal.Decimal`; no tolerance is
added anywhere. Unit conversion is NOT done here (only rule plans may specify
an explicitly authorized conversion).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


def decimal_of(value: object) -> Decimal:
    """Exact Decimal from int/float/str/Decimal; bool rejected; non-finite
    rejected (float NaN/inf and ``Decimal('NaN')`` alike)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"not a number: {value!r}")
    try:
        d = value if isinstance(value, Decimal) else Decimal(
            value if isinstance(value, (int, str)) else str(value)
        )
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"not a finite number: {value!r}") from e
    if not d.is_finite():
        raise ValueError(f"non-finite number: {value!r}")
    return d


def numbers_equal(a: object, b: object) -> bool:
    return decimal_of(a) == decimal_of(b)


def number_strictly_less(a: object, b: object) -> bool:
    """a < b exactly (used for the B001 small-package threshold: the endpoint
    itself is NOT small)."""
    return decimal_of(a) < decimal_of(b)


# ---------------------------------------------------------------------------
# Authorized unit conversion (R037-R042 style; factors come from rule plans)
# ---------------------------------------------------------------------------


def convert_exact(value: object, from_unit: object, to_unit: object,
                  factors: dict[tuple[str, str], object]) -> Decimal:
    """Exact conversion using ONLY the explicitly authorized factor table.

    ``factors`` maps ``(from_unit, to_unit)`` -> multiplier (``value * factor``
    equals the converted amount) and comes from the active versioned rule
    plan. Identical units pass through unchanged; any other pair raises
    ``ValueError`` — an unauthorized conversion is never guessed.
    """
    d = decimal_of(value)
    f_unit, t_unit = str(from_unit), str(to_unit)
    if f_unit == t_unit:
        return d
    factor = factors.get((f_unit, t_unit))
    if factor is None:
        raise ValueError(f"unauthorized unit conversion: {f_unit} -> {t_unit}")
    f = decimal_of(factor)
    if not f.is_finite() or f <= 0:
        raise ValueError(f"invalid conversion factor: {factor!r}")
    return d * f


def within_tolerance(actual: object, expected: object,
                     abs_tol: object | None = None,
                     rel_tol: object | None = None) -> bool:
    """Exact tolerance check (Decimal, no float drift): ok iff
    ``|actual - expected|`` is within the allowed band.

    - ``abs_tol``: absolute bound.
    - ``rel_tol``: relative bound against ``|expected|`` (e.g. 0.01 = 1%).
    - Both given: either bound being sufficient passes (standard OR semantics);
      endpoints are inclusive ("恰好 1% 允许").
    """
    if abs_tol is None and rel_tol is None:
        raise ValueError("within_tolerance requires abs_tol or rel_tol")
    diff = abs(decimal_of(actual) - decimal_of(expected))
    if abs_tol is not None:
        if diff <= decimal_of(abs_tol):
            return True
    if rel_tol is not None:
        r = decimal_of(rel_tol)
        if not r.is_finite() or r < 0:
            raise ValueError(f"invalid rel_tol: {rel_tol!r}")
        if diff <= r * abs(decimal_of(expected)):
            return True
    return False


def in_interval(value: object, lo: object, hi: object,
                *, closed_lo: bool = True, closed_hi: bool = True) -> bool:
    """Closed/open numeric interval membership, exact Decimal comparison."""
    d, low, high = decimal_of(value), decimal_of(lo), decimal_of(hi)
    if low > high:
        raise ValueError(f"invalid interval [{low}, {high}]")
    ge = d >= low if closed_lo else d > low
    le = d <= high if closed_hi else d < high
    return ge and le
