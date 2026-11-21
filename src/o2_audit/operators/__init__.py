"""Shared operators: text, dates, numbers, sets, geometry.

Rule-ID independent; rule-specific parameters (thresholds, formats, authorized
conversion factors) live in versioned rule plans. Operators return plain
Python values and raise ``ValueError`` on invalid inputs rather than silently
guessing.
"""
from o2_audit.operators.dates import (
    add_days,
    date_in_closed_interval,
    interval_is_ordered,
    parse_date,
)
from o2_audit.operators.geometry import (
    area_ratio,
    bbox_center,
    bbox_contains,
    bbox_height,
    bbox_ok,
    bbox_width,
    height_ratio,
    iou,
    relative_position,
)
from o2_audit.operators.numbers import (
    convert_exact,
    decimal_of,
    in_interval,
    number_strictly_less,
    numbers_equal,
    within_tolerance,
)
from o2_audit.operators.sets import (
    dict_correspondence,
    key_match,
    list_contains_model,
    multiset_equal_ignoring_duplicates,
    one_to_one_mapping,
    set_equal_ignoring_order,
    unique_items,
)
from o2_audit.operators.text import (
    MODEL_NORMALIZATION_NOTE,
    annotation_nfkc_strip,
    contains_verbatim_linebreaks,
    exact_case_match,
    material_number_equal,
    matches_pattern,
    model_normalize,
    ordered_steps_match,
    parse_step,
    text_equal,
    verbatim_match_linebreaks,
)

__all__ = [
    "MODEL_NORMALIZATION_NOTE",
    "add_days",
    "annotation_nfkc_strip",
    "area_ratio",
    "bbox_center",
    "bbox_contains",
    "bbox_height",
    "bbox_ok",
    "bbox_width",
    "contains_verbatim_linebreaks",
    "convert_exact",
    "date_in_closed_interval",
    "decimal_of",
    "dict_correspondence",
    "exact_case_match",
    "height_ratio",
    "in_interval",
    "interval_is_ordered",
    "iou",
    "key_match",
    "list_contains_model",
    "material_number_equal",
    "matches_pattern",
    "model_normalize",
    "multiset_equal_ignoring_duplicates",
    "number_strictly_less",
    "numbers_equal",
    "one_to_one_mapping",
    "ordered_steps_match",
    "parse_date",
    "parse_step",
    "relative_position",
    "set_equal_ignoring_order",
    "text_equal",
    "unique_items",
    "verbatim_match_linebreaks",
    "within_tolerance",
]
