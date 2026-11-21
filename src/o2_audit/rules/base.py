"""Unified rule plan / outcome interface shared by B001 and R001–R060.

Layering (unchanged four-layer architecture):

- **Plans** (this module + versioned family plans) carry ONLY rule-specific
  parameters: thresholds, authorized conversions, patterns, raw rule text and
  citations from ``resources/rules/``. Shared operators never hardcode them.
- **Evaluators** (``rules/b001`` and ``rules/advanced``) consume a
  ``FactView`` + typed plan and produce a :class:`RuleOutcome`.
- **Controllers** (``runtime/controller.py``) drive any registered plan through
  the same applicability → conflict → violation → unknown → satisfied order.

``B001Outcome``/``B001Plan`` subclass these types, so every Stage 01 API
stays backward compatible.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UnsupportedRuleError(KeyError):
    """Requested ``(rule_id, rule_version)`` is not registered.

    Subclasses :class:`KeyError` so callers may catch either. Registry access
    NEVER falls back to a generic pass-through plan: an unknown ID/version is
    a programming/configuration error, not a verdict.
    """

    def __init__(self, rule_id: str, rule_version: str) -> None:
        super().__init__(f"unsupported rule: {rule_id}@{rule_version}")
        self.rule_id = rule_id
        self.rule_version = rule_version


class RuleOutcome:
    """Decision result of one rule evaluation.

    Attributes (identical contract to the Stage 01 ``B001Outcome``):

    - ``applicability``: ``applicable`` | ``not_applicable`` | ``unknown``
      (not a conclusion label).
    - ``label``: ``pass`` | ``reject`` | ``ambiguous``.
    - ``reason_code`` / ``reason``: stable code + human explanation.
    - ``results``: per-requirement :class:`RequirementResult` rows.
    - ``evidence``: evidence rows for the final prediction.
    - ``missing_applicability_conditions``: metadata keys missing when
      ``applicability == "unknown"``.
    """

    def __init__(
        self,
        applicability: str,
        label: str,
        reason_code: str,
        reason: str,
        results: list[Any],
        evidence: list[Any],
        missing_applicability_conditions: list[str] | None = None,
    ) -> None:
        self.applicability = applicability
        self.label = label
        self.reason_code = reason_code
        self.reason = reason
        self.results = results
        self.evidence = evidence
        self.missing_applicability_conditions = missing_applicability_conditions or []

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"RuleOutcome(applicability={self.applicability!r}, label={self.label!r}, "
                f"reason_code={self.reason_code!r})")


class RulePlan(BaseModel):
    """Base of every typed, versioned rule plan.

    Rule-specific parameters (thresholds, authorized conversion factors,
    patterns, quote keys) live in subclasses — shared operators and the
    controller stay rule-ID independent.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    rule_version: str
    rule_family_id: str = ""
    title: str = ""
    # Exact rule wording loaded from resources/rules/ (raw citation).
    raw_rule_text: str = ""
    general_policy_ref: str = "规则总则.md"
    # Stable quote key -> original rule-text fragment (Evidence.rule_quote).
    quotes: dict[str, str] = Field(default_factory=dict)
    # Predicates the evaluator needs from observed entities.
    required_predicates: tuple[str, ...] = Field(default_factory=tuple)
    # Material roles the rule may inspect.
    material_roles: tuple[str, ...] = Field(default_factory=tuple)
    # Capability dependencies (e.g. visual inspection) declared up front.
    capability_deps: tuple[str, ...] = Field(default_factory=tuple)
    # reason_code -> (entity_kind, predicate, candidate roles) evidence-gap
    # hints used by the controller's NeedEvidence loop for this plan.
    gap_hints: dict[str, tuple[str, str, tuple[str, ...]]] = Field(default_factory=dict)

    def quote(self, key: str) -> str | None:
        return self.quotes.get(key)

    def gap_for(self, reason_code: str) -> tuple[str, str, tuple[str, ...]] | None:
        """Evidence-gap target for a remaining unknown reason code.

        Plans may override per reason code via ``gap_hints``; the generic
        fallback points at the plan's own material roles with a neutral
        predicate so the gateway request stays rule-ID independent.
        """
        hint = self.gap_hints.get(reason_code)
        if hint is not None:
            return hint
        if self.material_roles:
            return ("object", "observation", tuple(self.material_roles))
        return None


__all__ = ["RuleOutcome", "RulePlan", "UnsupportedRuleError"]
