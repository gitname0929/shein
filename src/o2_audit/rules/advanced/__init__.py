"""Advanced rule families F01-F10 (R001-R060), 6 rules each.

Each family module owns its typed plan class, applicability functions and
deterministic evaluators. The registry (rules/registry.py) is the single
place that maps (rule_id, rule_version) -> (plan, evaluator, applicability).
"""
from __future__ import annotations

from typing import Any

from o2_audit.rules.advanced import (
    f01_evidence,
    f02_identity,
    f03_fields,
    f04_placement,
    f05_visual,
    f06_warnings,
    f07_numeric,
    f08_documents,
    f09_cross_material,
    f10_boundary,
)

FAMILIES: dict[str, Any] = {
    "F01": f01_evidence,
    "F02": f02_identity,
    "F03": f03_fields,
    "F04": f04_placement,
    "F05": f05_visual,
    "F06": f06_warnings,
    "F07": f07_numeric,
    "F08": f08_documents,
    "F09": f09_cross_material,
    "F10": f10_boundary,
}

# rule_id -> family module (official family grouping, 6 rules per family).
FAMILY_BY_RULE: dict[str, Any] = {}
for _fid, _mod in FAMILIES.items():
    for _rid in _mod.EVALUATORS:
        FAMILY_BY_RULE[_rid] = _mod

RULE_IDS: tuple[str, ...] = tuple(FAMILY_BY_RULE)

__all__ = ["FAMILIES", "FAMILY_BY_RULE", "RULE_IDS"]
