"""Shared operators: text, dates, numbers, sets, geometry.

Rule-ID independent; rule-specific parameters (thresholds, formats) live in
versioned rule plans. Operators return plain Python values and raise
``ValueError`` on invalid inputs rather than silently guessing.
"""
from o2_audit.operators.dates import (
    add_days,
    date_in_closed_interval,
    parse_date,
)
from o2_audit.operators.geometry import iou, bbox_ok
from o2_audit.operators.numbers import (
    decimal_of,
    numbers_equal,
    number_strictly_less,
)
from o2_audit.operators.sets import key_match, list_contains_model, set_equal_ignoring_order
from o2_audit.operators.text import (
    MODEL_NORMALIZATION_NOTE,
    annotation_nfkc_strip,
    material_number_equal,
    matches_pattern,
    model_normalize,
    text_equal,
)

__all__ = [
    "MODEL_NORMALIZATION_NOTE",
    "add_days",
    "annotation_nfkc_strip",
    "bbox_ok",
    "date_in_closed_interval",
    "decimal_of",
    "iou",
    "key_match",
    "list_contains_model",
    "material_number_equal",
    "matches_pattern",
    "model_normalize",
    "number_strictly_less",
    "numbers_equal",
    "parse_date",
    "set_equal_ignoring_order",
    "text_equal",
]
