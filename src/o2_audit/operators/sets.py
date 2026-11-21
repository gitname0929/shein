"""Set / key matching operators (NFKC-based, rule independent)."""
from __future__ import annotations

from o2_audit.operators.text import annotation_nfkc_strip, model_normalize


def key_match(a: str, b: str) -> bool:
    """Dictionary/matrix key equality: NFKC + strip, case preserved."""
    return annotation_nfkc_strip(a) == annotation_nfkc_strip(b)


def set_equal_ignoring_order(a: list[str], b: list[str]) -> bool:
    ka = {annotation_nfkc_strip(x) for x in a}
    kb = {annotation_nfkc_strip(x) for x in b}
    return ka == kb


def list_contains_model(models: list[str], target: str) -> bool:
    """Does the model list contain the target under model normalization?

    Exact per-item model comparison; no wildcard/series-prefix acceptance.
    """
    t = model_normalize(target)
    return any(model_normalize(m) == t for m in models)
