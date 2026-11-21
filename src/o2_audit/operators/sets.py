"""Set / key matching operators (NFKC-based, rule independent)."""
from __future__ import annotations

from collections.abc import Callable

from o2_audit.operators.text import annotation_nfkc_strip, model_normalize, text_equal


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


def unique_items(items: list[str]) -> bool:
    """All items distinct under NFKC + strip (used for serial uniqueness:
    independently sold units require pairwise different serials)."""
    seen: set[str] = set()
    for x in items:
        k = annotation_nfkc_strip(str(x))
        if k in seen:
            return False
        seen.add(k)
    return True


def dict_correspondence(a: dict, b: dict, *,
                        key: Callable[[str, str], bool] = key_match,
                        value: Callable[[object, object], bool] = text_equal) -> bool:
    """Keyed correspondence of two dicts: identical key sets AND every key
    maps to a matching value.

    Used by keyed component/model and model/spec checks — deliberately NOT an
    independent set equality of keys or of values (same value set mapped to
    different keys must fail).

    Keys are matched through ``key``, never by raw equality:

    - default ``key_match``: keys are canonicalized with NFKC + strip before
      comparison, so ``" C1 "`` and ``"C1"`` are the same key while
      key-to-value pairs stay intact;
    - any other ``key``: general pairwise matching — every key of ``a`` must
      match exactly one distinct, so-far-unmatched key of ``b`` (surjective,
      injective pairing), then values of the paired keys must match.
    """
    if len(a) != len(b):
        return False
    if key is key_match:
        ca = {annotation_nfkc_strip(str(k)): a[k] for k in a}
        cb = {annotation_nfkc_strip(str(k)): b[k] for k in b}
        if set(ca.keys()) != set(cb.keys()):
            return False
        return all(value(ca[k], cb[k]) for k in ca)
    unmatched = list(b.keys())
    for ka in a:
        partners = [kb for kb in unmatched if key(str(ka), str(kb))]
        if len(partners) != 1:
            return False
        kb = partners[0]
        unmatched.remove(kb)
        if not value(a[ka], b[kb]):
            return False
    return True


def one_to_one_mapping(mapping: dict) -> bool:
    """True iff no two keys of ``mapping`` map to the same value (values
    compared under NFKC + strip). Keys of a dict are already unique; this
    checks the value side of a claimed one-to-one replacement relation."""
    seen: set[str] = set()
    for v in mapping.values():
        k = annotation_nfkc_strip(str(v))
        if k in seen:
            return False
        seen.add(k)
    return True


def multiset_equal_ignoring_duplicates(a: list[str], b: list[str]) -> bool:
    """Set equality where duplicates add no coverage ("重复出现不增加覆盖")."""
    return set_equal_ignoring_order(a, b)
