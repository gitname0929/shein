"""Shared evaluation helpers for the 60 advanced rules.

Every evaluator follows the 总则 decision order:

1. applicability (metadata-level, short-circuits before any gateway call);
2. rule-specific conflict/exception priority (inside family modules);
3. proven violations -> reject;
4. consequential unknowns -> ambiguous;
5. all applicable requirements satisfied -> pass.

These helpers are rule-ID independent: family modules pass rule texts/quote
keys from their typed plans. Consumes FactView + typed plan only (no
gateway/store access), keeping the semantic layer separable.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from o2_audit.domain.models import (
    Entity,
    EntityKind,
    Evidence,
    Observation,
    ObservationStatus,
    RequirementResult,
    RequirementStatus,
)
from o2_audit.operators.numbers import decimal_of
from o2_audit.runtime.facts import FactView, consolidate
from o2_audit.rules.base import RuleOutcome, RulePlan


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def req(req_id: str, status: RequirementStatus, reason_code: str, reason: str,
        evidence: list[Evidence] | None = None) -> RequirementResult:
    return RequirementResult(
        requirement_id=req_id, status=status, reason_code=reason_code,
        reason=reason, evidence=evidence or [],
        blocks_conclusion=(status in (RequirementStatus.UNKNOWN, RequirementStatus.CONFLICT)),
    )


def ev(obs: Observation | None, fact: str, rule_quote: str | None = None) -> list[Evidence]:
    """Evidence rows from an observation's provenance (never invented coords)."""
    if obs is None or obs.source is None:
        return []
    return [Evidence(
        material_id=obs.source.material_id,
        page_index=obs.source.page_index,
        fact=fact,
        bbox=list(obs.source.bbox) if obs.source.bbox else None,
        rule_quote=rule_quote,
    )]


def decide(results: list[RequirementResult], *, plan: RulePlan,
           applicability: str = "applicable",
           conflict_reason_code: str | None = None,
           conflict_reason: str | None = None,
           pass_reason: str = "全部适用要求满足",
           reject_reason: str = "存在已证实的不满足项") -> RuleOutcome:
    """Apply the shared decision order to collected requirement results."""
    conflicts = [r for r in results if r.status == RequirementStatus.CONFLICT]
    if conflicts:
        return RuleOutcome(
            applicability=applicability, label="ambiguous",
            reason_code=conflict_reason_code or conflicts[0].reason_code,
            reason=conflict_reason or conflicts[0].reason,
            results=results, evidence=conflicts[0].evidence,
        )
    violations = [r for r in results if r.status == RequirementStatus.VIOLATED]
    if violations:
        return RuleOutcome(
            applicability=applicability, label="reject",
            reason_code=violations[0].reason_code,
            reason="；".join(v.reason for v in violations[:3]) or reject_reason,
            results=results,
            evidence=[e for v in violations[:3] for e in v.evidence],
        )
    unknowns = [r for r in results if r.status == RequirementStatus.UNKNOWN]
    if unknowns:
        return RuleOutcome(
            applicability=applicability, label="ambiguous",
            reason_code=unknowns[0].reason_code,
            reason="；".join(u.reason for u in unknowns[:3]),
            results=results,
            evidence=[e for u in unknowns[:3] for e in u.evidence],
        )
    return RuleOutcome(
        applicability=applicability, label="pass", reason_code="all_requirements_satisfied",
        reason=pass_reason, results=results,
        evidence=[e for r in results for e in r.evidence][:10],
    )


def not_applicable_outcome(reason: str = "申报条件明确不在本条规则适用范围内") -> RuleOutcome:
    return RuleOutcome(
        applicability="not_applicable", label="pass", reason_code="rule_not_applicable",
        reason=reason,
        results=[req("applicability", RequirementStatus.SATISFIED,
                     "rule_not_applicable", "存在明确不满足适用条件的申报字段", [])],
        evidence=[],
    )


def applicability_unknown_outcome(missing: list[str]) -> RuleOutcome:
    return RuleOutcome(
        applicability="unknown", label="ambiguous", reason_code="applicability_unknown",
        reason=f"适用条件无法确定，缺失或非法条件: {', '.join(missing)}",
        results=[req("applicability", RequirementStatus.UNKNOWN,
                     "applicability_unknown", "适用条件未知", [])],
        evidence=[], missing_applicability_conditions=missing,
    )


# ---------------------------------------------------------------------------
# fact reading
# ---------------------------------------------------------------------------

_NON_READABLE = (ObservationStatus.UNREADABLE, ObservationStatus.INCOMPLETE_SCOPE,
                 ObservationStatus.IDENTITY_UNKNOWN)


def read(facts: FactView, entity_id: str,
         predicate: str) -> tuple[ObservationStatus, Any, Observation | None]:
    """Consolidated view of one (entity, predicate)."""
    return consolidate(facts.claims(entity_id, predicate))


def entities_of_kind_with(facts: FactView, kind: EntityKind,
                          predicate: str) -> list[tuple[Entity, ObservationStatus, Any, Observation | None]]:
    """All entities of ``kind`` that carry any claim for ``predicate``."""
    out = []
    for e in facts.entities_of_kind(kind):
        claims = facts.claims(e.entity_id, predicate)
        if not claims:
            continue
        status, value, obs = consolidate(claims)
        out.append((e, status, value, obs))
    return out


def first_target(facts: FactView, kind: EntityKind,
                 predicate: str) -> tuple[Entity | None, ObservationStatus, Any, Observation | None]:
    """First entity of ``kind`` with a claim for ``predicate`` (or None)."""
    found = entities_of_kind_with(facts, kind, predicate)
    if not found:
        return None, ObservationStatus.INCOMPLETE_SCOPE, None, None
    return found[0]


def quantity_of(value: Any) -> tuple[Decimal, str]:
    """Parse a value+unit pair: ``{"value": 500, "unit": "g"}`` or
    ``[value, unit]``. Raises ``ValueError`` on anything not a finite number
    with a unit string."""
    if isinstance(value, dict):
        raw_v, unit = value.get("value"), value.get("unit")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        raw_v, unit = value[0], value[1]
    else:
        raise ValueError(f"not a value+unit pair: {value!r}")
    if not isinstance(unit, str) or not unit.strip():
        raise ValueError(f"missing unit in {value!r}")
    try:
        return decimal_of(raw_v), unit.strip()
    except (ValueError, InvalidOperation, TypeError) as e:
        raise ValueError(f"invalid numeric value in {value!r}") from e


def read_quantity(facts: FactView, entity_id: str,
                  predicate: str) -> tuple[ObservationStatus, tuple[Decimal, str] | None, Observation | None]:
    status, value, obs = read(facts, entity_id, predicate)
    if status != ObservationStatus.READABLE:
        return status, None, obs
    try:
        return ObservationStatus.READABLE, quantity_of(value), obs
    except ValueError:
        return ObservationStatus.IDENTITY_UNKNOWN, None, obs


def field_check(facts: FactView, kind: EntityKind, predicate: str, req_id: str,
                *, check: Callable[[Any], tuple[bool, str]],
                quote: str | None = None,
                missing_violation: bool = True,
                missing_reason: str = "完整清晰检查范围内确认该字段缺失",
                unreadable_reason: str = "该字段读不清，按证据不足处理") -> RequirementResult:
    """Generic single-field scan across entities of ``kind``.

    - no claims at all            -> UNKNOWN (evidence insufficient)
    - any ``confirmed_missing``   -> violated (or unknown when the rule treats
      absence as non-conclusive, ``missing_violation=False``)
    - any non-readable status     -> UNKNOWN
    - readable values             -> ``check(value)`` decides satisfied/violated
    """
    found = entities_of_kind_with(facts, kind, predicate)
    if not found:
        return req(req_id, RequirementStatus.UNKNOWN, "target_field_unobserved",
                   "未观察到目标字段，证据不足", [])
    for _e, status, _v, obs in found:
        if status == ObservationStatus.CONFIRMED_MISSING:
            if missing_violation:
                return req(req_id, RequirementStatus.VIOLATED, "field_confirmed_missing",
                           missing_reason, ev(obs, "确认字段缺失", quote))
            return req(req_id, RequirementStatus.UNKNOWN, "field_absence_nonconclusive",
                       "字段确认缺失，但本条不能仅据此判定", ev(obs, "字段缺失", quote))
    for _e, status, _v, obs in found:
        if status in _NON_READABLE:
            return req(req_id, RequirementStatus.UNKNOWN, "field_unreadable",
                       unreadable_reason, ev(obs, "字段不可读", quote))
    for e, status, value, obs in found:
        ok, reason = check(value)
        if not ok:
            return req(req_id, RequirementStatus.VIOLATED, "field_check_failed",
                       reason, ev(obs, reason, quote))
    value_repr = found[0][2]
    return req(req_id, RequirementStatus.SATISFIED, "field_check_passed",
               f"字段核验通过（{value_repr!r}）",
               ev(found[0][3], f"字段为 {value_repr!r}", quote))


def metadata_value(task: Any, key: str) -> Any:
    return task.metadata.values.get(key)


def positive_int_or_none(value: Any) -> int | None:
    """Strict positive JSON integer (bool rejected); None when invalid/absent."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


__all__ = [
    "applicability_unknown_outcome",
    "decide",
    "entities_of_kind_with",
    "ev",
    "field_check",
    "first_target",
    "metadata_value",
    "not_applicable_outcome",
    "positive_int_or_none",
    "quantity_of",
    "read",
    "read_quantity",
    "req",
]
