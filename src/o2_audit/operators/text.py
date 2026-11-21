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


# ---------------------------------------------------------------------------
# Fixed-warning / step-sequence primitives (rule plans supply the exact text)
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def verbatim_match_linebreaks(text: str, expected: str) -> bool:
    """Exact verbatim match where line breaks BETWEEN characters are allowed.

    NFKC + strip first; then all whitespace (including newlines) is removed
    from both sides and the remaining inner characters must be identical.
    Missing characters, homophone substitutions or shortened variants never
    match (rule plans own the expected text).
    """
    a = _WHITESPACE_RE.sub("", annotation_nfkc_strip(text))
    b = _WHITESPACE_RE.sub("", annotation_nfkc_strip(expected))
    return a == b


def contains_verbatim_linebreaks(text: str, expected: str) -> bool:
    """Does ``text`` contain ``expected`` as a contiguous run once whitespace
    (allowed line breaks) is removed from ``text``?"""
    a = _WHITESPACE_RE.sub("", annotation_nfkc_strip(text))
    b = _WHITESPACE_RE.sub("", annotation_nfkc_strip(expected))
    return b in a


def exact_case_match(text: str, expected: str) -> bool:
    """Case-sensitive exact match after NFKC + strip only (case preserved).

    Used for English fixed sentences whose casing must match the original
    (e.g. ``Keep dry``); no lowercasing is applied anywhere.
    """
    return annotation_nfkc_strip(text) == annotation_nfkc_strip(expected)


# Step list item: either a (number, text) pair or a "1 连接底座" style string.
StepItem = object


def parse_step(item: object) -> tuple[int, str]:
    """Parse one step entry into ``(number, text)``.

    Accepts ``[1, "连接底座"]`` / ``(1, "连接底座")`` / ``{"number": 1,
    "text": "连接底座"}`` / ``"1 连接底座"``. Raises ``ValueError`` on
    anything else (never guesses a step number).
    """
    if isinstance(item, dict):
        num, text = item.get("number"), item.get("text")
    elif isinstance(item, (list, tuple)) and len(item) == 2:
        num, text = item[0], item[1]
    elif isinstance(item, str):
        m = re.fullmatch(r"\s*(\d+)\s+(.*)", annotation_nfkc_strip(item))
        if not m:
            raise ValueError(f"not a numbered step: {item!r}")
        num, text = m.group(1), m.group(2)
    else:
        raise ValueError(f"not a step entry: {item!r}")
    if isinstance(num, bool) or not isinstance(num, (int, str)):
        raise ValueError(f"invalid step number: {num!r}")
    return int(num), annotation_nfkc_strip(str(text))


def ordered_steps_match(steps: list[object], expected_texts: list[str]) -> bool:
    """Exact ordered step-sequence match: numbers must be exactly 1..N in
    order without gaps or duplicates, and each text must equal the expected
    text (NFKC + strip; wording preserved)."""
    if len(steps) != len(expected_texts):
        return False
    for i, item in enumerate(steps):
        try:
            num, text = parse_step(item)
        except ValueError:
            return False
        if num != i + 1:
            return False
        if not text_equal(text, expected_texts[i]):
            return False
    return True
