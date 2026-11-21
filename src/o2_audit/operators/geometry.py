"""Geometry primitives for normalized bboxes.

Validation semantics mirror the public contract (``contracts.bbox_ok``); the
IoU helper is used for annotation evidence checks. Implementations here are
plain float math, not a vendored algorithm.
"""
from __future__ import annotations

import math

BBox = tuple[float, float, float, float]


def bbox_ok(b: object) -> bool:
    """0 <= x1 < x2 <= 1, 0 <= y1 < y2 <= 1; finite numbers; bool rejected."""
    if not isinstance(b, (list, tuple)) or len(b) != 4:
        return False
    for x in b:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            return False
        if x < 0 or x > 1:
            return False
    x1, y1, x2, y2 = float(b[0]), float(b[1]), float(b[2]), float(b[3])
    return x1 < x2 and y1 < y2


def iou(a: BBox, b: BBox) -> float:
    if not bbox_ok(a) or not bbox_ok(b):
        return 0.0
    inter_w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    inter_h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = inter_w * inter_h
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


# ---------------------------------------------------------------------------
# Containment, relative position and ratio primitives (rule plans own the
# thresholds; these helpers only compute deterministic geometry)
# ---------------------------------------------------------------------------


def bbox_center(b: BBox) -> tuple[float, float]:
    if not bbox_ok(b):
        raise ValueError(f"invalid bbox: {b!r}")
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def bbox_contains(outer: BBox, inner: BBox) -> bool:
    """True iff ``inner`` lies fully inside ``outer`` (edge touching allowed)."""
    if not bbox_ok(outer) or not bbox_ok(inner):
        return False
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and inner[2] <= outer[2] and inner[3] <= outer[3])


def relative_position(a: BBox, b: BBox) -> str:
    """Position of ``a`` relative to ``b`` from bbox centers.

    Same-column test: the horizontal center distance is at most half the sum
    of the widths (i.e. the boxes overlap horizontally) -> ``above``/``below``
    (or ``adjacent`` when centers coincide vertically); otherwise
    ``left``/``right``. Deterministic pure float math; no view semantics here.
    """
    ax, ay = bbox_center(a)
    bx, by = bbox_center(b)
    overlap_x = abs(ax - bx) <= (a[2] - a[0] + b[2] - b[0]) / 2.0
    overlap_y = abs(ay - by) <= (a[3] - a[1] + b[3] - b[1]) / 2.0
    if not overlap_x:
        return "left" if ax < bx else "right"
    if not overlap_y:
        return "above" if ay < by else "below"
    return "adjacent"


def bbox_height(b: BBox) -> float:
    if not bbox_ok(b):
        raise ValueError(f"invalid bbox: {b!r}")
    return b[3] - b[1]


def bbox_width(b: BBox) -> float:
    if not bbox_ok(b):
        raise ValueError(f"invalid bbox: {b!r}")
    return b[2] - b[0]


def height_ratio(mark: BBox, reference: BBox) -> float:
    """mark height / reference height; caller compares against the
    rule-plan threshold (e.g. Decimal(\"1.5\"))."""
    ref_h = bbox_height(reference)
    if ref_h <= 0:
        raise ValueError("reference height must be positive")
    return bbox_height(mark) / ref_h


def area_ratio(a: BBox, b: BBox) -> float:
    """area(a) / area(b); bounded positive floats only."""
    area_a = bbox_width(a) * bbox_height(a)
    area_b = bbox_width(b) * bbox_height(b)
    if area_b <= 0:
        raise ValueError("reference area must be positive")
    return area_a / area_b
