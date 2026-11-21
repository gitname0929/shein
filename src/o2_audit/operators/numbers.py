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
