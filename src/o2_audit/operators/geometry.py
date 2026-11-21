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
