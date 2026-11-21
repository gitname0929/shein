"""Conclusion quality evaluation plugin (Protocol 1.3.1).

Faithfully delegates joint ``(label, applicability)`` scoring, frozen
denominator accounting, duplicate/invalid/missing/extra/error handling,
artifact-version checking, and optional contrast scoring to vendored
``o2_public_contracts.quality``.

This module belongs strictly to the isolated evaluation boundary and MUST NOT
be imported by ``build``, ``infer``, ``annotate``, ``rules``, or ``runtime``.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from o2_public_contracts import contracts as public_contracts
from o2_public_contracts import quality as public_quality

PLUGIN_ID = "o2.conclusion"
PLUGIN_VERSION = "1.0.0"
INPUT_SCHEMA_VERSION = "1.3.1"


def _resolve_artifact_versions(
    references: list[dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, str] | None:
    """Resolve optional expected artifact-version map for ``quality.metric``."""
    raw_map = context.get("artifact_versions")
    if isinstance(raw_map, dict):
        return {str(k): str(v) for k, v in raw_map.items()}
    single = context.get("expected_artifact_version")
    if isinstance(single, str) and single.strip():
        keys = {
            f"{r['rule_id']}@{r['rule_version']}"
            for r in references
            if isinstance(r, dict) and r.get("rule_id") and r.get("rule_version")
        }
        return {k: single.strip() for k in keys}
    return None


def _summary_metrics(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "accuracy": raw["accuracy"],
        "machine_review_rate": raw["machine_review_rate"],
        "machine_accuracy": raw["machine_accuracy"],
        "applicability_accuracy": raw["applicability_accuracy"],
        "label_only_accuracy": raw["label_only_accuracy"],
        "correct_decisive_coverage": raw["correct_decisive_coverage"],
        "quality": raw["quality"],
        "all": raw["all"],
        "correct": raw["correct"],
        "label_only_correct": raw["label_only_correct"],
        "applicability_correct": raw["applicability_correct"],
        "binary_correct": raw["binary_correct"],
        "binary_truth": raw["binary_truth"],
        "pass": raw["pass"],
        "reject": raw["reject"],
        "ambiguous": raw["ambiguous"],
        "error": raw["error"],
        "invalid": raw["invalid"],
        "missing": raw["missing"],
        "extraneous": raw["extraneous"],
        "unidentified_invalid_records": raw["unidentified_invalid_records"],
    }


def evaluate(
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    references: list[dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Protocol 1.3.1 conclusion-quality plugin ``evaluate(inputs, outputs, references, context)``."""
    ctx = context if isinstance(context, dict) else {}
    errors: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []

    if not references:
        # L-FUNC-09: empty reference set -> core metrics MUST be null (never 0 or 1).
        empty_metrics = {
            "accuracy": None,
            "machine_review_rate": None,
            "machine_accuracy": None,
            "applicability_accuracy": None,
            "label_only_accuracy": None,
            "correct_decisive_coverage": None,
            "quality": None,
            "all": 0,
            "correct": 0,
            "label_only_correct": 0,
            "applicability_correct": 0,
            "binary_correct": 0,
            "binary_truth": 0,
            "pass": 0,
            "reject": 0,
            "ambiguous": 0,
            "error": 0,
            "invalid": 0,
            "missing": 0,
            "extraneous": len(outputs) if isinstance(outputs, list) else 0,
            "unidentified_invalid_records": 0,
        }
        diagnostics.append({
            "type": "empty_reference_set",
            "reason": "references is empty; core quality metrics are null (never 0 or 1).",
        })
        return {
            "plugin_id": PLUGIN_ID,
            "plugin_version": PLUGIN_VERSION,
            "input_schema_version": INPUT_SCHEMA_VERSION,
            "metrics": empty_metrics,
            "by_rule_version": {},
            "label_confusion_matrix": {},
            "contrasts": None,
            "diagnostics": diagnostics,
            "errors": errors,
            "official": False,
            "note": (
                "A/U/Q use legal outputs with both label and applicability correct. "
                "Official totals are computed only by organizer trusted code."
            ),
        }

    artifact_versions = _resolve_artifact_versions(references, ctx)

    try:
        overall = public_quality.metric(
            references, outputs, inputs, artifact_versions=artifact_versions
        )
    except public_contracts.ContractError as exc:
        errors.append({"code": "contract_error", "detail": str(exc)})
        return {
            "plugin_id": PLUGIN_ID,
            "plugin_version": PLUGIN_VERSION,
            "input_schema_version": INPUT_SCHEMA_VERSION,
            "metrics": {
                "accuracy": None,
                "machine_review_rate": None,
                "machine_accuracy": None,
                "applicability_accuracy": None,
                "label_only_accuracy": None,
                "correct_decisive_coverage": None,
                "quality": None,
            },
            "by_rule_version": {},
            "label_confusion_matrix": {},
            "contrasts": None,
            "diagnostics": diagnostics,
            "errors": errors,
            "official": False,
        }

    # Per-(rule_id, rule_version) grouped metrics (never mixes versions).
    refs_by_rv: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ref in references:
        rv_key = f"{ref['rule_id']}@{ref['rule_version']}"
        refs_by_rv[rv_key].append(ref)

    by_rule_version: dict[str, dict[str, Any]] = {}
    for rv_key in sorted(refs_by_rv):
        sub_refs = refs_by_rv[rv_key]
        sub_ids = {r["audit_unit_id"] for r in sub_refs}
        sub_outputs = [
            p for p in outputs
            if isinstance(p, dict) and p.get("audit_unit_id") in sub_ids
        ]
        sub_m = public_quality.metric(
            sub_refs, sub_outputs, inputs, artifact_versions=artifact_versions
        )
        by_rule_version[rv_key] = _summary_metrics(sub_m)

    # Optional contrast groups ONLY if explicitly provided in context.
    contrast_result: dict[str, Any] | None = None
    contrast_groups = ctx.get("contrast_groups")
    if contrast_groups is not None:
        try:
            cs, cd = public_quality.contrasts(
                references,
                outputs,
                inputs,
                contrast_groups,
                artifact_versions=artifact_versions,
            )
            contrast_result = {"by_rule": cs, "details": cd}
        except public_contracts.ContractError as exc:
            errors.append({"code": "contrast_contract_error", "detail": str(exc)})

    correct_set = set(overall["correct_ids"])
    pred_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pred in outputs:
        if isinstance(pred, dict) and isinstance(pred.get("audit_unit_id"), str):
            pred_by_id[pred["audit_unit_id"]].append(pred)

    for ref in references:
        uid = ref["audit_unit_id"]
        rows = pred_by_id.get(uid, [])
        pred_app = rows[0].get("applicability") if len(rows) == 1 else None
        diagnostics.append({
            "audit_unit_id": uid,
            "rule_id": ref["rule_id"],
            "rule_version": ref["rule_version"],
            "truth_label": ref["label"],
            "truth_applicability": ref["applicability"],
            "outcome": overall["outcomes"][uid],
            "pred_applicability": pred_app,
            "correct": uid in correct_set,
            "issues": overall["issues"].get(uid, []),
        })

    return {
        "plugin_id": PLUGIN_ID,
        "plugin_version": PLUGIN_VERSION,
        "input_schema_version": INPUT_SCHEMA_VERSION,
        "metrics": _summary_metrics(overall),
        "by_rule_version": by_rule_version,
        "label_confusion_matrix": overall["label_confusion_matrix"],
        "contrasts": contrast_result,
        "diagnostics": diagnostics,
        "errors": errors,
        "official": False,
        "note": (
            "A/U/Q use legal outputs with both label and applicability correct. "
            "Official totals are computed only by organizer trusted code."
        ),
    }
