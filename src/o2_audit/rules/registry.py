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


# Declarative per-rule ontology mapping: rule_id -> (primary entity kind,
# required observation predicates in canonical domain.vocabulary.P terms).
# Populated on every advanced RulePlan so the visual gateway extracts all
# rule-relevant predicates for each physical entity in one cohesive pass.
_ADVANCED_ONTOLOGY: dict[str, tuple[str, tuple[str, ...]]] = {
    "R001": ("package", ("boundary_visible", "interior_obstructed")),
    "R002": ("package", ("visible_faces",)),
    "R003": ("document", ("printed_page_number", "total_printed_pages")),
    "R004": ("label", ("carrier", "attached_to_package")),
    "R005": ("label", ("interior_obstructed", "model", "material_number", "supplement_field_identity")),
    "R006": ("document", ("material_authenticity",)),
    "R007": ("label", ("model",)),
    "R008": ("package", ("component_models",)),
    "R009": ("object", ("serial_number",)),
    "R010": ("label", ("main_model", "compatible_models")),
    "R011": ("certificate", ("old_model", "replacement_map")),
    "R012": ("label", ("model",)),
    "R013": ("label", ("material_number",)),
    "R014": ("label", ("batch_number",)),
    "R015": ("label", ("piece_count",)),
    "R016": ("label", ("support_url", "qr_present")),
    "R017": ("label", ("valid_from", "valid_until")),
    "R018": ("label", ("product_name_cn", "product_name_en")),
    "R019": ("label", ("model", "view_type")),
    "R020": ("object", ("serial_carrier",)),
    "R021": ("label", ("model", "material_number")),
    "R022": ("label", ("material_number", "carrier", "attached_to_package")),
    "R023": ("label", ("line_crosses_field", "model", "material_number")),
    "R024": ("object", ("relative_position", "view_type")),
    "R025": ("object", ("shape_type", "is_closed", "is_filled", "text_inside_mark")),
    "R026": ("object", ("shape_type", "is_closed", "relative_position")),
    "R027": ("object", ("shape_type", "mark_bbox", "relative_position")),
    "R028": ("object", ("color_mode", "shape_type", "mark_color")),
    "R029": ("object", ("shape_type", "is_filled", "void_marked", "front_fully_shown")),
    "R030": ("object", ("height_ratio", "view_type")),
    "R031": ("label", ("verbatim_text",)),
    "R032": ("label", ("indoor_only_text", "outdoor_durable_text")),
    "R033": ("label", ("step_list",)),
    "R034": ("label", ("disassembly_clauses",)),
    "R035": ("label", ("language_text",)),
    "R036": ("label", ("verbatim_text",)),
    "R037": ("label", ("capacity",)),
    "R038": ("label", ("net_weight", "total_weight")),
    "R039": ("label", ("adjustable_height_min", "adjustable_height_max")),
    "R040": ("label", ("component_percentages",)),
    "R041": ("label", ("front_weight", "back_weight")),
    "R042": ("label", ("production_date",)),
    "R043": ("certificate", ("material_number",)),
    "R044": ("certificate", ("valid_from", "valid_until")),
    "R045": ("certificate", ("covered_models",)),
    "R046": ("certificate", ("void_marked",)),
    "R047": ("certificate", ("document_version",)),
    "R048": ("certificate", ("withdrawal_date", "issue_date", "valid_from", "valid_until")),
    "R049": ("label", ("material_number",)),
    "R050": ("certificate", ("component_model_map",)),
    "R051": ("label", ("batch_number", "batch_interval")),
    "R052": ("label", ("package_spec", "model_spec_map")),
    "R053": ("label", ("model", "material_number", "figure_model", "figure_material_number")),
    "R054": ("certificate", ("material_number", "contact_info", "void_marked")),
    "R055": ("label", ("verbatim_text",)),
    "R056": ("label", ("contents_list",)),
    "R057": ("label", ("material_number", "carrier", "attached_to_package")),
    "R058": ("label", ("production_date", "model")),
    "R059": ("package", ("box_color", "shape_type")),
    "R060": ("label", ("model", "carrier")),
}


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
        roles = tuple(entry.get("material_role_candidates", ()))
        visual_roles = tuple(r for r in roles if r != "declared_metadata") or roles
        primary_kind, req_preds = _ADVANCED_ONTOLOGY.get(rule_id, ("object", ()))
        default_hints: dict[str, tuple[str, str, tuple[str, ...]]] = {}
        if req_preds and visual_roles:
            default_hints["*"] = (primary_kind, req_preds[0], visual_roles)
        self._plans[key] = family.PLAN_CLASS(
            rule_id=rule_id,
            rule_version=str(entry["version"]),
            rule_family_id=str(entry.get("rule_family_id", "")),
            title=str(entry.get("title", "")),
            raw_rule_text=str(entry.get("rule_text", "")),
            quotes=_clause_quotes(str(entry.get("rule_text", ""))),
            required_predicates=req_preds,
            material_roles=roles,
            capability_deps=("inspect_material",),
            gap_hints=default_hints,
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
