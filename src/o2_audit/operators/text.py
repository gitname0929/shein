"""Text comparison operators (规则总则 §3).

- General text: NFKC + strip both ends; inner characters preserved.
- Model comparison: additionally ignore ASCII Latin case, half-width spaces and
  half-width hyphens. O/0 and I/1 stay distinct (no mapping is ever applied).
  Only ASCII ``A-Z`` is lowercased: ``casefold()`` is deliberately NOT used
  because it alters non-ASCII characters (e.g. ``ß`` -> ``ss``), which the
  general policy does not allow.
- Material/编号 comparison: NFKC + strip only; inner characters must match.
- ``matches_pattern`` is the generic NFKC+strip fullmatch helper; rule-specific
  patterns (e.g. the B001 material-number format) live in versioned rule plans,
  never here (shared operators are rule-ID independent).
"""
from __future__ import annotations

import re
import unicodedata

MODEL_NORMALIZATION_NOTE = (
    "型号归一化: NFKC + 去首尾空白 + 忽略拉丁大小写/半角空格/半角连字符; O/0 与 I/1 不视为等价"
)

# ASCII A-Z -> a-z only; non-ASCII letters are preserved verbatim.
_ASCII_LOWER_TABLE = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)


def annotation_nfkc_strip(value: str) -> str:
    """NFKC + strip; the only normalization allowed on annotation values."""
    return unicodedata.normalize("NFKC", value).strip()


def text_equal(a: str, b: str) -> bool:
    return annotation_nfkc_strip(a) == annotation_nfkc_strip(b)


def model_normalize(value: str) -> str:
    """Business model normalization per 规则总则 §3 (rule semantics, not a
    vendored algorithm). ASCII Latin case only; ``ß`` and other non-ASCII
    characters are never case-mapped."""
    s = annotation_nfkc_strip(value)
    s = s.replace(" ", "").replace("-", "")
    return s.translate(_ASCII_LOWER_TABLE)


def model_equal(a: str, b: str) -> bool:
    return model_normalize(a) == model_normalize(b)


def material_number_equal(a: str, b: str) -> bool:
    """编号比较: NFKC + 首尾去空白 only; inner characters must be identical
    (so ``QLAB-123456`` vs ``QLAB－123456`` after NFKC may converge, but
    ``O`` vs ``0`` inside the number never converge)."""
    return annotation_nfkc_strip(a) == annotation_nfkc_strip(b)


def matches_pattern(value: str, pattern: str) -> bool:
    """NFKC + strip, then ``re.fullmatch(pattern, ...)``.

    Generic helper shared by rules; the pattern itself is rule-specific and
    comes from the active versioned plan.
    """
    s = annotation_nfkc_strip(value)
    return bool(re.fullmatch(pattern, s))
