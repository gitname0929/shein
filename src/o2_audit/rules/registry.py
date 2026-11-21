"""RuleRegistry: all 62 (rule_id, rule_version) pairs with typed plans.

Sources (repo-local, read-only):

- ``resources/rules/进阶规则60条.jsonl``  -> R001-R060 @3.0 (F01-F10 families)
- ``resources/rules/基础规则版本.jsonl``   -> B001 @3.0 / @3.1

**No silent fallback**: ``get_plan`` / ``evaluate`` raise
:class:`UnsupportedRuleError` (a ``KeyError``) for unknown ID/version pairs —
unsupported rules never degrade to a generic pass-through plan.

Each plan carries the exact raw rule text and clause citations loaded from the
resource files; evaluators consume FactView + typed plan only. Evaluators for
advanced rules return the shared :class:`RuleOutcome`; B001 keeps its
backward-compatible :class:`B001Outcome` (a ``RuleOutcome`` subclass).
"""
from __future__ import annotations

import json
from typing import Any, Callable

from o2_audit.domain.models import AuditInput
from o2_audit.resources import rules_dir
from o2_audit.runtime.facts import FactView
from o2_audit.rules.advanced import FAMILIES, FAMILY_BY_RULE
from o2_audit.rules.advanced.base import (
    applicability_unknown_outcome,
    not_applicable_outcome,
)
from o2_audit.rules.base import RuleOutcome, RulePlan, UnsupportedRuleError
from o2_audit.rules.b001.evaluate import applicability_precheck as b001_applicability
from o2_audit.rules.b001.evaluate import evaluate_b001
from o2_audit.rules.b001.plan import QUOTES_3_0, QUOTES_3_1, B001Plan


ApplicabilityFn = Callable[[AuditInput, RulePlan], tuple[str, list[str]]]
EvaluatorFn = Callable[[AuditInput, RulePlan, FactView], RuleOutcome]


def _load_jsonl(path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _clause_quotes(text: str) -> dict[str, str]:
    """Stable quote keys: full text plus numbered clauses (split on ；)."""
    quotes = {"rule_text": text}
    for i, clause in enumerate(text.split("；")):
        clause = clause.strip()
        if clause:
            quotes[f"clause_{i}"] = clause
    return quotes


class RuleRegistry:
    """Loads and serves the 62 registered (rule_id, rule_version) plans."""

    def __init__(self, rules_directory=None) -> None:
        base = rules_directory if rules_directory is not None else rules_dir()
        self._plans: dict[tuple[str, str], RulePlan] = {}
        self._evaluators: dict[tuple[str, str], EvaluatorFn] = {}
        self._applicability: dict[tuple[str, str], ApplicabilityFn] = {}
        self._sources: dict[tuple[str, str], dict[str, Any]] = {}

        for entry in _load_jsonl(base / "基础规则版本.jsonl"):
            self._register_b001(entry)
        for entry in _load_jsonl(base / "进阶规则60条.jsonl"):
            self._register_advanced(entry)

    # -- registration -------------------------------------------------------

    def _register_b001(self, entry: dict[str, Any]) -> None:
        version = str(entry["version"])
        quotes = QUOTES_3_0 if version == "3.0" else QUOTES_3_1
        key = ("B001", version)
        self._plans[key] = B001Plan(
            rule_version=version,
            small_tag_threshold_mm="100" if version == "3.0" else "80",
            rule_quotes=dict(quotes),
            quotes=dict(quotes),
            rule_family_id="B001",
            title=str(entry.get("title", "")),
            raw_rule_text=str(entry.get("rule_text", "")),
            material_roles=tuple(entry.get("material_role_candidates", ())),
        )
        self._evaluators[key] = evaluate_b001
        self._applicability[key] = b001_applicability
        self._sources[key] = entry

    def _register_advanced(self, entry: dict[str, Any]) -> None:
        rule_id = str(entry["rule_id"])
        if rule_id not in FAMILY_BY_RULE:
            raise ValueError(f"no family module registered for {rule_id}")
        family = FAMILY_BY_RULE[rule_id]
        key = (rule_id, str(entry["version"]))
        self._plans[key] = family.PLAN_CLASS(
            rule_id=rule_id,
            rule_version=str(entry["version"]),
            rule_family_id=str(entry.get("rule_family_id", "")),
            title=str(entry.get("title", "")),
            raw_rule_text=str(entry.get("rule_text", "")),
            quotes=_clause_quotes(str(entry.get("rule_text", ""))),
            material_roles=tuple(entry.get("material_role_candidates", ())),
            capability_deps=("inspect_material",),
        )
        self._evaluators[key] = family.EVALUATORS[rule_id]
        self._applicability[key] = family.APPLICABILITY[rule_id]
        self._sources[key] = entry

    # -- lookup ---------------------------------------------------------------

    def keys(self) -> list[tuple[str, str]]:
        return sorted(self._plans.keys())

    def __contains__(self, key: object) -> bool:
        return key in self._plans

    def __len__(self) -> int:
        return len(self._plans)

    def _require(self, rule_id: str, rule_version: str) -> tuple[str, str]:
        key = (str(rule_id), str(rule_version))
        if key not in self._plans:
            raise UnsupportedRuleError(str(rule_id), str(rule_version))
        return key

    def get_plan(self, rule_id: str, rule_version: str) -> RulePlan:
        return self._plans[self._require(rule_id, rule_version)]

    def get_evaluator(self, rule_id: str, rule_version: str) -> EvaluatorFn:
        return self._evaluators[self._require(rule_id, rule_version)]

    def get_applicability(self, rule_id: str, rule_version: str) -> ApplicabilityFn:
        return self._applicability[self._require(rule_id, rule_version)]

    def get_source_entry(self, rule_id: str, rule_version: str) -> dict[str, Any]:
        return self._sources[self._require(rule_id, rule_version)]

    # -- evaluation ------------------------------------------------------------

    def applicability_precheck(self, task: AuditInput,
                               plan: RulePlan | None = None) -> tuple[str, list[str]]:
        """Metadata-level applicability; short-circuits before any gateway call."""
        p = plan if plan is not None else self.get_plan(task.rule_id, task.rule_version)
        fn = self._applicability[(p.rule_id, p.rule_version)]
        return fn(task, p)

    def evaluate(self, task: AuditInput, facts: FactView,
                 plan: RulePlan | None = None) -> RuleOutcome:
        p = plan if plan is not None else self.get_plan(task.rule_id, task.rule_version)
        # The task's requested (rule_id, version) must be registered — never
        # fall back to another plan silently.
        self._require(task.rule_id, task.rule_version)
        if (p.rule_id, p.rule_version) != (task.rule_id, task.rule_version):
            raise UnsupportedRuleError(
                task.rule_id,
                f"{task.rule_version} (plan is {p.rule_id}@{p.rule_version})")
        # Consistent applicability precheck for non-B001 rules: calling
        # ``registry.evaluate`` directly must produce the same applicability
        # semantics as ``AuditController.run`` (short-circuit BEFORE any
        # evaluation-side unknowns leak into the outcome).
        if p.rule_id != "B001":
            applicability, missing = self.applicability_precheck(task, p)
            if applicability == "not_applicable":
                return not_applicable_outcome()
            if applicability == "unknown":
                return applicability_unknown_outcome(missing)
        fn = self._evaluators[(p.rule_id, p.rule_version)]
        return fn(task, p, facts)


_DEFAULT: RuleRegistry | None = None


def default_registry() -> RuleRegistry:
    """Process-wide registry over the repo-local resources (lazy singleton)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = RuleRegistry()
    return _DEFAULT


__all__ = ["RuleRegistry", "default_registry", "UnsupportedRuleError"]
